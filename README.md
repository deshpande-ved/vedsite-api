# vedsite-api

Flask backend behind [vedsite.com](https://vedsite.com), served at `api.vedsite.com`. It powers the live music and film widgets on the misc page and a couple of panels on a Home Assistant kiosk.

## Endpoints

Public, all `GET`, all safe to open in a browser to check they're up:

| Endpoint | Returns |
|---|---|
| [`/api/spotify/now-playing`](https://api.vedsite.com/api/spotify/now-playing) | current Spotify track |
| [`/api/spotify/queue`](https://api.vedsite.com/api/spotify/queue) | upcoming queue (up to 10) |
| [`/api/spotify/recently-played`](https://api.vedsite.com/api/spotify/recently-played) | recent Spotify plays |
| [`/api/lastfm/top-artists`](https://api.vedsite.com/api/lastfm/top-artists) | top artists |
| [`/api/lastfm/recent-tracks`](https://api.vedsite.com/api/lastfm/recent-tracks) | recent scrobbles (does a Spotify lookup per song, so it's slow) |
| [`/api/lastfm/search?q=radiohead`](https://api.vedsite.com/api/lastfm/search?q=radiohead) | songs in the library matching `q` |
| [`/api/lastfm/track?artist=Radiohead&track=Creep`](https://api.vedsite.com/api/lastfm/track?artist=Radiohead&track=Creep) | plays and first/last scrobble for one song |
| [`/api/letterboxd/recent`](https://api.vedsite.com/api/letterboxd/recent) | recent diary entries from RSS |
| [`/api/letterboxd/top4`](https://api.vedsite.com/api/letterboxd/top4) | the four films in `top4.json` |
| [`/api/letterboxd/search?q=master`](https://api.vedsite.com/api/letterboxd/search?q=master) | logged films matching `q` |

Internal only: `/api/homelab` and `/api/mbta` are routes in this app, but the reverse proxy blocks them on `api.vedsite.com` (a 403 there is expected). Only the Home Assistant kiosk uses them, and it reaches them over a private network.

## Files

**`server.py`** — The Flask app and entry point: it sets the CORS headers, registers the blueprints below, and runs on port 5000. It owns the Spotify integration, including token storage with automatic refresh, plus the now-playing, queue and recently-played endpoints. It also serves the Last.fm top-artists and recent-tracks feeds and the Letterboxd recent and top-four endpoints.

**`search.py`** — A blueprint behind the "have i heard it? / have i seen it?" search bars on the misc page. It builds and periodically rebuilds an index of the full Last.fm library, and looks up per-song play counts and first/last scrobble dates on demand. Film search merges a Letterboxd export with an RSS log that is refreshed in the background, so entries persist after they drop out of the 50-item feed.

**`homelab.py`** — A blueprint at `/api/homelab` (internal only, see below) that feeds the homelab status panel on the Home Assistant kiosk. It signs in to a Beszel monitoring hub's PocketBase API with a read-only account and flattens the latest one-minute sample into a single JSON payload. Responses are cached for 20 seconds and served stale if the hub is unreachable, so the panel doesn't blank out.

**`mbta.py`** — A blueprint at `/api/mbta` (internal only, see below) that returns the next trains in each direction for the kiosk's MBTA transit sign. By default it shows MFA on the Green Line E branch and falls back to Fenway on the D branch when the E branch is suspended or shuttled. Passing `?station=` (`mfa`, `northeastern`, `fenway` or `ruggles`) picks a single station, and results are cached per station for 20 seconds.

**`notify.py`** — Discord webhook alerts for failures that need a human, such as a dead Spotify refresh token or one that is about to expire. Because the frontend polls constantly, alerts are throttled with per-key cooldowns so the same failure doesn't fire hundreds of times an hour. Cooldown state is written to disk rather than kept in memory, so it survives container restarts.

**`top4.json`** — A hand-maintained list of the four favourite films shown on the misc page. Each entry holds the title, year, rating, poster URL and Letterboxd link. `/api/letterboxd/top4` serves it as-is, so changing the picks means editing this file and rebuilding.

**`requirements.txt`** — The Python dependencies: Flask, requests, feedparser and python-dotenv. The Docker image installs them with pip at build time. Versions are unpinned, so a rebuild pulls the latest releases.

**`Dockerfile`** — Builds the image on `python:3.11-slim`, installing the requirements and copying in each source file plus `top4.json`. Because the code is copied into the image rather than mounted, a plain container restart won't pick up changes. Redeploy with `docker compose up -d --build`.

**`docker-compose.yml`** — Runs the image as the `vedsite-api` container, bound to localhost only, with a reverse proxy and tunnel handling public traffic. It mounts a secrets directory for API keys and tokens, plus a `data/` volume where the search caches and Letterboxd export are kept. It restarts the container automatically unless it was stopped on purpose.

**`.gitignore`** — Keeps secrets (`.env`), the `data/` cache directory, the local virtualenv, bytecode and `.bak` files out of the repo. Everything in `data/` can be rebuilt from a fresh Letterboxd export and the Last.fm API. Credentials live outside the repo and are loaded through environment variables at runtime.
