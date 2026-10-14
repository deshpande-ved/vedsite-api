"""
Discord notifications for vedsite-api.

Alert-worthy failures here are sticky, not transient: once a Spotify refresh
token dies, every subsequent request fails the same way. Since the frontend
polls continuously, an un-throttled webhook would fire hundreds of times an
hour. Cooldown state therefore lives on disk (in the mounted secrets volume)
rather than in memory, so it survives container restarts.
"""

import json
import os
import time

import requests

DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")

# Same directory the token files live in — already a mounted volume, so
# os.replace() is atomic here.
_SECRETS_DIR = os.path.dirname(
    os.environ.get("SPOTIFY_TOKENS_FILE", "/app/secrets/spotify_tokens.json")
) or "."
STATE_FILE = os.path.join(_SECRETS_DIR, "notify_state.json")


def _load_state():
    try:
        with open(STATE_FILE, "r") as f:
            content = f.read()
            return json.loads(content) if content.strip() else {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_state(state):
    tmp_path = STATE_FILE + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(state, f)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, STATE_FILE)


def send(content):
    """Post to Discord. Never raises — a failed alert must not break a request."""
    if not DISCORD_WEBHOOK:
        print("WARN: DISCORD_WEBHOOK not set, skipping notification")
        return False
    try:
        resp = requests.post(
            DISCORD_WEBHOOK, json={"content": content}, timeout=5
        )
        return resp.status_code in (200, 204)
    except Exception as exc:
        print(f"ERROR: Discord notification failed: {exc}")
        return False


def send_once(key, content, cooldown_s):
    """Send at most once per cooldown window for the given key."""
    state = _load_state()
    now = time.time()
    last = state.get(key, 0)
    if now - last < cooldown_s:
        return False
    if send(content):
        state[key] = now
        _save_state(state)
        return True
    return False


def clear(key):
    """Drop a cooldown so the next failure alerts immediately (call on recovery)."""
    state = _load_state()
    if key in state:
        del state[key]
        _save_state(state)


def notify_spotify_refresh_failed(error_body):
    """Spotify refresh returned no access_token.

    invalid_grant means the refresh token is dead and only a manual
    re-authorization will fix it, so it gets a long cooldown and explicit
    instructions. Anything else is likely transient (5xx, network) and gets a
    shorter one so a genuine outage still surfaces reasonably fast.
    """
    error = ""
    if isinstance(error_body, dict):
        error = error_body.get("error", "")

    if error == "invalid_grant":
        return send_once(
            "spotify_invalid_grant",
            "🔑 **Spotify re-authorization required**\n"
            "The refresh token is no longer valid (`invalid_grant`). "
            "Live Spotify data on vedsite.com is down until you re-auth and "
            "write a new `spotify_tokens.json`.\n"
            "_Muted for 12 hours._",
            43200,
        )

    return send_once(
        "spotify_refresh_error",
        f"⚠️ **Spotify token refresh failed**\n"
        f"```{str(error_body)[:400]}```\n"
        "_Muted for 1 hour._",
        3600,
    )


def notify_spotify_expiring(days_left):
    """Heads-up before the ~6-month authorization window closes."""
    return send_once(
        "spotify_expiring",
        f"⏳ **Spotify authorization expires in ~{days_left} days**\n"
        "Re-authorize before it lapses to avoid downtime on vedsite.com.\n"
        "_Muted for 7 days._",
        604800,
    )
