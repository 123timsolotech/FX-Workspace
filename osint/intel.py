"""Analysis over what the world monitor already collects - no new feeds, nothing fetched.

The ideas come from Project Velocity (github.com/AndrewCTF/velocity, AGPL-3.0): its GPS-jamming layer, its watch
officer and its whats_changed tool. Only the ideas - this code is written here, against this app's own store.

  jamming()   GPS interference, the gpsjam.org way: an airborne aircraft whose ADS-B position is reported with
              poor accuracy (NACp < 8, or NIC < 7) is "degraded". Over the last 30 minutes, per half-degree cell,
              the share of degraded aircraft: 2-10% medium, over 10% high. Only aircraft the monitor sees (near
              your areas, and military worldwide) - a cell with few aircraft says little, so fewer than 3 is left out.
  scan()      the watch officer: emergency squawks (7500 hijack, 7600 radio failure, 7700 emergency), high jamming
              cells, and strong earthquakes (M6+ anywhere, M4.5+ near a watched area) become alerts - events of the
              layer "alerts", one per thing per hour, so nothing is repeated.
  changes()   what changed in an area: aircraft now against an hour (or N) ago, new events, alerts, jamming.
"""
from __future__ import annotations

import collections
import threading
import time

from . import geo, store

CELL = 0.5                     # degrees
WINDOW_S = 1800                # the jamming picture covers the last 30 minutes
MIN_AIRCRAFT = 3
SQUAWKS = {"7500": ("hijack", "high"), "7600": ("radio failure", "low"), "7700": ("emergency", "medium")}
EMERGENCY = {"general": "medium", "lifeguard": "medium", "minfuel": "low", "nordo": "low", "unlawful": "high", "downed": "high"}

_lock = threading.Lock()
_seen: collections.deque = collections.deque()          # (t, id, cell, degraded) samples for jamming()


def _cell(lat: float, lon: float) -> tuple:
    return (round((lat // CELL) * CELL + CELL / 2, 3), round((lon // CELL) * CELL + CELL / 2, 3))


def degraded(props: dict) -> bool | None:
    """True when the aircraft reports poor position accuracy; None when it reports neither value."""
    nacp, nic = props.get("nac_p"), props.get("nic")
    if nacp is None and nic is None:
        return None
    return (isinstance(nacp, (int, float)) and nacp < 8) or (isinstance(nic, (int, float)) and nic < 7)


def sample(now: float | None = None) -> int:
    """Add the current airborne aircraft to the jamming picture (the collector does this every minute)."""
    now = now or time.time()
    rows = [r for r in store.current(["flights", "military"]) if not r["props"].get("ground") and now - r["t"] < 120]
    with _lock:
        for r in rows:
            bad = degraded(r["props"])
            if bad is not None:
                _seen.append((now, r["id"], _cell(r["lat"], r["lon"]), bad))
        while _seen and _seen[0][0] < now - WINDOW_S:
            _seen.popleft()
    return len(rows)


def jamming(bounds: tuple | None = None, now: float | None = None) -> list:
    """[{lat, lon, n, bad, pct, level}] for cells with enough aircraft and some degradation, worst first."""
    now = now or time.time()
    latest: dict = {}
    with _lock:
        for t, id_, cell, bad in _seen:
            if t >= now - WINDOW_S:
                latest[(cell, id_)] = bad                      # each aircraft counted once per cell
    cells: dict = collections.defaultdict(lambda: [0, 0])
    for (cell, _id), bad in latest.items():
        cells[cell][0] += 1
        cells[cell][1] += bool(bad)
    out = []
    for (lat, lon), (n, bad) in cells.items():
        if n < MIN_AIRCRAFT or not bad:
            continue
        if bounds and not (bounds[0] <= lat <= bounds[2] and (bounds[1] <= lon <= bounds[3] if bounds[1] <= bounds[3] else True)):
            continue
        pct = bad / n
        if pct < 0.02:
            continue
        out.append({"lat": lat, "lon": lon, "n": n, "bad": bad, "pct": round(pct, 3), "level": "high" if pct > 0.10 else "medium"})
    return sorted(out, key=lambda c: (-c["pct"], -c["n"]))


def _alert(key: str, t: float, lat, lon, kind: str, severity: str, title: str, url: str = "", **props) -> dict:
    hour = int(t // 3600)                                   # one alert per thing per hour
    return {"uid": f"alert:{key}:{hour}", "layer": "alerts", "t": t, "lat": lat, "lon": lon, "kind": kind,
            "title": title[:280], "url": url, "source": "computed here",
            "props": {"severity": severity, "sev": {"low": 1, "medium": 2, "high": 3}[severity], **props}}


def scan(areas: list, now: float | None = None) -> list:
    """The watch officer's pass: new alerts (also kept as events of the layer "alerts"). Returns the new ones."""
    now = now or time.time()
    alerts = []
    for r in store.current(["flights", "military"]):
        if now - r["t"] > 300:
            continue
        p, name = r["props"], r["label"] or r["id"]
        code = str(p.get("squawk") or "")
        what = None
        if code in SQUAWKS:
            what, sev = f"squawking {code} ({SQUAWKS[code][0]})", SQUAWKS[code][1]
        elif str(p.get("emergency") or "") in EMERGENCY:
            what, sev = f"declaring an emergency ({p['emergency']})", EMERGENCY[p["emergency"]]
        if what:
            alerts.append(_alert(f"sq:{r['id']}:{code or p.get('emergency')}", now, r["lat"], r["lon"], "aircraft emergency", sev,
                                 f"{name} {what} - {geo.where(r['lat'], r['lon'])}", f"https://globe.adsb.lol/?icao={r['id']}",
                                 id=r["id"], layer=r["layer"]))
    for c in jamming(now=now):
        if c["level"] == "high" and c["bad"] >= 3:
            alerts.append(_alert(f"jam:{c['lat']}:{c['lon']}", now, c["lat"], c["lon"], "gps jamming", "medium",
                                 f"GPS interference near {geo.where(c['lat'], c['lon'])}: {c['bad']} of {c['n']} aircraft report "
                                 f"degraded positions ({round(c['pct'] * 100)}%, last 30 min)", pct=c["pct"], n=c["n"]))
    for q in store.events("quakes", now - 2 * 3600, limit=500):
        mag = q["props"].get("mag") or 0
        near = q["lat"] is not None and any(geo.km(q["lat"], q["lon"], float(a["lat"]), float(a["lon"])) < 500 for a in areas)
        if mag >= 6 or (near and mag >= 4.5):
            alerts.append(_alert(f"quake:{q['uid']}", q["t"], q["lat"], q["lon"], "earthquake", "high" if mag >= 6.5 else "medium",
                                 q["title"], q["url"], mag=mag))
    if not alerts:
        return []
    known = {e["uid"] for e in store.events("alerts", now - 2 * 3600, limit=2000)}
    new = [a for a in alerts if a["uid"] not in known]
    store.put_events(new)
    return new


def changes(p: dict, hours: float = 1.0, now: float | None = None) -> dict:
    """What changed within the place's radius since `hours` ago."""
    now = now or time.time()
    radius = max(float(p.get("radius_km") or 300), 150.0)
    b = geo.bbox(p["lat"], p["lon"], radius)
    then = now - hours * 3600
    out = {"place": p["name"], "radius_km": round(radius), "hours": hours, "aircraft": {}}
    for layer in ("military", "flights"):
        out["aircraft"][layer] = {"now": store.count_in(layer, now, b), "before": store.count_in(layer, then, b)}
    out["events"] = {layer: len(store.events(layer, then, b, limit=1000)) for layer in ("quakes", "news")}
    out["alerts"] = store.events("alerts", then, b, limit=20)
    out["jamming"] = jamming(b, now)
    return out
