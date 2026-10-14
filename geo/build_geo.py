"""
geo/build_geo.py
================
Turns the downloaded Natural Earth and GeoNames files (geo/raw) into the compact files the app
reads: geo/world.json (the globe: land, lakes, land borders) and geo/cities.json (places the AI
and the globe's search understand, with their Hebrew, Russian and Arabic names).

Run once after downloading:  .venv\\Scripts\\python.exe geo\\build_geo.py
Sources: Natural Earth 1:50m (public domain), GeoNames cities15000 (CC BY 4.0).
"""
from __future__ import annotations

import io
import json
import os
import re
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
RAW = os.path.join(HERE, "raw")
# One name in each script the app speaks besides English: Hebrew, Russian (Cyrillic), Arabic.
_SCRIPTS = (re.compile(r"[א-ת][֐-׿\s'\"־-]*"), re.compile(r"[А-Яа-яЁё][А-Яа-яЁё\s-]*"),
            re.compile(r"[ء-ي][؀-ۿ\s-]*"))


def _flat(coords, step: float = 0.0) -> list:
    """[lon, lat, lon, lat, ...] rounded to 0.01 deg (about 1 km), consecutive duplicates dropped."""
    out, last = [], None
    for lon, lat, *_ in coords:
        p = (round(lon, 2), round(lat, 2))
        if p != last:
            out += p
            last = p
    return out


def _rings(path: str) -> list:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    rings = []
    for feat in data["features"]:
        g = feat["geometry"]
        if not g:
            continue
        if g["type"] == "Polygon":
            polys = [g["coordinates"]]
        elif g["type"] == "MultiPolygon":
            polys = g["coordinates"]
        elif g["type"] == "LineString":
            polys = [[g["coordinates"]]]
        elif g["type"] == "MultiLineString":
            polys = [g["coordinates"]]
        else:
            continue
        for poly in polys:
            for ring in poly:
                flat = _flat(ring)
                if len(flat) >= 6:
                    rings.append(flat)
    return rings


def world() -> dict:
    return {"source": "Natural Earth 1:50m and 1:110m (public domain)",
            "land": _rings(os.path.join(RAW, "ne_50m_land.geojson")),
            "land_low": _rings(os.path.join(RAW, "ne_110m_land.geojson")),
            "lakes": _rings(os.path.join(RAW, "ne_50m_lakes.geojson")),
            "borders": _rings(os.path.join(RAW, "ne_50m_admin_0_boundary_lines_land.geojson"))}


def cities() -> dict:
    rows = []
    with zipfile.ZipFile(os.path.join(RAW, "cities15000.zip")) as z:
        text = io.TextIOWrapper(z.open("cities15000.txt"), encoding="utf-8")
        for line in text:
            c = line.rstrip("\n").split("\t")
            if len(c) < 15:
                continue
            names = c[3].split(",")
            # Every Hebrew, Russian and Arabic name it has (GeoNames lists them in no useful order, so all
            # are kept for matching; English stays the display name).
            alt = list(dict.fromkeys(a for a in names if any(s.fullmatch(a) for s in _SCRIPTS)))
            rows.append([c[1], round(float(c[4]), 4), round(float(c[5]), 4), c[8], int(c[14] or 0), alt])
    rows.sort(key=lambda r: -r[4])
    return {"source": "GeoNames cities15000 (CC BY 4.0, geonames.org)",
            "fields": ["name", "lat", "lon", "country", "population", "names"], "rows": rows}


if __name__ == "__main__":
    for name, data in (("world.json", world()), ("cities.json", cities())):
        path = os.path.join(HERE, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        print(name, round(os.path.getsize(path) / 1e6, 2), "MB")
