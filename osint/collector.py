"""Runs each world-monitor feed on its own schedule, only while the monitor is on.

On: while the page shows MossadGPT (it sends a heartbeat - beat() - every minute; three minutes of silence
turn the monitor off), or always when Settings osint_background is on. Off: nothing is fetched.
A feed that fails backs off (doubling, up to 30 min) and says why in status(); one that is blocked by the
network route (netlayer) waits for the route. The assistant's tool can refresh a stale feed on demand.
"""
from __future__ import annotations

import concurrent.futures as cf
import threading
import time

import netlayer

from . import satellites, sources, store

LAYERS = {
    "flights": {"label": "Flights near your areas", "every": 30, "source": "adsb.lol", "group": "Movement"},
    "military": {"label": "Military aircraft (worldwide)", "every": 45, "source": "adsb.lol", "group": "Movement"},
    "flights_global": {"label": "Flights worldwide (every 20 min)", "every": 1200, "source": "OpenSky Network", "group": "Movement"},
    "satellites": {"label": "Satellites, ISS, Starlink", "every": 6 * 3600, "source": "CelesTrak (positions computed here)", "group": "Space"},
    "space": {"label": "Space weather and aurora", "every": 900, "source": "NOAA SWPC", "group": "Space"},
    "quakes": {"label": "Earthquakes", "every": 120, "source": "USGS", "group": "Hazards"},
    "news": {"label": "World events (conflict, protest)", "every": 900, "source": "GDELT", "group": "Events"},
    # Computed here from the layers above (osint/intel.py) - nothing fetched.
    "jamming": {"label": "GPS jamming (from aircraft)", "every": 60, "source": "computed here from ADS-B position accuracy", "group": "Hazards"},
    "alerts": {"label": "Alerts (emergencies, jamming, strong quakes)", "every": 60, "source": "computed here", "group": "Events"},
}
DEFAULT_AREAS = [{"name": "Israel", "lat": 31.8, "lon": 35.0, "nm": 250}]
BEAT_TIMEOUT_S = 180
MAX_BACKOFF_S = 1800
PRUNE_EVERY_S = 3600

_lock = threading.RLock()
_settings = lambda: {}                                   # noqa: E731  (configure() sets the real one)
_status: dict = {name: {"state": "off", "last_ok": 0, "error": "", "count": 0, "next": 0, "fails": 0} for name in LAYERS}
_extra: dict = {"kp": None, "aurora": [], "aurora_time": None}
_focus: dict = {"t": 0}
_running: set = set()
_last_beat = 0.0
_last_prune = 0.0
_thread: threading.Thread | None = None
_pool = cf.ThreadPoolExecutor(max_workers=3, thread_name_prefix="osint")
_stop = threading.Event()


def configure(settings_fn) -> None:
    global _settings
    _settings = settings_fn


def _cfg() -> dict:
    s = _settings() or {}
    return {"layers": {**{k: True for k in LAYERS}, **(s.get("osint_layers") or {})},
            "areas": s.get("osint_areas") or DEFAULT_AREAS, "background": bool(s.get("osint_background")),
            "days": float(s.get("osint_days") or 7), "max_mb": float(s.get("osint_max_mb") or 2048)}


def active() -> bool:
    return _cfg()["background"] or time.time() - _last_beat < BEAT_TIMEOUT_S


def beat() -> dict:
    """The page shows MossadGPT: keep collecting for three more minutes."""
    global _last_beat
    _last_beat = time.time()
    _ensure_thread()
    return status()


def _ensure_thread() -> None:
    global _thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            _stop.clear()
            _thread = threading.Thread(target=_loop, name="osint-collector", daemon=True)
            _thread.start()


def stop() -> None:
    _stop.set()


# ---- the jobs -------------------------------------------------------------------------------------
def _job_flights(cfg) -> int:
    n = 0
    for area in cfg["areas"][:6]:
        rows = sources.area_flights(float(area["lat"]), float(area["lon"]), int(area.get("nm") or 250))
        n += store.put_positions("flights", rows, max_age_s=120)
    return n


def _job(name: str, cfg: dict) -> int:
    if name == "flights":
        return _job_flights(cfg)
    if name == "military":
        return store.put_positions("military", sources.military(), max_age_s=240)
    if name == "flights_global":
        return store.put_positions("flights_global", sources.opensky_global(), max_age_s=1500, history=False)
    if name == "quakes":
        return store.put_events(sources.quakes())
    if name == "news":
        return store.put_events(sources.gdelt_events())
    if name == "space":
        kp, alerts, aurora = sources.space_weather()
        _extra.update(kp=kp, aurora=aurora, aurora_time=time.time())
        return store.put_events(alerts)
    if name == "satellites":
        return _job_satellites()
    if name == "jamming":
        from . import intel
        intel.sample()
        return len(intel.jamming())
    if name == "alerts":
        from . import intel
        intel.scan(cfg["areas"])
        return store.count_since("alerts", time.time() - 86400)
    raise KeyError(name)


def _job_satellites() -> int:
    """Orbit data for every group, at most every 6 hours (CelesTrak asks for no more than every 2). A group
    that fails (Starlink's file is large and slow) keeps its last orbits and alone is tried again in 30 min."""
    sat = satellites.load()
    have = sources.load_tles(satellites.TLE_PATH)
    fresh = sat["age_h"] is not None and sat["age_h"] < LAYERS["satellites"]["every"] / 3600
    want = [g for g in sources.SAT_GROUPS if not fresh or g not in have or g in _extra.get("sat_missing", ())]
    if not want:
        return len(sat["names"])
    missing, retry = [], 1800
    for g in want:
        try:
            have[g] = sources.tle_group(g)
        except sources.NotUpdated:                  # nothing new yet: keep what we have, ask after its next update
            retry = 7200
            if g not in have:
                missing.append(g)
        except (sources.FeedError, netlayer.RouteBlocked):
            missing.append(g)
    _extra["sat_missing"], _extra["sat_retry"] = missing, retry
    if not have:
        raise sources.FeedError("CelesTrak sent no orbit data")
    if len(missing) < len(want):
        sources.save_tles(satellites.TLE_PATH, have)
    return len(satellites.load()["names"])


def run_layer(name: str) -> dict:
    """Run one feed now (the collector's schedule, or the assistant's tool on a stale layer)."""
    with _lock:
        if name in _running:
            return _status[name]
        _running.add(name)
    st = _status[name]
    try:
        count = _job(name, _cfg())
        st.update(state="live", last_ok=time.time(), error="", count=count, fails=0,
                  next=time.time() + LAYERS[name]["every"])
        if name == "satellites" and _extra.get("sat_missing"):
            wait = _extra.get("sat_retry", 1800)
            st.update(error=f"Not loaded yet: {', '.join(_extra['sat_missing'])} (trying again in {wait // 60} min)", next=time.time() + wait)
        if name in ("quakes", "news", "space"):         # what's kept for the last day, not just what was new
            st["count"] = store.count_since(name, time.time() - 86400)
    except netlayer.RouteBlocked as e:
        st.update(state="blocked", error=str(e)[:160], next=time.time() + 120)
    except Exception as e:                                # a feed must never take the collector down
        st["fails"] = st.get("fails", 0) + 1
        wait = min(MAX_BACKOFF_S, LAYERS[name]["every"] * (2 ** st["fails"]))
        st.update(state="error", error=f"{type(e).__name__}: {str(e)[:140]}", next=time.time() + wait)
    finally:
        with _lock:
            _running.discard(name)
    return st


def fresh(name: str) -> bool:
    return time.time() - _status[name]["last_ok"] < LAYERS[name]["every"] * 2.5


def _loop() -> None:
    global _last_prune
    while not _stop.is_set():
        cfg = _cfg()
        now = time.time()
        if active():
            for name in LAYERS:
                st = _status[name]
                if not cfg["layers"].get(name, True):
                    st["state"] = "off"
                    continue
                if st["state"] == "off":
                    st["state"] = "waiting"
                if now >= st.get("next", 0) and name not in _running:
                    st["next"] = now + 5                     # not again until this run reports
                    _pool.submit(run_layer, name)
            if now - _last_prune > PRUNE_EVERY_S:
                _last_prune = now
                _pool.submit(store.prune, cfg["days"], cfg["max_mb"])
        else:
            for st in _status.values():
                if st["state"] in ("live", "waiting"):
                    st["state"] = "paused"
        _stop.wait(1.0)


# ---- what the page and the tool read --------------------------------------------------------------
def set_focus(lat: float, lon: float, label: str = "", radius_km: float = 300, kind: str = "",
              layer: str = "", id: str = "") -> None:
    """Where the assistant last pointed: the globe flies there (the page reads it from status). A tracked
    aircraft or satellite also names its layer and id, so the globe can lock onto the thing itself."""
    _focus.clear()
    _focus.update(t=time.time(), lat=lat, lon=lon, label=label, radius_km=radius_km,
                  **{k: v for k, v in (("kind", kind), ("layer", layer), ("id", str(id) if id else "")) if v})


def status() -> dict:
    now = time.time()
    cfg = _cfg()
    layers = []
    for name, meta in LAYERS.items():
        st = _status[name]
        state = st["state"]
        if state == "live" and not fresh(name):
            state = "stale"
        layers.append({"id": name, **meta, "on": cfg["layers"].get(name, True), "state": state,
                       "age_s": round(now - st["last_ok"]) if st["last_ok"] else None, "count": st["count"], "error": st["error"]})
    return {"active": active(), "background": cfg["background"], "layers": layers, "kp": _extra["kp"],
            "areas": cfg["areas"], "store_mb": round(store.size_mb(), 1), "limits": {"days": cfg["days"], "max_mb": cfg["max_mb"]},
            "focus": dict(_focus) if _focus.get("t") else None}


def aurora() -> list:
    return _extra["aurora"]
