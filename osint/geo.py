"""Places and distances for the world monitor: "near Haifa", "in the Red Sea", "32.8,35.0".

Cities come from geo/cities.json (GeoNames cities15000, CC BY 4.0 - built by geo/build_geo.py), with
their Hebrew, Russian and Arabic names; regions are a short list of areas the monitor is asked about.
"""
from __future__ import annotations

import json
import math
import os
import re
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CITIES_PATH = os.path.join(ROOT, "geo", "cities.json")
WORLD_PATH = os.path.join(ROOT, "geo", "world.json")

# name: (lat, lon, radius km). Hebrew and Russian names point at the same entries (ALIASES).
REGIONS = {
    "israel": (31.4, 35.0, 250), "gaza": (31.42, 34.38, 35), "west bank": (31.95, 35.25, 80),
    "lebanon": (33.85, 35.85, 140), "syria": (35.0, 38.5, 450), "jordan": (31.2, 36.5, 250),
    "egypt": (27.0, 30.5, 700), "sinai": (29.5, 33.8, 200), "iraq": (33.2, 43.7, 550), "iran": (32.4, 53.7, 1000),
    "yemen": (15.6, 47.6, 600), "saudi arabia": (24.0, 45.0, 1100), "turkey": (39.0, 35.2, 800),
    "cyprus": (35.1, 33.3, 120), "levant": (33.5, 36.2, 550), "gulf": (26.5, 52.0, 850),
    "persian gulf": (26.5, 52.0, 850), "hormuz": (26.6, 56.3, 200), "strait of hormuz": (26.6, 56.3, 200),
    "red sea": (20.0, 38.8, 1100), "bab el-mandeb": (12.6, 43.4, 150), "suez": (30.4, 32.4, 150),
    "mesopotamia": (33.3, 44.0, 650), "middle east": (29.0, 45.0, 2600), "eastern mediterranean": (34.0, 32.0, 500),
    "mediterranean": (37.5, 17.0, 2000), "europe": (50.0, 12.0, 2600), "ukraine": (48.6, 31.4, 750),
    "russia": (58.0, 60.0, 3500), "black sea": (43.3, 34.3, 600), "baltic": (58.0, 20.0, 600),
    "taiwan strait": (24.3, 119.6, 250), "south china sea": (13.0, 114.0, 1300), "korea": (37.0, 127.5, 450),
    "united states": (39.5, -98.4, 2500), "usa": (39.5, -98.4, 2500),
}
ALIASES = {
    "ישראל": "israel", "עזה": "gaza", "רצועת עזה": "gaza", "יהודה ושומרון": "west bank", "לבנון": "lebanon",
    "סוריה": "syria", "ירדן": "jordan", "מצרים": "egypt", "סיני": "sinai", "עיראק": "iraq", "איראן": "iran",
    "תימן": "yemen", "ערב הסעודית": "saudi arabia", "טורקיה": "turkey", "קפריסין": "cyprus",
    "המפרץ": "gulf", "המפרץ הפרסי": "gulf", "מצר הורמוז": "hormuz", "הים האדום": "red sea", "המזרח התיכון": "middle east",
    "הים התיכון": "mediterranean", "אוקראינה": "ukraine", "רוסיה": "russia", "אירופה": "europe",
    "израиль": "israel", "газа": "gaza", "ливан": "lebanon", "сирия": "syria", "иран": "iran", "ирак": "iraq",
    "йемен": "yemen", "красное море": "red sea", "ближний восток": "middle east", "украина": "ukraine",
    "россия": "russia", "европа": "europe", "персидский залив": "gulf", "ормузский пролив": "hormuz",
}
_LATLON = re.compile(r"^\s*(-?\d{1,2}(?:\.\d+)?)\s*[, ]\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
_lock = threading.Lock()
_cities: list | None = None
_index: dict | None = None


def _norm(s: str) -> str:
    return re.sub(r"[\s'\"`׳״.\-]+", " ", str(s or "").strip().lower()).strip()


def cities() -> list:
    """[[name, lat, lon, country, population, [names]]], most populous first."""
    global _cities, _index
    with _lock:
        if _cities is None:
            try:
                with open(CITIES_PATH, encoding="utf-8") as f:
                    _cities = json.load(f)["rows"]
            except (OSError, ValueError, KeyError):
                _cities = []
            _index = {}
            for row in _cities:                       # most populous first: the first entry for a name wins
                for n in [row[0], *row[5]]:
                    _index.setdefault(_norm(n), row)
        return _cities


def place(query) -> dict | None:
    """{name, lat, lon, radius_km, kind} for a region, a city (any of its names) or "lat,lon"; None if unknown."""
    q = str(query or "").strip()
    if not q:
        return None
    m = _LATLON.match(q)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return {"name": f"{lat:.3f},{lon:.3f}", "lat": lat, "lon": lon, "radius_km": 100, "kind": "point"}
    key = _norm(q)
    key = re.sub(r"^(?:the|in|near|around|over)\s+", "", key)
    region = ALIASES.get(key, key)
    if region in REGIONS:
        lat, lon, r = REGIONS[region]
        return {"name": region.title() if region.isascii() else region, "lat": lat, "lon": lon, "radius_km": r, "kind": "region"}
    cities()
    row = (_index or {}).get(key)
    if row is None and key.startswith(("ה", "ב")) and len(key) > 3:          # Hebrew prefixes: "בחיפה"
        row = (_index or {}).get(key[1:])
    if row is None:
        return None
    return {"name": row[0], "lat": row[1], "lon": row[2], "radius_km": 60 if row[4] < 1_000_000 else 100,
            "kind": "city", "country": row[3]}


_WORDS = re.compile(r"[^\W\d_][\w'׳\-]*", re.U)


def places_in(text: str, most: int = 4) -> list:
    """The regions and cities a sentence names, in the order it names them: [(start, place)]. Cities need
    100,000 people (50,000 in Hebrew, Russian or Arabic) and, in Latin letters, a capital - so "nice weather"
    and "reading" aren't places."""
    cities()
    words = [(m.start(), m.group(0)) for m in _WORDS.finditer(str(text or ""))]
    out, seen, i = [], set(), 0
    while i < len(words) and len(out) < most:
        hit = None
        for n in (3, 2, 1):                                   # the longest name first: "Tel Aviv" before "Tel"
            chunk = words[i:i + n]
            if len(chunk) < n:
                continue
            key = _norm(" ".join(w for _s, w in chunk))
            key = ALIASES.get(key, key)
            if key in REGIONS:
                lat, lon, r = REGIONS[key]
                hit = (n, {"name": key.title(), "lat": lat, "lon": lon, "radius_km": r, "kind": "region"})
                break
            row = (_index or {}).get(key)
            for cut in (1, 2):                                    # Hebrew prefixes: "בחיפה", "ובירושלים"
                if row is None and n == 1 and len(key) > cut + 2 and all(c in "והבלמש" for c in key[:cut]):
                    row = (_index or {}).get(key[cut:])
            if row is None and n == 1 and len(key) > 4 and "а" <= key[-1] <= "я":    # Russian case endings: "над Москвой"
                for stem in (key[:-2] + "а", key[:-1] + "а", key[:-2], key[:-1]):
                    row = row or (_index or {}).get(stem)
            first = chunk[0][1]
            latin = first[:1].isascii()
            # A capital that only starts the sentence ("Nice weather") proves nothing: then only a big city counts.
            opens = not str(text)[:chunk[0][0]].rstrip() or str(text)[:chunk[0][0]].rstrip()[-1:] in ".!?:\n"
            need = (1_000_000 if opens else 100_000) if latin else 50_000
            if row and len(key) >= 3 and row[4] >= need and (not latin or first[:1].isupper()):
                hit = (n, {"name": row[0], "lat": row[1], "lon": row[2], "radius_km": 60 if row[4] < 1_000_000 else 100, "kind": "city"})
                break
        if hit and hit[1]["name"] not in seen:
            seen.add(hit[1]["name"])
            out.append((words[i][0], hit[1]))
        i += hit[0] if hit else 1
    return out


def km(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in km."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(a)))


def bbox(lat, lon, radius_km) -> tuple:
    """(south, west, north, east) around a point - a quick filter before the exact distance."""
    dlat = radius_km / 111.0
    dlon = radius_km / max(1e-6, 111.0 * math.cos(math.radians(max(-89.0, min(89.0, lat)))))
    return lat - dlat, lon - dlon, lat + dlat, lon + dlon


def nearest_city(lat, lon, min_population: int = 50_000, max_km: float = 400) -> tuple | None:
    """(name, km) of the nearest sizeable city - for saying where something is."""
    best = None
    for row in cities():
        if row[4] < min_population:
            break
        if abs(row[1] - lat) > 5 or abs(row[2] - lon) > 7:
            continue
        d = km(lat, lon, row[1], row[2])
        if d <= max_km and (best is None or d < best[1]):
            best = (row[0], d)
    return best


def where(lat, lon) -> str:
    """"12 km from Haifa", "at Haifa", or the coordinates when no sizeable city is within 400 km."""
    near = nearest_city(lat, lon)
    if not near:
        return f"{lat:.2f}, {lon:.2f} (no city within 400 km)"
    return f"{near[1]:.0f} km from {near[0]}" if near[1] >= 5 else f"at {near[0]}"
