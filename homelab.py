"""
Homelab panel data for the Home Assistant kiosk.

Reads the Beszel hub's PocketBase API over the tailnet and flattens the latest
1-minute sample into a single payload. Agents report roughly once a minute, so
responses are cached briefly and served stale on upstream failure rather than
blanking the panel.
"""

import os
import threading
import time

import requests
from flask import Blueprint, jsonify

homelab_bp = Blueprint("homelab", __name__)

BESZEL_URL = os.environ.get("BESZEL_URL", "").rstrip("/")
BESZEL_EMAIL = os.environ.get("BESZEL_EMAIL", "")
BESZEL_PASSWORD = os.environ.get("BESZEL_PASSWORD", "")
BESZEL_SYSTEM = os.environ.get("BESZEL_SYSTEM", "veds-pi5")

CACHE_TTL = 20

_auth = {"token": None, "expires_at": 0}
_auth_lock = threading.Lock()
_cache = {"payload": None, "expires_at": 0}


def _authenticate():
    """PocketBase password auth against the users collection (not _superusers)."""
    resp = requests.post(
        f"{BESZEL_URL}/api/collections/users/auth-with-password",
        json={"identity": BESZEL_EMAIL, "password": BESZEL_PASSWORD},
        timeout=5,
    )
    resp.raise_for_status()
    token = resp.json().get("token")
    if not token:
        raise RuntimeError("Beszel auth returned no token")
    _auth["token"] = token
    # PocketBase user tokens last far longer; re-auth hourly to stay well inside it.
    _auth["expires_at"] = time.time() + 3600
    return token


def _get_token(force=False):
    with _auth_lock:
        if force or not _auth["token"] or time.time() >= _auth["expires_at"]:
            return _authenticate()
        return _auth["token"]


def _pb_get(path, params=None):
    """GET with a single forced re-auth retry on 401."""
    for attempt in (0, 1):
        token = _get_token(force=bool(attempt))
        resp = requests.get(
            f"{BESZEL_URL}{path}",
            headers={"Authorization": token},
            params=params or {},
            timeout=5,
        )
        if resp.status_code == 401 and attempt == 0:
            continue
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("Beszel request failed after re-auth")


def _latest(collection, system_id):
    """Newest 1m record for a system. Coarser buckets (10m, 120m) are rollups."""
    data = _pb_get(
        f"/api/collections/{collection}/records",
        {
            "filter": f"system='{system_id}' && type='1m'",
            "sort": "-created",
            "perPage": 1,
        },
    )
    items = data.get("items") or []
    return items[0] if items else None


def _build_payload():
    systems = _pb_get("/api/collections/systems/records", {"perPage": 50})
    target = next(
        (s for s in systems.get("items") or [] if s.get("name") == BESZEL_SYSTEM),
        None,
    )
    if target is None:
        raise RuntimeError(f"System '{BESZEL_SYSTEM}' not visible to this user")

    system_id = target["id"]
    stats = (_latest("system_stats", system_id) or {}).get("stats", {})
    container_record = _latest("container_stats", system_id) or {}

    disks = [
        {
            "name": "root",
            "used_gb": stats.get("du", 0),
            "total_gb": stats.get("d", 0),
            "pct": stats.get("dp", 0),
        }
    ]
    for label, fs in (stats.get("efs") or {}).items():
        total = fs.get("d") or 0
        used = fs.get("du") or 0
        disks.append(
            {
                "name": label,
                "used_gb": used,
                "total_gb": total,
                "pct": round(used / total * 100, 2) if total else 0,
            }
        )

    containers = sorted(
        (
            {
                "name": c.get("n"),
                "cpu": c.get("c", 0),
                "mem_mb": c.get("m", 0),
            }
            for c in container_record.get("stats") or []
        ),
        key=lambda c: c["mem_mb"],
        reverse=True,
    )

    return {
        "name": target.get("name"),
        "status": target.get("status"),
        "updated": target.get("updated"),
        "uptime_s": (target.get("info") or {}).get("u", 0),
        "cpu_pct": stats.get("cpu", 0),
        "memory": {
            "used_gb": stats.get("mu", 0),
            "total_gb": stats.get("m", 0),
            "pct": stats.get("mp", 0),
        },
        "temps": stats.get("t") or {},
        "disks": disks,
        "containers": containers,
    }


@homelab_bp.route("/api/homelab")
def homelab():
    now = time.time()
    if _cache["payload"] and now < _cache["expires_at"]:
        return jsonify(_cache["payload"])

    try:
        payload = _build_payload()
    except Exception as exc:
        print(f"ERROR: homelab fetch failed: {exc}")
        if _cache["payload"]:
            stale = dict(_cache["payload"])
            stale["stale"] = True
            return jsonify(stale)
        return jsonify({"error": "hub unreachable"}), 503

    _cache["payload"] = payload
    _cache["expires_at"] = now + CACHE_TTL
    return jsonify(payload)
