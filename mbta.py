# mbta.py — next trains for the HA kiosk MBTA sign.
# /api/mbta            auto: MFA (Green-E), falls back to Fenway (Green-D) if E is disrupted
# /api/mbta?station=X  one station: mfa | fenway | ruggles
import os
import time
from datetime import datetime, timezone

import requests
from flask import Blueprint, jsonify, request

mbta_bp = Blueprint("mbta", __name__)

API = "https://api-v3.mbta.com"
KEY = os.environ.get("MBTA_API_KEY", "")
STATIONS = {
    "mfa": {"stop": "place-mfa", "route": "Green-E", "name": "MFA", "line": "Green Line E"},
    "northeastern": {"stop": "place-nuniv", "route": "Green-E", "name": "NEU", "line": "Green Line E"},
    "fenway": {"stop": "place-fenwy", "route": "Green-D", "name": "Fenway", "line": "Green Line D"},
    "ruggles": {"stop": "place-rugg", "route": "Orange", "name": "Ruggles", "line": "Orange Line"},
}
DOWN_EFFECTS = {"SUSPENSION", "SHUTTLE", "STATION_CLOSURE", "NO_SERVICE"}
PER_DIR = 2      # trains per direction
CACHE_TTL = 20   # seconds

_cache = {}
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


def _alerts(*routes):
    return _get("/alerts", **{"filter[route]": ",".join(routes), "filter[datetime]": "NOW"})


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


def _result(mode, key, cfg=None, rows=None, alert=None, label=None):
    return {"mode": mode, "station": key,
            "stop": cfg["name"] if cfg else None,
            "label": label or (cfg["line"] if cfg else None),
            "rows": rows or [], "alert": alert}


def _build_auto():
    mfa, fen = STATIONS["mfa"], STATIONS["fenway"]
    alerts = _alerts(mfa["route"], fen["route"])
    e_alert = _alert_for(alerts, mfa["route"], mfa["stop"])
    if not e_alert:
        rows = _upcoming(mfa)
        if any(r["mins"] for r in rows):
            return _result("primary", "auto", mfa, rows)

    d_alert = _alert_for(alerts, fen["route"], fen["stop"])
    if not d_alert:
        rows = _upcoming(fen)
        if any(r["mins"] for r in rows):
            return _result("fallback", "auto", fen, rows, e_alert or "No E trains predicted")

    if e_alert or d_alert:
        return _result("down", "auto", alert=e_alert or d_alert, label="Green Line")
    return _result("none", "auto", alert="No trains predicted", label="Green Line E")


def _build_station(key):
    cfg = STATIONS[key]
    alert = _alert_for(_alerts(cfg["route"]), cfg["route"], cfg["stop"])
    if alert:
        return _result("down", key, cfg, alert=alert)
    rows = _upcoming(cfg)
    if any(r["mins"] for r in rows):
        return _result("station", key, cfg, rows)
    return _result("none", key, cfg, alert="No trains predicted")


@mbta_bp.route("/api/mbta")
def mbta():
    key = request.args.get("station", "auto").lower()
    if key != "auto" and key not in STATIONS:
        return jsonify({"error": f"unknown station '{key}'"}), 400
    c = _cache.setdefault(key, {"t": 0, "data": None})
    if c["data"] is None or time.time() - c["t"] > CACHE_TTL:
        try:
            c["data"] = _build_auto() if key == "auto" else _build_station(key)
            c["t"] = time.time()
        except Exception as exc:
            if c["data"] is None:
                return jsonify({"mode": "error", "rows": [], "alert": str(exc)}), 502
    return jsonify(c["data"])
