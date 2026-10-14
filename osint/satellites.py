"""Satellite positions and passes, computed on this PC from CelesTrak's orbit data (TLEs, downloaded by the
collector at most every 6 hours) with SGP4 (the sgp4 package). No service is asked where a satellite is.

Positions are TEME rotated to the Earth by Greenwich sidereal time (IAU 1982) - polar motion and the
equation of the equinoxes are left out, a few hundred metres at most: fine for a globe and for passes.
"""
from __future__ import annotations

import math
import os
import threading
import time
from datetime import datetime, timezone

import numpy as np
from sgp4.api import SatrecArray, Satrec, jday

from . import store

TLE_PATH = os.path.join(os.path.dirname(store.DB_PATH), "tle.txt")
EARTH_R = 6378.137
_lock = threading.Lock()
_cache: dict = {"mtime": None, "path": None}


def load() -> dict:
    """{"names", "groups", "ids", "array"} from the saved orbit file (reloaded when it changes)."""
    path = TLE_PATH
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {"names": [], "groups": [], "ids": [], "recs": [], "array": None, "age_h": None}
    with _lock:
        if _cache["mtime"] != mtime or _cache["path"] != path:
            names, groups, ids, recs, seen = [], [], [], [], set()
            with open(path, encoding="utf-8") as f:
                for line in f:
                    parts = line.rstrip("\n").split("\t")
                    if len(parts) != 4:
                        continue
                    group, name, l1, l2 = parts
                    norad = l1[2:7].strip()
                    if norad in seen:                       # a satellite in two groups is drawn once
                        continue
                    try:
                        recs.append(Satrec.twoline2rv(l1, l2))
                    except Exception:
                        continue
                    seen.add(norad)
                    names.append(name)
                    groups.append(group)
                    ids.append(norad)
            _cache.update(mtime=mtime, path=path, names=names, groups=groups, ids=ids, recs=recs,
                          array=SatrecArray(recs) if recs else None)
        return {**_cache, "age_h": (time.time() - mtime) / 3600}


def _jd(ts: np.ndarray) -> tuple:
    jd, fr = [], []
    for t in ts:
        d = datetime.fromtimestamp(float(t), tz=timezone.utc)
        a, b = jday(d.year, d.month, d.day, d.hour, d.minute, d.second + d.microsecond / 1e6)
        jd.append(a)
        fr.append(b)
    return np.array(jd), np.array(fr)


def _gmst(jd: np.ndarray, fr: np.ndarray) -> np.ndarray:
    t = ((jd - 2451545.0) + fr) / 36525.0
    sec = 67310.54841 + (876600.0 * 3600 + 8640184.812866) * t + 0.093104 * t * t - 6.2e-6 * t ** 3
    return np.radians((sec % 86400.0) / 240.0)


def _ecef(r: np.ndarray, g: np.ndarray) -> np.ndarray:
    """TEME (sats, times, 3) -> Earth-fixed, by GMST (times)."""
    c, s = np.cos(g)[None, :], np.sin(g)[None, :]
    x, y, z = r[..., 0], r[..., 1], r[..., 2]
    return np.stack([x * c + y * s, -x * s + y * c, z], axis=-1)


def positions(t: float | None = None, groups=None, query: str = "", limit: int = 20000) -> list:
    """[{id, name, group, lat, lon, alt_km}] at time t (now), for some groups or names containing query."""
    sat = load()
    if sat["array"] is None:
        return []
    jd, fr = _jd(np.array([t or time.time()]))
    err, r, _v = sat["array"].sgp4(jd, fr)
    p = _ecef(r, _gmst(jd, fr))[:, 0, :]
    q = query.lower().strip()
    out = []
    for i, name in enumerate(sat["names"]):
        if err[i, 0] or (groups and sat["groups"][i] not in groups) or (q and q not in name.lower()):
            continue
        x, y, z = p[i]
        rr = math.sqrt(x * x + y * y + z * z)
        out.append({"id": sat["ids"][i], "name": name, "group": sat["groups"][i], "lat": round(math.degrees(math.asin(z / rr)), 3),
                    "lon": round(math.degrees(math.atan2(y, x)), 3), "alt_km": round(rr - EARTH_R)})
        if len(out) >= limit:
            break
    return out


def _observer(lat: float, lon: float) -> tuple:
    la, lo = math.radians(lat), math.radians(lon)
    up = np.array([math.cos(la) * math.cos(lo), math.cos(la) * math.sin(lo), math.sin(la)])
    return up * EARTH_R, up


def passes(query: str, lat: float, lon: float, hours: float = 24, min_elevation: float = 10, start: float | None = None,
           most: int = 8) -> list:
    """Passes over a place in the next `hours`: [{name, rise, peak, set, max_elevation}] (unix times), soonest first."""
    sat = load()
    if sat["array"] is None:
        return []
    q = query.lower().strip() or "iss (zarya)"
    q = {"iss": "iss (zarya)", "international space station": "iss (zarya)", "tiangong": "css (tianhe)"}.get(q, q)
    idx = [i for i, n in enumerate(sat["names"]) if q == n.lower()] or [i for i, n in enumerate(sat["names"]) if q in n.lower()]
    idx = idx[:6]
    if not idx:
        return []
    start = start or time.time()
    ts = start + np.arange(0, hours * 3600, 30.0)
    jd, fr = _jd(ts)
    obs, up = _observer(lat, lon)
    err_all, r_all, _v = SatrecArray([sat["recs"][i] for i in idx]).sgp4(jd, fr)       # only the chosen few
    p_all = _ecef(r_all, _gmst(jd, fr))
    out = []
    for n, i in enumerate(idx):
        err, p = err_all[n:n + 1], p_all[n]
        d = p - obs
        el = np.degrees(np.arcsin(np.clip((d @ up) / np.linalg.norm(d, axis=1), -1, 1)))
        el[err[0] != 0] = -90
        above = el >= min_elevation
        k = 0
        while k < len(ts):
            if not above[k]:
                k += 1
                continue
            j = k
            while j < len(ts) and above[j]:
                j += 1
            seg = el[k:j]
            m = k + int(np.argmax(seg))
            out.append({"name": sat["names"][i], "rise": float(ts[k]), "peak": float(ts[m]), "set": float(ts[j - 1]),
                        "max_elevation": round(float(seg.max()))})
            k = j
    out.sort(key=lambda x: x["rise"])
    kept = []                                   # modules docked together (ISS ZARYA and NAUKA) pass together: once
    for x in out:
        if not any(abs(x["rise"] - k["rise"]) < 90 and abs(x["max_elevation"] - k["max_elevation"]) <= 2 for k in kept):
            kept.append(x)
    return kept[:most]

