# mbta.py — next Green Line trains for the HA weather card.
# MFA (Green-E) first; falls back to Fenway (Green-D) if E is disrupted.
import os
import time
from datetime import datetime, timezone

import requests
from flask import Blueprint, jsonify

mbta_bp = Blueprint("mbta", __name__)

API = "https://api-v3.mbta.com"
KEY = os.environ.get("MBTA_API_KEY", "")
PRIMARY = {"stop": "place-mfa", "route": "Green-E", "name": "MFA", "line": "E"}
FALLBACK = {"stop": "place-fenwy", "route": "Green-D", "name": "Fenway", "line": "D"}
DOWN_EFFECTS = {"SUSPENSION", "SHUTTLE", "STATION_CLOSURE", "NO_SERVICE"}
PER_DIR = 2      # trains shown per direction
CACHE_TTL = 20   # seconds

_cache = {"t": 0, "data": None}
_dests = {}


def _get(path, **params):
    headers = {"x-api-key": KEY} if KEY else {}
    r = requests.get(f"{API}{path}", params=params, headers=headers, timeout=5)
    r.raise_for_status()
    return r.json()["data"]


def _destinations(route):
    if route not in _dests:
        _dests[route] = _get(f"/routes/{route}")["attributes"]["direction_destinations"]
    return _dests[route]


def _alert_for(alerts, route, stop):
    """Header of an active service-killing alert covering this route at this stop."""
    for a in alerts:
        attrs = a["attributes"]
        if attrs["effect"] not in DOWN_EFFECTS:
            continue
        for e in attrs["informed_entity"]:
            if e.get("route") == route and e.get("stop") in (None, stop):
                return attrs.get("short_header") or attrs["header"]
    return None


def _upcoming(cfg):
    now = datetime.now(timezone.utc)
    preds = _get("/predictions", **{
        "filter[stop]": cfg["stop"],
        "filter[route]": cfg["route"],
        "sort": "departure_time",
    })
    trains = {0: [], 1: []}
    for p in preds:
        a = p["attributes"]
        if a.get("schedule_relationship") in ("CANCELLED", "SKIPPED"):
            continue
        t = a.get("departure_time") or a.get("arrival_time")
        if not t:
            continue
        at = datetime.fromisoformat(t)  # MBTA sends local ET offset
        m = int((at - now).total_seconds() // 60)
        d = a["direction_id"]
        if m >= 0 and len(trains[d]) < PER_DIR:
            trains[d].append((m, at.strftime("%-I:%M")))
    dests = _destinations(cfg["route"])
    return [{"dest": dests[d],
             "mins": [m for m, _ in trains[d]],
             "times": [s for _, s in trains[d]]} for d in (0, 1)]


def _build():
    alerts = _get("/alerts", **{
        "filter[route]": "Green-D,Green-E",
        "filter[datetime]": "NOW",
    })
    e_alert = _alert_for(alerts, PRIMARY["route"], PRIMARY["stop"])
    if not e_alert:
        rows = _upcoming(PRIMARY)
        if any(r["mins"] for r in rows):
            return {"mode": "primary", "stop": PRIMARY["name"], "line": PRIMARY["line"],
                    "rows": rows, "alert": None}

    d_alert = _alert_for(alerts, FALLBACK["route"], FALLBACK["stop"])
    if not d_alert:
        rows = _upcoming(FALLBACK)
        if any(r["mins"] for r in rows):
            return {"mode": "fallback", "stop": FALLBACK["name"], "line": FALLBACK["line"],
                    "rows": rows, "alert": e_alert or "No E trains predicted"}

    if e_alert or d_alert:
        return {"mode": "down", "stop": None, "line": None, "rows": [],
                "alert": e_alert or d_alert}
    return {"mode": "none", "stop": None, "line": None, "rows": [],
            "alert": "No trains predicted"}


@mbta_bp.route("/api/mbta")
def mbta():
    if _cache["data"] is None or time.time() - _cache["t"] > CACHE_TTL:
        try:
            _cache["data"] = _build()
            _cache["t"] = time.time()
        except Exception as exc:
            if _cache["data"] is None:
                return jsonify({"mode": "error", "rows": [], "alert": str(exc)}), 502
    return jsonify(_cache["data"])
