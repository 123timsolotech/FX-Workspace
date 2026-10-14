"""The world monitor's feeds - each read straight from its origin, no key, through the app's network route
(netlayer, component "other"). Each fetcher returns plain rows for osint.store; the collector decides when.

  flights         adsb.lol (community ADS-B receivers, ODbL) around each area of interest - every ~30 s
  flights_global  OpenSky Network, anonymous (400 credits a day; a world snapshot costs 4) - every 20 min
  military        adsb.lol /v2/mil: every aircraft flagged military, worldwide - every ~45 s
  quakes          USGS: every earthquake of the past day - every 2 min
  space           NOAA SWPC: planetary K index, space-weather alerts, the aurora oval - every 15 min
  news            GDELT 2.0 events (geolocated, every 15 min): conflict, force, protest and coercion only
  satellites      CelesTrak orbit data (TLE) - positions are computed here (satellites.py); every 6 h

Tested 2026-09-29: airplanes.live now refuses unregistered use (403), so it isn't used.
"""
from __future__ import annotations

import calendar
import csv
import io
import os
import time
import zipfile

import netlayer

UA = "AgenticWorkspace/1.0 (personal local world monitor)"
ADSB_LOL = "https://api.adsb.lol/v2"
OPENSKY = "https://opensky-network.org/api/states/all"
USGS_DAY = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_day.geojson"
SWPC_KP = "https://services.swpc.noaa.gov/products/noaa-planetary-k-index.json"
SWPC_ALERTS = "https://services.swpc.noaa.gov/products/alerts.json"
SWPC_OVATION = "https://services.swpc.noaa.gov/json/ovation_aurora_latest.json"
GDELT_LAST = "http://data.gdeltproject.org/gdeltv2/lastupdate.txt"
CELESTRAK = "https://celestrak.org/NORAD/elements/gp.php?GROUP={group}&FORMAT=tle"
SAT_GROUPS = ("stations", "visual", "gnss", "weather", "military", "starlink")


class FeedError(Exception):
    pass


def _get(url: str, timeout: float = 25.0, as_json: bool = True):
    try:
        with netlayer.client("other", timeout=timeout, follow_redirects=True, private=False,
                             headers={"User-Agent": UA, "Accept": "application/json, text/plain, */*"}) as c:
            r = c.get(url)
    except netlayer.RouteBlocked:
        raise
    except Exception as e:
        raise FeedError(f"{type(e).__name__}: {str(e)[:120]}")
    if r.status_code != 200:
        raise FeedError(f"HTTP {r.status_code}")
    if not as_json:
        return r.content
    try:
        return r.json()
    except ValueError:
        raise FeedError("not JSON")


# ---- aircraft -----------------------------------------------------------------------------------
def _aircraft(ac: dict, now: float, source: str) -> dict | None:
    if ac.get("lat") is None or ac.get("lon") is None:
        return None
    alt = ac.get("alt_baro")
    ground = alt == "ground"
    props = {k: ac[k] for k in ("r", "t", "squawk", "category", "nic", "nac_p", "emergency") if ac.get(k) not in (None, "", "none")}
    if props.get("r"):
        props["reg"] = props.pop("r")
    if props.get("t"):
        props["type"] = props.pop("t")
    if ac.get("dbFlags", 0) & 1:
        props["mil"] = 1
    if ground:
        props["ground"] = 1
    props["src"] = source
    return {"id": str(ac.get("hex") or "").lower(), "t": now - float(ac.get("seen_pos") or 0),
            "lat": round(float(ac["lat"]), 5), "lon": round(float(ac["lon"]), 5),
            "alt": 0 if ground else (round(float(alt) * 0.3048) if isinstance(alt, (int, float)) else None),   # feet -> m
            "hdg": ac.get("track") if ac.get("track") is not None else ac.get("true_heading"),
            "spd": round(float(ac["gs"]) * 1.852) if isinstance(ac.get("gs"), (int, float)) else None,          # knots -> km/h
            "label": str(ac.get("flight") or "").strip() or props.get("reg") or "", "props": props}


def area_flights(lat: float, lon: float, radius_nm: int = 250) -> list:
    """Aircraft within radius_nm (at most 250) of a point - adsb.lol."""
    now = time.time()
    d = _get(f"{ADSB_LOL}/lat/{lat:.3f}/lon/{lon:.3f}/dist/{min(250, max(10, int(radius_nm)))}")
    return [a for a in (_aircraft(ac, now, "adsb.lol") for ac in d.get("ac") or []) if a and a["id"]]


def military() -> list:
    now = time.time()
    d = _get(f"{ADSB_LOL}/mil")
    return [a for a in (_aircraft(ac, now, "adsb.lol") for ac in d.get("ac") or []) if a and a["id"]]


def opensky_global() -> list:
    """A world snapshot from OpenSky (anonymous: most recent state vectors only)."""
    d = _get(OPENSKY, timeout=40)
    out = []
    for s in d.get("states") or []:
        if len(s) < 11 or s[5] is None or s[6] is None:
            continue
        alt = s[13] if s[13] is not None else s[7]
        out.append({"id": str(s[0]).lower(), "t": float(s[3] or s[4] or d.get("time") or time.time()),
                    "lat": round(s[6], 5), "lon": round(s[5], 5), "alt": 0 if s[8] else (round(alt) if alt is not None else None),
                    "hdg": s[10], "spd": round(s[9] * 3.6) if s[9] is not None else None,
                    "label": str(s[1] or "").strip(), "props": {"country": s[2], "src": "OpenSky", **({"ground": 1} if s[8] else {})}})
    return out


# ---- earthquakes, space weather ------------------------------------------------------------------
def quakes() -> list:
    d = _get(USGS_DAY)
    out = []
    for f in d.get("features") or []:
        p, g = f.get("properties") or {}, (f.get("geometry") or {}).get("coordinates") or [None, None, None]
        if g[0] is None or p.get("mag") is None:
            continue
        out.append({"uid": f"usgs:{f.get('id')}", "layer": "quakes", "t": (p.get("time") or 0) / 1000, "lat": g[1], "lon": g[0],
                    "kind": "earthquake", "title": f"M{p['mag']:.1f} - {p.get('place') or 'unknown place'}",
                    "url": p.get("url") or "", "source": "USGS",
                    "props": {"mag": p["mag"], "depth_km": g[2], "tsunami": p.get("tsunami"), "alert": p.get("alert"),
                              "felt": p.get("felt"), "status": p.get("status")}})
    return out


def space_weather() -> tuple:
    """(kp: {"kp", "time"}, alert events, aurora points [[lon, lat, %], ...] where the oval is 10% or more)."""
    kp_rows = _get(SWPC_KP)
    last = kp_rows[-1] if kp_rows else {}
    kp = {"kp": last.get("Kp"), "time": last.get("time_tag")}
    alerts = []
    for a in _get(SWPC_ALERTS)[:40]:
        msg = str(a.get("message") or "")
        head = next((ln.strip() for ln in msg.splitlines() if ln.strip().upper().startswith(("WARNING", "ALERT", "WATCH", "SUMMARY", "EXTENDED"))), "")
        try:
            t = calendar.timegm(time.strptime(a["issue_datetime"][:19], "%Y-%m-%d %H:%M:%S"))
        except (KeyError, ValueError):
            continue
        alerts.append({"uid": f"swpc:{a.get('product_id')}:{a['issue_datetime']}", "layer": "space", "t": t, "lat": None, "lon": None,
                       "kind": "space weather", "title": head or msg.splitlines()[0][:120], "url": "https://www.swpc.noaa.gov/",
                       "source": "NOAA SWPC", "props": {"code": a.get("product_id")}})
    ov = _get(SWPC_OVATION)
    aurora = [[p[0] if p[0] <= 180 else p[0] - 360, p[1], p[2]] for p in ov.get("coordinates") or [] if len(p) == 3 and p[2] >= 10]
    return kp, alerts, aurora


# ---- GDELT events ---------------------------------------------------------------------------------
# CAMEO root codes kept: the conflict end of GDELT (verbal and material conflict).
CAMEO = {"13": "threat", "14": "protest", "15": "show of force", "16": "sanctions / cutting ties", "17": "coercion",
         "18": "assault", "19": "fighting", "20": "mass violence"}
_G = {"id": 0, "day": 1, "actor1": 6, "actor2": 16, "code": 26, "root": 28, "quad": 29, "goldstein": 30, "mentions": 31,
      "sources": 32, "tone": 34, "geo_name": 52, "geo_cc": 53, "lat": 56, "lon": 57, "added": 59, "url": 60}


def _actor(name: str) -> str:
    """GDELT's upper-case actor names, readable: "UNITED STATES" -> "United States", "THE US" -> "The US"."""
    return " ".join(w if len(w) <= 3 and w not in ("THE", "AND", "FOR", "OF") else w.capitalize() for w in name.split())


def gdelt_events() -> list:
    """The latest 15-minute GDELT export: conflict events with a place (lat/lon) and a source article."""
    line = _get(GDELT_LAST, as_json=False).decode("utf-8", "replace").splitlines()[0]
    url = line.split()[-1]
    raw = _get(url, timeout=60, as_json=False)
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            text = z.read(z.namelist()[0]).decode("utf-8", "replace")
    except zipfile.BadZipFile:
        raise FeedError("GDELT export wasn't a zip")
    out = []
    for c in csv.reader(io.StringIO(text), delimiter="\t"):
        if len(c) < 61 or c[_G["root"]] not in CAMEO or not c[_G["lat"]] or not c[_G["lon"]]:
            continue
        try:
            t = calendar.timegm(time.strptime(c[_G["added"]], "%Y%m%d%H%M%S"))
            lat, lon = float(c[_G["lat"]]), float(c[_G["lon"]])
        except ValueError:
            continue
        a1, a2 = _actor(c[_G["actor1"]]), _actor(c[_G["actor2"]])
        what = CAMEO[c[_G["root"]]]
        who = f"{a1} -> {a2}" if a1 and a2 else a1 or a2 or "Unnamed actors"
        out.append({"uid": f"gdelt:{c[_G['id']]}", "layer": "news", "t": t, "lat": lat, "lon": lon, "kind": what,
                    "title": f"{what.capitalize()}: {who} - {c[_G['geo_name']]}", "url": c[_G["url"]],
                    "source": "GDELT", "props": {"cameo": c[_G["code"]], "goldstein": float(c[_G["goldstein"]] or 0),
                                                 "mentions": int(c[_G["mentions"]] or 0), "sources": int(c[_G["sources"]] or 0),
                                                 "country": c[_G["geo_cc"]], "quad": c[_G["quad"]]}})
    return out


# ---- satellite orbits -----------------------------------------------------------------------------
class NotUpdated(FeedError):
    """CelesTrak: nothing new since this PC's last download (it updates every 2 hours and refuses repeats)."""


def tle_group(group: str) -> list:
    """[(name, line1, line2)] for a CelesTrak group."""
    try:
        text = _get(CELESTRAK.format(group=group), timeout=150 if group == "starlink" else 60, as_json=False).decode("utf-8", "replace")
    except FeedError as e:
        if "403" in str(e):
            raise NotUpdated(f"{group}: CelesTrak has nothing newer than the last download")
        raise
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    out = []
    for i in range(0, len(lines) - 2, 3):
        name, l1, l2 = lines[i:i + 3]
        if l1.startswith("1 ") and l2.startswith("2 "):
            out.append((name.strip(), l1, l2))
    if not out:
        raise FeedError(f"CelesTrak sent no orbits for {group}")
    return out


def load_tles(path: str) -> dict:
    """{group: [(name, line1, line2)]} from the saved orbit file ({} when there is none)."""
    out: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                parts = line.rstrip("\n").split("\t")
                if len(parts) == 4:
                    out.setdefault(parts[0], []).append((parts[1], parts[2], parts[3]))
    except OSError:
        pass
    return out


def save_tles(path: str, groups: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for group, rows in groups.items():
            for name, l1, l2 in rows:
                f.write(f"{group}\t{name}\t{l1}\t{l2}\n")
    os.replace(tmp, path)
