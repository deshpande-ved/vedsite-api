"""
search endpoints for the misc page
  /api/lastfm/search?q=           songs in my all-time Last.fm library
  /api/lastfm/track?artist=&track= plays + first/last scrobble for one song
  /api/letterboxd/search?q=       films from the Letterboxd export + RSS

Registered as a blueprint in server.py. Writes its caches to DATA_DIR, which
must be a mounted volume so they survive a rebuild.
"""

import csv
import json
import os
import re
import tempfile
import threading
import time
import unicodedata

import feedparser
import requests
from flask import Blueprint, jsonify, request

search_bp = Blueprint('search', __name__)

LASTFM_API_KEY = os.environ["LASTFM_API_KEY"]
LASTFM_USER = os.environ.get("LASTFM_USER", "yerasaki")
LETTERBOXD_USER = os.environ.get("LETTERBOXD_USER", "yerasaki")
DATA_DIR = os.environ.get("SEARCH_DATA_DIR", "data")

LASTFM_ROOT = "https://ws.audioscrobbler.com/2.0/"
LIBRARY_FILE = os.path.join(DATA_DIR, "lastfm_library.json")
LIBRARY_MAX_AGE = 6 * 3600      # rebuild the song index every 6 hours
LIBRARY_RETRY_AFTER = 120       # after a failed build, wait before trying again
DETAIL_MAX_AGE = 3600           # per-song scrobble dates cached for an hour

LETTERBOXD_DIR = os.path.join(DATA_DIR, "letterboxd")   # diary.csv + watched.csv from the export
RSS_LOG_FILE = os.path.join(DATA_DIR, "letterboxd_rss_log.json")
RSS_MAX_AGE = 15 * 60           # a search re-reads the RSS feed at most every 15 minutes
RSS_BACKGROUND_EVERY = 3600     # and any API request triggers a background re-read hourly

MAX_QUERY = 100
MAX_RESULTS = 8

# Spotify lives in server.py; it hands us its token getter so album art uses
# the same auto-refreshing token as everything else.
_spotify_token_getter = None


def set_spotify_token_getter(getter):
    global _spotify_token_getter
    _spotify_token_getter = getter


# HELPERS

def save_json(path, data):
    """Atomic write: temp file in the same dir, then os.replace."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), suffix='.tmp')
    with os.fdopen(fd, 'w') as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp_path, 0o644)  # mkstemp makes it root-only; keep it readable on the host
    os.replace(tmp_path, path)


def load_json(path, default):
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


# Remaster / feat. / live tags that make one song show up under several names.
_VARIANT_SUFFIX = re.compile(
    r"\s*(?:[-–(\[]\s*)(?:\d{4}\s+)?(?:remaster(?:ed)?|feat\.?|ft\.?|with |live|mono|stereo|"
    r"deluxe|bonus|radio edit|single version|album version|original mix|edit\b|version)"
    r".*$",
    re.IGNORECASE,
)


def normalize(text):
    """Lowercase, strip accents and punctuation. Keeps non-Latin letters."""
    text = unicodedata.normalize('NFKD', text or '')
    text = ''.join(c for c in text if not unicodedata.combining(c))
    text = text.lower().replace('&', ' and ')
    text = re.sub(r"[^\w\s]", ' ', text)
    return ' '.join(text.split())


def base_title(title):
    return normalize(_VARIANT_SUFFIX.sub('', title or '')) or normalize(title)


def match_score(query_tokens, title, extra=''):
    """0 = no match. Every query word has to start some word in title + extra."""
    words = (title + ' ' + extra).split()
    if not all(any(w.startswith(q) for w in words) for q in query_tokens):
        return 0
    query = ' '.join(query_tokens)
    if title == query:
        return 3
    if title.startswith(query):
        return 2
    return 1


def read_query():
    q = normalize(request.args.get('q', '')[:MAX_QUERY])
    return q.split() if len(q) >= 2 else None


# LAST.FM LIBRARY INDEX

_library = {"built_at": 0, "songs": []}
_library_lock = threading.Lock()
_library_building = False
_library_failed_at = 0


def log(message):
    # The container runs python without -u; flush so this reaches docker logs.
    print(message, flush=True)


def lastfm_get(params, timeout=20, attempts=1):
    """Last.fm call; with attempts > 1, retries with backoff. Deep
    user.gettoptracks pages fail intermittently ('Operation failed')."""
    for attempt in range(attempts):
        try:
            response = requests.get(
                LASTFM_ROOT,
                params={**params, "api_key": LASTFM_API_KEY, "format": "json"},
                timeout=timeout,
            )
            data = response.json()
            if response.status_code != 200 or 'error' in data:
                raise RuntimeError(f"Last.fm {params.get('method')}: {data.get('message', response.status_code)}")
            return data
        except (requests.RequestException, RuntimeError, ValueError):
            if attempt == attempts - 1:
                raise
            time.sleep(2 ** attempt * 2)  # 2s, 4s, 8s...


def build_library():
    """Page through every track I've ever scrobbled and group name variants."""
    global _library, _library_building, _library_failed_at
    try:
        raw = []
        page, total_pages = 1, 1
        while page <= total_pages:
            data = lastfm_get({
                "method": "user.gettoptracks",
                "user": LASTFM_USER,
                "period": "overall",
                "limit": 1000,
                "page": page,
            }, timeout=30, attempts=5)
            top = data.get('toptracks', {})
            total_pages = int(top.get('@attr', {}).get('totalPages', 1))
            for t in top.get('track', []):
                raw.append((t['name'], t['artist']['name'], int(t.get('playcount', 0)), t.get('url', '')))
            page += 1
            time.sleep(0.25)  # stay well under Last.fm's rate limit

        # "Band On The Run" and "Band On The Run - 2010 Remaster" are one song.
        grouped = {}
        for name, artist, plays, url in raw:
            key = (base_title(name), normalize(artist))
            song = grouped.setdefault(key, {
                "track": name, "artist": artist, "plays": 0, "url": url,
                "variants": [], "_top": -1,
            })
            song["plays"] += plays
            song["variants"].append(name)
            if plays > song["_top"]:  # display the most-played spelling
                song.update(track=name, url=url, _top=plays)

        songs = []
        for (title_key, artist_key), song in grouped.items():
            song.pop("_top")
            song["title_key"] = title_key
            song["artist_key"] = artist_key
            songs.append(song)

        index = {"built_at": time.time(), "songs": songs}
        save_json(LIBRARY_FILE, index)
        with _library_lock:
            _library = index
        log(f"search: indexed {len(songs)} songs from {len(raw)} Last.fm tracks")
    except (requests.RequestException, RuntimeError, ValueError, KeyError) as e:
        # Keep serving the previous index; retry after LIBRARY_RETRY_AFTER.
        _library_failed_at = time.time()
        log(f"ERROR: Last.fm library build failed: {e}")
    finally:
        with _library_lock:
            _library_building = False


def get_library():
    """Current index (possibly stale). Starts a background rebuild when due."""
    global _library, _library_building
    with _library_lock:
        if not _library["songs"]:
            _library = load_json(LIBRARY_FILE, _library)
        stale = time.time() - _library.get("built_at", 0) > LIBRARY_MAX_AGE
        cooling = time.time() - _library_failed_at < LIBRARY_RETRY_AFTER
        if stale and not cooling and not _library_building:
            _library_building = True
            threading.Thread(target=build_library, daemon=True).start()
        return _library


@search_bp.route('/api/lastfm/search')
def lastfm_search():
    tokens = read_query()
    if tokens is None:
        return jsonify({"songs": []})

    library = get_library()
    if not library["songs"]:
        return jsonify({"songs": [], "indexing": True}), 503

    scored = []
    for song in library["songs"]:
        score = match_score(tokens, song["title_key"], song["artist_key"])
        if score:
            scored.append((score, song["plays"], song))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)

    return jsonify({"songs": [{
        "track": s["track"],
        "artist": s["artist"],
        "plays": s["plays"],
        "variants": s["variants"][:5],
    } for _, _, s in scored[:MAX_RESULTS]]})


# LAST.FM SINGLE SONG DETAIL

_detail_cache = {}


def scrobble_page(artist, track, page):
    """One scrobble of this track (limit=1), so totalPages == total scrobbles.
    user.getTrackScrobbles is undocumented but has been stable for years."""
    data = lastfm_get({
        "method": "user.gettrackscrobbles",
        "user": LASTFM_USER,
        "artist": artist,
        "track": track,
        "limit": 1,
        "page": page,
    }, timeout=10)
    body = data.get('trackscrobbles') or next(
        (v for v in data.values() if isinstance(v, dict) and 'track' in v), {})
    attr = body.get('@attr', {})
    items = body.get('track', [])
    if isinstance(items, dict):
        items = [items]
    dated = [int(t['date']['uts']) for t in items if t.get('date', {}).get('uts')]
    return int(attr.get('totalPages', 0)), (dated[0] if dated else None)


def album_art(artist, track):
    token = _spotify_token_getter() if _spotify_token_getter else None
    if not token:
        return None
    try:
        search = requests.get(
            "https://api.spotify.com/v1/search",
            headers={"Authorization": f"Bearer {token}"},
            params={"q": f"{track} {artist}", "type": "track", "limit": 1},
            timeout=5,
        )
        if search.status_code == 200:
            items = search.json().get('tracks', {}).get('items', [])
            if items:
                images = items[0]['album'].get('images', [])
                if images:
                    return images[-1]['url']  # smallest = 64x64 thumb
    except (requests.RequestException, ValueError, KeyError, IndexError):
        pass
    return None


@search_bp.route('/api/lastfm/track')
def lastfm_track():
    artist = request.args.get('artist', '')[:MAX_QUERY]
    track = request.args.get('track', '')[:MAX_QUERY]

    # Only songs already in my library: this endpoint spends Last.fm calls,
    # so it can't be pointed at arbitrary input.
    key = (base_title(track), normalize(artist))
    song = next((s for s in get_library()["songs"]
                 if (s["title_key"], s["artist_key"]) == key), None)
    if song is None:
        return jsonify({"error": "not in library"}), 404

    cache_key = '|'.join(key)
    cached = _detail_cache.get(cache_key)
    if cached and time.time() - cached[0] < DETAIL_MAX_AGE:
        return jsonify(cached[1])

    plays, first, last, dated = 0, None, None, True
    for name in song["variants"][:5]:
        try:
            total, newest = scrobble_page(song["artist"], name, 1)
            if total == 0:
                continue
            _, oldest = scrobble_page(song["artist"], name, total) if total > 1 else (total, newest)
        except (requests.RequestException, RuntimeError, ValueError, KeyError) as e:
            log(f"ERROR: scrobble lookup failed for {song['artist']} - {name}: {e}")
            dated = False
            break
        plays += total
        if newest and (last is None or newest > last):
            last = newest
        if oldest and (first is None or oldest < first):
            first = oldest

    result = {
        "track": song["track"],
        "artist": song["artist"],
        "url": song["url"],
        # Fall back to the index's count if the dated lookup failed.
        "plays": plays if dated and plays else song["plays"],
        "first_played": first if dated else None,
        "last_played": last if dated else None,
        "album_image": album_art(song["artist"], song["track"]),
    }
    _detail_cache[cache_key] = (time.time(), result)
    return jsonify(result)


# LETTERBOXD

_rss_lock = threading.Lock()
_rss_checked_at = 0


def read_export():
    """diary.csv (every logged watch, with tags) + watched.csv (films marked
    watched without a diary entry). Both come from Letterboxd's data export."""
    entries, watched = [], []
    try:
        with open(os.path.join(LETTERBOXD_DIR, 'diary.csv'), newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                tags = [t.strip() for t in (row.get('Tags') or '').split(',') if t.strip()]
                rating = row.get('Rating')
                entries.append({
                    "title": row.get('Name', ''),
                    "year": row.get('Year', ''),
                    "watched": row.get('Watched Date') or row.get('Date') or None,
                    "logged": row.get('Date') or None,
                    "rewatch": (row.get('Rewatch') or '').lower() == 'yes',
                    "rating": float(rating) if rating else None,
                    "tags": tags,
                    "url": row.get('Letterboxd URI', ''),
                })
    except FileNotFoundError:
        pass
    try:
        with open(os.path.join(LETTERBOXD_DIR, 'watched.csv'), newline='', encoding='utf-8') as f:
            for row in csv.DictReader(f):
                watched.append({
                    "title": row.get('Name', ''),
                    "year": row.get('Year', ''),
                    "url": row.get('Letterboxd URI', ''),
                })
    except FileNotFoundError:
        pass
    return entries, watched


def refresh_rss_log(max_age=RSS_MAX_AGE):
    """RSS only ever holds ~50 entries, so every entry it shows is kept in a log
    on disk: watches logged after the export survive falling off the feed.
    Entries still in the feed are rewritten each time, so later edits (rating,
    date) replace the old copy instead of being counted twice."""
    global _rss_checked_at
    with _rss_lock:
        if time.time() - _rss_checked_at < max_age:
            return
        _rss_checked_at = time.time()
        feed = feedparser.parse(f"https://letterboxd.com/{LETTERBOXD_USER}/rss/")
        if not feed.entries:
            return  # feed unreachable or empty: never wipe the log on a bad fetch
        old = load_json(RSS_LOG_FILE, {})
        rss = dict(old)
        for e in feed.entries:
            title = e.get('letterboxd_filmtitle')
            guid = e.get('id') or e.get('link')
            if not title or not guid:
                continue  # lists and other non-film items have no film title
            rating = e.get('letterboxd_memberrating')
            published = e.get('published_parsed')
            rss[guid] = {
                "title": title,
                "year": e.get('letterboxd_filmyear', ''),
                "watched": e.get('letterboxd_watcheddate'),
                # UTC date the entry was logged; compared with the export's Date
                "logged": time.strftime('%Y-%m-%d', published) if published else None,
                "rewatch": (e.get('letterboxd_rewatch') or '').lower() == 'yes',
                "rating": float(rating) if rating else None,
                "tags": [],  # the feed has no tags; they arrive with the next export
                # my diary entry (/yerasaki/film/slug/, or .../slug/2/ for a rewatch),
                # matching the export's boxd.it links, which also open the entry
                "url": e.get('link') or '',
            }
        if rss != old:
            save_json(RSS_LOG_FILE, rss)
            log(f"search: RSS log updated, {len(rss)} entries")


def build_films():
    refresh_rss_log()
    diary, watched = read_export()

    # The export is the truth for everything it covers: an RSS entry logged
    # before the export's newest entry is ignored (so edits and deletions made
    # since are respected). Around the export date, an RSS entry that matches a
    # diary entry is the same watch: keep the export's copy (it has tags), but
    # take the RSS link. Anything newer is added.
    export_through = max((d["logged"] for d in diary if d["logged"]), default=None)
    in_diary = {(normalize(d["title"]), str(d["year"]), d["watched"]): d for d in diary}
    entries = list(diary)
    for r in load_json(RSS_LOG_FILE, {}).values():
        if export_through and r.get("logged") and r["logged"] < export_through:
            continue
        match = in_diary.get((normalize(r["title"]), str(r["year"]), r["watched"]))
        if match:
            match["url"] = r["url"] or match["url"]
        else:
            entries.append(r)

    films = {}
    for e in entries:
        key = (normalize(e["title"]), str(e["year"]))
        film = films.setdefault(key, {
            "title": e["title"], "year": e["year"], "url": e["url"],
            "dates": [], "rewatches": 0, "tags": [], "rating": None, "_rated": "", "_linked": "",
        })
        if e["watched"]:
            film["dates"].append(e["watched"])
        # link to my most recent entry for the film
        if e["url"] and (e["watched"] or '') >= film["_linked"]:
            film["url"], film["_linked"] = e["url"], e["watched"] or ''
        film["rewatches"] += 1 if e["rewatch"] else 0
        for tag in e["tags"]:
            if tag not in film["tags"]:
                film["tags"].append(tag)
        if e["rating"] is not None and (e["watched"] or '') >= film["_rated"]:
            film["rating"], film["_rated"] = e["rating"], e["watched"] or ''

    # Marked watched but never logged with a date.
    for w in watched:
        key = (normalize(w["title"]), str(w["year"]))
        films.setdefault(key, {
            "title": w["title"], "year": w["year"], "url": w["url"],
            "dates": [], "rewatches": 0, "tags": [], "rating": None, "_rated": "", "_linked": "",
        })

    return films


# UPKEEP

_upkeep_lock = threading.Lock()
_upkeep_rss_at = 0


def _background_rss():
    try:
        refresh_rss_log(max_age=0)
    except Exception as e:  # a background refresh must never take anything down
        log(f"ERROR: background RSS refresh failed: {e}")


@search_bp.before_app_request
def keep_fresh():
    """Searches aren't the only trigger. The misc page and the wall kiosk hit this
    API all day, so any request can start a refresh when one is due: the RSS log
    hourly (the feed only holds ~50 entries) and the song index every 6 hours.
    Nothing here blocks or fails the request that triggered it."""
    global _upkeep_rss_at
    try:
        now = time.time()
        if now - _upkeep_rss_at >= RSS_BACKGROUND_EVERY:
            with _upkeep_lock:
                if now - _upkeep_rss_at >= RSS_BACKGROUND_EVERY:
                    _upkeep_rss_at = now
                    threading.Thread(target=_background_rss, daemon=True).start()
        get_library()  # cheap unless stale, then it rebuilds in the background
    except Exception as e:
        log(f"ERROR: upkeep check failed: {e}")


@search_bp.route('/api/letterboxd/search')
def letterboxd_search():
    tokens = read_query()
    if tokens is None:
        return jsonify({"films": []})

    scored = []
    for (title_key, _), film in build_films().items():
        score = match_score(tokens, title_key)
        if score:
            dates = sorted(film["dates"])
            scored.append((score, dates[-1] if dates else '', film, dates))
    scored.sort(key=lambda s: (s[0], s[1]), reverse=True)

    return jsonify({"films": [{
        "title": f["title"],
        "year": f["year"],
        "url": f["url"],
        "times_watched": max(len(dates), 1),
        "rewatches": f["rewatches"],
        "first_watched": dates[0] if dates else None,
        "last_watched": dates[-1] if dates else None,
        "rating": f["rating"],
        "tags": f["tags"],
    } for _, _, f, dates in scored[:MAX_RESULTS]]})
