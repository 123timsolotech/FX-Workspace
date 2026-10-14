"""The assistant's world_monitor tool (offered only to MossadGPT's turns - composer tool "world").

  near     what is around a place now: aircraft, military aircraft, earthquakes and world events of the
           last hours, satellites overhead
  events   events in a place or region over the last hours (earthquakes, world events, space weather)
  track    one aircraft (callsign, hex, registration) or satellite: where it is, where it has been
  passes   when a satellite (default the ISS) passes over a place
  status   which feeds are live and how old their data is
  show     turn the globe to a place

Every figure comes from the local store (osint.store), refreshed first when stale; each source is
numbered for citation like web results ([n]). What a feed doesn't cover is said, not guessed.
"""
from __future__ import annotations

import math
import re
import time

import netlayer

from . import collector, geo, satellites, sources, store

COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
ACTIONS = ("near", "events", "track", "passes", "jamming", "alerts", "changes", "status", "show")


def _cite(url: str, title: str, snippet: str = "", date: str = "") -> int | None:
    if not url:
        return None
    try:
        import sources as registry
        return registry.current().add(url, title=title, snippet=snippet, date=date, read=True, note="world monitor")
    except Exception:
        return None


def _ref(n) -> str:
    return f" [{n}]" if n else ""


def _bearing(lat1, lon1, lat2, lon2) -> str:
    y = math.sin(math.radians(lon2 - lon1)) * math.cos(math.radians(lat2))
    x = (math.cos(math.radians(lat1)) * math.sin(math.radians(lat2))
         - math.sin(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.cos(math.radians(lon2 - lon1)))
    return COMPASS[int(((math.degrees(math.atan2(y, x)) + 360) % 360 + 22.5) // 45) % 8]


def _clock(t: float) -> str:
    return time.strftime("%H:%M", time.localtime(t)) + time.strftime(" (%H:%MZ)", time.gmtime(t))


def _age(t: float) -> str:
    s = max(0, time.time() - t)
    return f"{s:.0f} s" if s < 90 else f"{s / 60:.0f} min" if s < 5400 else f"{s / 3600:.1f} h"


def _ensure(*layers: str) -> list:
    """Refresh stale layers now (the monitor may be off). Returns notes about feeds that failed."""
    notes = []
    for name in layers:
        if not collector.fresh(name):
            st = collector.run_layer(name)
            if st["state"] in ("error", "blocked"):
                notes.append(f"{collector.LAYERS[name]['label']}: couldn't refresh ({st['error']}); older data shown if any.")
    return notes


def _in_areas(lat, lon, radius_km) -> bool:
    for a in collector._cfg()["areas"]:
        if geo.km(lat, lon, float(a["lat"]), float(a["lon"])) + radius_km <= float(a.get("nm") or 250) * 1.852:
            return True
    return False


def _aircraft_line(a: dict, p: dict) -> str:
    pr = a["props"]
    d = geo.km(p["lat"], p["lon"], a["lat"], a["lon"])
    what = ", ".join(x for x in (pr.get("type"), pr.get("reg")) if x)
    alt = "on the ground" if pr.get("ground") else (f"{round(a['alt']):,} m" if a.get("alt") is not None else "altitude unknown")
    move = f", {round(a['spd'])} km/h heading {round(a['hdg'])}°" if a.get("spd") is not None and a.get("hdg") is not None else ""
    flags = " MILITARY" if pr.get("mil") else ""
    flags += f" SQUAWK {pr['squawk']}" if pr.get("squawk") in ("7500", "7600", "7700") else ""
    n = _cite(f"https://globe.adsb.lol/?icao={a['id']}", f"{a['label'] or a['id']} - live position (adsb.lol)")
    return f"- {a['label'] or a['id']}{f' ({what})' if what else ''}{flags}: {d:.0f} km {_bearing(p['lat'], p['lon'], a['lat'], a['lon'])} of {p['name']}, {alt}{move}{_ref(n)}"


def _near(args: dict, p: dict) -> str:
    radius = float(args.get("radius_km") or p["radius_km"])
    hours = float(args.get("hours") or 24)
    want = set(args.get("layers") or ("flights", "military", "quakes", "news", "satellites"))
    notes = []
    lines = [f"WORLD MONITOR - {p['name']} ({p['lat']:.2f}, {p['lon']:.2f}), within {radius:.0f} km, at {_clock(time.time())}."]
    b = geo.bbox(p["lat"], p["lon"], radius)
    if want & {"flights", "military"}:
        if "flights" in want and not _in_areas(p["lat"], p["lon"], radius):
            try:                                     # outside the watched areas: read this spot once
                store.put_positions("flights", sources.area_flights(p["lat"], p["lon"], min(250, radius / 1.852)), max_age_s=120)
            except (sources.FeedError, netlayer.RouteBlocked) as e:
                notes.append(f"Aircraft near {p['name']}: couldn't read ({e}).")
        else:
            notes += _ensure("flights")
        notes += _ensure("military")
        seen, planes = set(), []
        for a in store.current(["military", "flights"], b):
            if a["id"] in seen or geo.km(p["lat"], p["lon"], a["lat"], a["lon"]) > radius:
                continue
            if a["layer"] == "military":
                a["props"]["mil"] = 1
            seen.add(a["id"])
            planes.append(a)
        mil = [a for a in planes if a["props"].get("mil")]
        planes.sort(key=lambda a: (not a["props"].get("mil"), geo.km(p["lat"], p["lon"], a["lat"], a["lon"])))
        newest = max((a["t"] for a in planes), default=0)
        lines.append(f"\nAIRCRAFT: {len(planes)} broadcasting positions ({len(mil)} flagged military)"
                     + (f"; newest position {_age(newest)} old" if planes else "") + ". Aircraft with transponders off are not seen.")
        lines += [_aircraft_line(a, p) for a in planes[:14]]
        if len(planes) > 14:
            lines.append(f"- ...and {len(planes) - 14} more")
    since = time.time() - hours * 3600
    if "quakes" in want:
        notes += _ensure("quakes")
        qs = [e for e in store.events("quakes", since, b) if geo.km(p["lat"], p["lon"], e["lat"], e["lon"]) <= radius]
        lines.append(f"\nEARTHQUAKES (last {hours:.0f} h): {len(qs) or 'none'}")
        for e in sorted(qs, key=lambda e: -e["props"].get("mag", 0))[:8]:
            n = _cite(e["url"], e["title"], date=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(e["t"])))
            lines.append(f"- {e['title']}, {_clock(e['t'])}, depth {e['props'].get('depth_km', '?')} km{_ref(n)}")
    if "news" in want:
        notes += _ensure("news")
        lines += _news_lines(store.events("news", since, b), p, radius, hours)
    if "satellites" in want:
        notes += _ensure("satellites")
        order = {"stations": 0, "visual": 1, "military": 2, "weather": 3, "gnss": 4}
        over = sorted((s for s in satellites.positions(groups=tuple(order))
                       if geo.km(p["lat"], p["lon"], s["lat"], s["lon"]) < _footprint_km(s["alt_km"], 10)),
                      key=lambda s: (order[s["group"]], geo.km(p["lat"], p["lon"], s["lat"], s["lon"])))
        starlink = sum(1 for s in satellites.positions(groups=("starlink",))
                       if geo.km(p["lat"], p["lon"], s["lat"], s["lon"]) < _footprint_km(s["alt_km"], 25))
        lines.append(f"\nSATELLITES ABOVE 10° NOW (orbit data CelesTrak, positions computed here): {len(over)} tracked"
                     + (f"; Starlink above 25°: {starlink}" if starlink else ""))
        for s in over[:10]:
            lines.append(f"- {s['name']} ({s['group']}), {s['alt_km']} km up, over {geo.where(s['lat'], s['lon'])}")
    collector.set_focus(p["lat"], p["lon"], p["name"], radius, kind=p.get("kind", ""))
    return "\n".join(lines + ([""] + notes if notes else []))


def _footprint_km(alt_km: float, min_elevation: float, earth_km: float = 6371.0) -> float:
    """Ground distance within which a satellite at alt_km is above min_elevation."""
    e = math.radians(min_elevation)
    return (math.acos(earth_km / (earth_km + alt_km) * math.cos(e)) - e) * earth_km


def _news_lines(evs: list, p: dict | None, radius: float, hours: float) -> list:
    if p:
        evs = [e for e in evs if geo.km(p["lat"], p["lon"], e["lat"], e["lon"]) <= radius]
    by_url: dict = {}
    for e in evs:
        by_url.setdefault(e["url"], []).append(e)
    stories = sorted(by_url.values(), key=lambda g: (-max(x["props"].get("sources", 1) for x in g), -g[0]["t"]))
    kinds: dict = {}
    for e in evs:
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    out = [f"\nWORLD EVENTS (GDELT, machine-coded from news - last {hours:.0f} h): {len(evs) or 'none'}"
           + (" - " + ", ".join(f"{k} {v}" for k, v in sorted(kinds.items(), key=lambda kv: -kv[1])) if kinds else "")]
    for g in stories[:10]:
        e = g[0]
        n = _cite(e["url"], e["title"], date=time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(e["t"])))
        out.append(f"- {e['title']} ({_clock(e['t'])}; {len(g)} coded event(s)){_ref(n)}")
    if evs:
        out.append("GDELT codes events automatically from articles: treat each as one outlet's report until others confirm it.")
    return out


def _events(args: dict, p: dict | None) -> str:
    hours = float(args.get("hours") or 24)
    since = time.time() - hours * 3600
    radius = float(args.get("radius_km") or (p["radius_km"] if p else 0))
    b = geo.bbox(p["lat"], p["lon"], radius) if p else None
    kinds = set(args.get("layers") or ("quakes", "news", "space"))
    lines = [f"WORLD MONITOR - events {'near ' + p['name'] if p else 'worldwide'}, last {hours:.0f} h, at {_clock(time.time())}."]
    notes = _ensure(*[k for k in ("quakes", "news", "space") if k in kinds])
    if "quakes" in kinds:
        qs = [e for e in store.events("quakes", since, b) if not p or geo.km(p["lat"], p["lon"], e["lat"], e["lon"]) <= radius]
        big = sorted(qs, key=lambda e: -e["props"].get("mag", 0))
        lines.append(f"\nEARTHQUAKES: {len(qs)}" + ("" if p else " (magnitude 4.5 and up listed)"))
        for e in [x for x in big if p or x["props"].get("mag", 0) >= 4.5][:10]:
            n = _cite(e["url"], e["title"])
            lines.append(f"- {e['title']}, {_clock(e['t'])}{_ref(n)}")
    if "news" in kinds:
        lines += _news_lines(store.events("news", since, b, limit=3000), p, radius, hours)
    if "space" in kinds:
        kp = collector._extra.get("kp") or {}
        alerts = store.events("space", since)
        lines.append(f"\nSPACE WEATHER (NOAA SWPC): planetary K index {kp.get('kp', '?')} at {kp.get('time', '?')} UTC"
                     f" ({'storm' if (kp.get('kp') or 0) >= 5 else 'quiet to unsettled'}); alerts: {len(alerts)}")
        for e in alerts[:5]:
            lines.append(f"- {e['title']} ({_clock(e['t'])}){_ref(_cite(e['url'], 'NOAA SWPC alerts'))}")
    if p:
        collector.set_focus(p["lat"], p["lon"], p["name"], radius, kind=p.get("kind", ""))
    return "\n".join(lines + ([""] + notes if notes else []))


def _track(args: dict) -> str:
    q = str(args.get("query") or args.get("place") or "").strip()
    if not q:
        return "Give a callsign, an aircraft hex code, a registration or a satellite name as query."
    _ensure("military")
    found = store.find(q)
    if found:
        a = found[0]
        pr = a["props"]
        hist = store.track(a["id"], time.time() - 6 * 3600)
        name = a["label"] or a["id"]
        kind = ", ".join(x for x in (pr.get("type"), pr.get("reg"), "military" if pr.get("mil") or a["layer"] == "military" else "") if x)
        alt = "on the ground" if pr.get("ground") else f"{round(a['alt']):,} m" if a.get("alt") is not None else "altitude unknown"
        move = f", {round(a['spd'])} km/h heading {round(a['hdg'])}°" if a.get("spd") is not None and a.get("hdg") is not None else ""
        n = _cite(f"https://globe.adsb.lol/?icao={a['id']}", f"{name} - live position (adsb.lol)")
        lines = [f"WORLD MONITOR - {name} ({kind or 'aircraft'})",
                 f"Now: {geo.where(a['lat'], a['lon'])} ({a['lat']:.3f}, {a['lon']:.3f}), {alt}{move} - position {_age(a['t'])} old{_ref(n)}"]
        if len(hist) >= 2:
            first = hist[0]
            dist = sum(geo.km(x["lat"], x["lon"], y["lat"], y["lon"]) for x, y in zip(hist, hist[1:]))
            lines.append(f"Seen here since {_clock(first['t'])}, first at {geo.where(first['lat'], first['lon'])}; about {dist:.0f} km flown since.")
        collector.set_focus(a["lat"], a["lon"], a["label"] or a["id"], 150, kind="aircraft", layer=a["layer"], id=a["id"])
        return "\n".join(lines)
    _ensure("satellites")
    sats = satellites.positions(query=q, limit=3)
    if sats:
        s = sats[0]
        collector.set_focus(s["lat"], s["lon"], s["name"], 2000, kind="satellite", layer="satellites", id=s.get("id", ""))
        return (f"WORLD MONITOR - {s['name']} (satellite, {s['group']}): now {s['alt_km']} km above {geo.where(s['lat'], s['lon'])}"
                f" - computed here from CelesTrak orbit data{_ref(_cite('https://celestrak.org/', 'CelesTrak orbit data'))}.")
    return (f"No aircraft or satellite matching '{q}' is in the monitor right now. Aircraft are only seen while they broadcast "
            "(ADS-B) within the watched areas or are flagged military; try near <place> to read another area.")


def _passes(args: dict, p: dict) -> str:
    _ensure("satellites")
    name = str(args.get("query") or "ISS (ZARYA)")
    hours = min(72.0, float(args.get("hours") or 24))
    found = satellites.passes(name, p["lat"], p["lon"], hours=hours)
    if not found:
        return f"No passes of '{name}' above 10° over {p['name']} in the next {hours:.0f} h (or no such satellite in the orbit data)."
    n = _cite("https://celestrak.org/", "CelesTrak orbit data")
    lines = [f"WORLD MONITOR - passes over {p['name']} in the next {hours:.0f} h (computed here from CelesTrak orbit data{_ref(n)}; local time, UTC in brackets):"]
    for i, x in enumerate(found):               # soonest first; the first is spelled out so it is never mistaken
        mins = max(0, round((x["rise"] - time.time()) / 60))
        when = f"NEXT, in {mins} min" if i == 0 and mins < 180 else "NEXT" if i == 0 else "then"
        lines.append(f"- {when}: {x['name']} rises {_clock(x['rise'])}, highest {x['max_elevation']}° at {_clock(x['peak'])}, sets {_clock(x['set'])}")
    lines.append("Visible to the eye only when the satellite is sunlit and the sky is dark (about an hour after sunset or before sunrise).")
    return "\n".join(lines)


_JAM_NOTE = ("Degraded = the aircraft reports poor GPS position accuracy (ADS-B NACp < 8 or NIC < 7) - the gpsjam.org "
             "method. It points to jamming or spoofing nearby but doesn't prove it, and it only covers aircraft the monitor "
             "sees (near the watched areas, and military worldwide).")


def _jamming(args: dict, p: dict | None) -> str:
    from . import intel
    _ensure("flights", "military")
    intel.sample()
    b = geo.bbox(p["lat"], p["lon"], max(float(args.get("radius_km") or p["radius_km"]), 300)) if p else None
    cells = intel.jamming(b)
    where = f" around {p['name']}" if p else ""
    if not cells:
        return (f"WORLD MONITOR - GPS interference{where}: no cell with degraded aircraft positions in the last 30 minutes "
                f"(at least {intel.MIN_AIRCRAFT} aircraft per half-degree cell needed to say anything). {_JAM_NOTE}")
    lines = [f"WORLD MONITOR - GPS interference{where}, last 30 minutes, computed here from ADS-B accuracy:"]
    for c in cells[:8]:
        lines.append(f"- {c['level'].upper()}: near {geo.where(c['lat'], c['lon'])} ({c['lat']:.1f}, {c['lon']:.1f}) - "
                     f"{c['bad']} of {c['n']} aircraft degraded ({round(c['pct'] * 100)}%)")
    if p:
        collector.set_focus(p["lat"], p["lon"], p["name"], max(float(p["radius_km"]), 300), kind=p.get("kind", ""))
    return "\n".join(lines + [_JAM_NOTE])


def _alerts(args: dict, p: dict | None) -> str:
    from . import intel
    _ensure("flights", "military", "quakes")
    intel.sample()
    intel.scan(collector._cfg()["areas"])
    hours = min(72.0, float(args.get("hours") or 6))
    b = geo.bbox(p["lat"], p["lon"], max(float(args.get("radius_km") or p["radius_km"]), 300)) if p else None
    evs = store.events("alerts", time.time() - hours * 3600, b, limit=30)
    where = f" around {p['name']}" if p else ""
    if not evs:
        return (f"WORLD MONITOR - alerts{where}, last {hours:.0f} h: none (emergency squawks 7500/7600/7700, GPS interference "
                "hotspots, M6+ quakes or M4.5+ near the watched areas).")
    lines = [f"WORLD MONITOR - alerts{where}, last {hours:.0f} h, newest first (computed here from the monitor's feeds):"]
    for e in sorted(evs, key=lambda e: (-e["props"].get("sev", 1), -e["t"]))[:12]:
        n = _cite(e["url"], e["title"][:90]) if e["url"] else None
        lines.append(f"- [{e['props'].get('severity', '?')}] {_clock(e['t'])}: {e['title']}{_ref(n)}")
    return "\n".join(lines)


def _changes(args: dict, p: dict) -> str:
    from . import intel
    _ensure("flights", "military", "quakes", "news")
    intel.sample()
    hours = min(24.0, max(0.25, float(args.get("hours") or 1)))
    c = intel.changes({**p, "radius_km": float(args.get("radius_km") or p["radius_km"])}, hours)
    a = c["aircraft"]
    delta = lambda d: f"{d['before']} -> {d['now']}" + (f" ({d['now'] - d['before']:+d})" if d["now"] != d["before"] else " (same)")  # noqa: E731
    lines = [f"WORLD MONITOR - what changed within {c['radius_km']} km of {c['place']} over the last {hours:g} h "
             "(from the history this PC recorded, one point a minute per aircraft):",
             f"- Military aircraft seen: {delta(a['military'])}",
             f"- All tracked aircraft (near the watched areas only): {delta(a['flights'])}",
             f"- New earthquakes: {c['events']['quakes']}; new conflict/protest events coded from news: {c['events']['news']}"]
    if c["alerts"]:
        lines.append(f"- Alerts: " + "; ".join(e["title"] for e in c["alerts"][:4]))
    if c["jamming"]:
        j = c["jamming"][0]
        lines.append(f"- GPS interference: {len(c['jamming'])} cell(s), worst near {geo.where(j['lat'], j['lon'])} "
                     f"({round(j['pct'] * 100)}% of {j['n']} aircraft degraded)")
    if not (a["military"]["before"] or a["flights"]["before"]):
        lines.append("Note: no history from then - the monitor collects only while MossadGPT is on screen (or with "
                     "background collection on), so 'before' may just mean it wasn't collecting.")
    collector.set_focus(p["lat"], p["lon"], p["name"], c["radius_km"], kind=p.get("kind", ""))
    return "\n".join(lines)


def _status() -> str:
    st = collector.status()
    lines = [f"WORLD MONITOR status: {'collecting' if st['active'] else 'idle (collects while MossadGPT is on screen)'}; "
             f"store {st['store_mb']} MB of {st['limits']['max_mb']:.0f} MB, {st['limits']['days']:.0f} days kept."]
    for x in st["layers"]:
        age = f", data {x['age_s']} s old" if x["age_s"] is not None else ""
        why = f" - {x['error']}" if x["error"] else ""
        lines.append(f"- {x['label']} ({x['source']}): {x['state']}{age}{why}")
    lines.append("Watched areas: " + ", ".join(f"{a['name']} ({a.get('nm', 250)} nm)" for a in st["areas"]))
    return "\n".join(lines)


# ---- What a sentence talks about (voice mode's globe follows the reply as it's spoken) ---------------
_CALLSIGN = re.compile(r"\b(?:[A-Z]{2,4}\d{1,5}[A-Z]{0,2}|[A-Z0-9]{1,2}-[A-Z0-9]{3,5}|[0-9a-f]{6})\b")
_ISS = re.compile(r"\bISS\b|space station|תחנת החלל|\bМКС\b", re.I)


def mentions(text: str, most: int = 4) -> list:
    """[{name, lat, lon, radius_km, kind, layer?, id?}] for the places, aircraft (callsign, registration, hex)
    and the ISS a sentence names, in the order it names them. Only what the monitor can place right now."""
    found = []
    for pos, p in geo.places_in(text, most):
        found.append((pos, p))
    for m in _CALLSIGN.finditer(str(text or "")):
        hit = store.find(m.group(0))
        if hit:
            a = hit[0]
            found.append((m.start(), {"name": a["label"] or a["id"], "lat": a["lat"], "lon": a["lon"], "radius_km": 120,
                                      "kind": "aircraft", "layer": a["layer"], "id": a["id"]}))
    m = _ISS.search(str(text or ""))
    if m:
        s = satellites.positions(query="ISS (ZARYA)", limit=1)
        if s:
            found.append((m.start(), {"name": "ISS", "lat": s[0]["lat"], "lon": s[0]["lon"], "radius_km": 1500,
                                      "kind": "satellite", "layer": "satellites", "id": s[0]["id"]}))
    found.sort(key=lambda x: x[0])
    out, seen = [], set()
    for _pos, p in found:
        if p["name"] not in seen:
            seen.add(p["name"])
            out.append(p)
    return out[:most]


# A question about the world right now (English, Hebrew, Russian). The model can't know any of it from memory.
_WORLD_Q = re.compile(
    r"\b(?:iss|space station|satellites?|starlink|pass(?:es)? over|flights?|aircraft|planes?|jets?|helicopters?|military|"
    r"drones?|earthquakes?|quakes?|tremors?|space weather|aurora|geomagnetic|happening|going on|events?|strikes?|attacks?|"
    r"protests?|fighting|overhead|in the air|jamming|jammed|gps|gnss|spoof\w*|squawk\w*|emergenc(?:y|ies)|alerts?|"
    r"what(?:'s| has)? changed|changes)\b"
    r"|מטוס|מטוסים|טיס|צבאי|רעידת|לוויין|לווין|תחנת החלל|מה קורה|אירועים|תקיפ|שמיים|שיבוש|ג'י ?פי ?אס|חירום|התרע|"
    r"самол[её]т|рейс|военн|спутник|мкс|землетрясени|что происходит|событи|глушени|подавлени|тревог", re.I)


def turn_hint(message: str) -> str:
    """The turn header's nudge for MossadGPT: when the request is about the world now, call the tool first."""
    if not _WORLD_Q.search(str(message or "")):
        return ""
    return ("WORLD MONITOR NEEDED: this asks about the world now. You do not know current positions, events or "
            "satellite passes - and earlier chats, their summaries and memory are not current data either. Call "
            "world_monitor before answering (near / events / passes / track, one call per part of the question) and "
            "answer only from what it returns, citing [n]. Never estimate a time or a count.")


def world_monitor_tool(args: dict) -> dict:
    action = str(args.get("action") or "near").lower()
    if action not in ACTIONS:
        return {"ok": False, "message": f"action must be one of {', '.join(ACTIONS)}."}
    started = time.time()
    def result(message: str, place: dict | None = None) -> dict:
        import sources as citations
        refs = sorted({int(n) for n in re.findall(r"\[(\d+)\]", message)})
        evidence = [{k: item.get(k) for k in ("n", "url", "title", "date", "domain")}
                    for n in refs if (item := citations.current().get(n))]
        st = collector.status()
        focus = st.get("focus") or {}
        if focus.get("t", 0) < started:
            focus = {k: place[k] for k in ("lat", "lon", "name", "radius_km", "kind") if place and k in place}
        return {"ok": True, "message": message, "world": {"action": action, "focus": focus,
                "evidence": evidence, "layers": [{k: layer.get(k) for k in ("id", "state", "age_s", "source")}
                                            for layer in st.get("layers", [])], "at": time.time()}}
    try:
        if action == "status":
            return result(_status())
        if action == "track":
            return result(_track(args))
        place = args.get("place") or ""
        p = geo.place(place) if place else None
        if place and not p:
            return {"ok": False, "message": f"Couldn't place '{place}'. Give a city (any language), a region (Gaza, Red Sea, "
                                            "Hormuz, Levant...) or 'lat,lon'."}
        if action == "events":
            return result(_events(args, p), p)
        if action == "jamming":
            return result(_jamming(args, p), p)
        if action == "alerts":
            return result(_alerts(args, p), p)
        if not p:
            return {"ok": False, "message": f"{action} needs a place: a city, a region or 'lat,lon'."}
        if action == "show":
            collector.set_focus(p["lat"], p["lon"], p["name"], float(args.get("radius_km") or p["radius_km"]), kind=p.get("kind", ""))
            return result(f"The globe now shows {p['name']}.", p)
        if action == "passes":
            return result(_passes(args, p), p)
        if action == "changes":
            return result(_changes(args, p), p)
        return result(_near(args, p) + "\n\nCite as [n]. Positions are what aircraft and sources broadcast - "
                "report them as such, with their times.", p)
    except netlayer.RouteBlocked as e:
        return {"ok": False, "status": "permanent_failure", "blocked": True, "message": str(e)}
