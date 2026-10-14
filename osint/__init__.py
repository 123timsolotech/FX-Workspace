"""
osint - the world monitor (MossadGPT's globe and the AI's world_monitor tool)
===========================================================================
Live public data, collected on this PC and kept here, so the globe draws it and the assistant can
ask about it. Every source is read straight from its origin (no middleman service, no key); what
arrives is stored in data/osint/osint.db (sizes and days capped: Settings keys osint_*).

  geo.py         places (GeoNames cities, with Hebrew/Russian/Arabic names) and regions, distances
  store.py       the local store: current positions, position history, events
  sources.py     the feeds: flights (airplanes.live, adsb.lol, OpenSky), military flights, earthquakes
                 (USGS), space weather (NOAA SWPC), world news events (GDELT), satellite orbits (CelesTrak)
  satellites.py  satellite positions and passes, computed here from the orbit data (sgp4)
  collector.py   runs each feed on its own schedule while the monitor is on; status per layer
  tool.py        the world_monitor tool the assistant calls (only offered with MossadGPT)

The feature belongs to the secret theme: the page turns the monitor on while MossadGPT is on screen
(a heartbeat), and the tool is offered only to MossadGPT's turns (agent/tasks._begin_composer_tools).
"""
