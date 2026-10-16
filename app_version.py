"""
app_version.py
==============
The app's version (the VERSION file - the one number the installer, the desktop app and Settings > About use)
and the update check: Settings > About > Check for updates asks GitHub for the newest release and, when it is
newer, links its installer. Updating = run the new installer over this one: it keeps chats, settings, keys,
models and the Python environment, and replaces only the app's own files (installer/FXWorkspace.iss).

Nothing is checked unless the user presses the button; it goes through netlayer like every other request.
"""
from __future__ import annotations

import os
import re
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = "fuxion64-ctrl/FX-Workspace"
RELEASES_URL = f"https://github.com/{REPO}/releases"
_cache: dict = {"at": 0.0, "result": None}


def current() -> str:
    try:
        with open(os.path.join(ROOT, "VERSION"), encoding="utf-8") as f:
            return f.read().strip() or "0.0.0"
    except OSError:
        return "0.0.0"


def _parts(v: str) -> tuple:
    """'v1.4.0-beta' -> (1, 4, 0). Missing parts count as 0."""
    nums = [int(n) for n in re.findall(r"\d+", str(v or ""))[:3]]
    return tuple(nums + [0] * (3 - len(nums)))


def newer(latest: str, mine: str) -> bool:
    return _parts(latest) > _parts(mine)


def check(force: bool = False) -> dict:
    """{"current", "latest", "newer", "url", "installer", "notes", "published"} - or {"current", "error"}."""
    mine = current()
    if not force and _cache["result"] and time.time() - _cache["at"] < 600:
        return {**_cache["result"], "current": mine}
    import netlayer
    try:
        with netlayer.client("other", timeout=15.0, headers={"Accept": "application/vnd.github+json",
                                                              "User-Agent": "fx-workspace-update-check"}) as c:
            r = c.get(f"https://api.github.com/repos/{REPO}/releases/latest")
        if r.status_code == 404:
            return {"current": mine, "error": "No release has been published yet."}
        r.raise_for_status()
        rel = r.json()
    except Exception as e:                                   # offline, blocked by the network settings, rate limit
        return {"current": mine, "error": f"Couldn't reach GitHub ({str(e)[:160]})."}
    latest = str(rel.get("tag_name") or rel.get("name") or "").lstrip("vV")
    setup = next((a.get("browser_download_url") for a in rel.get("assets") or []
                  if str(a.get("name", "")).lower().endswith(".exe") and "setup" in str(a.get("name", "")).lower()), None)
    result = {"latest": latest, "newer": newer(latest, mine), "url": rel.get("html_url") or RELEASES_URL,
              "installer": setup, "notes": str(rel.get("body") or "")[:4000], "published": rel.get("published_at")}
    _cache.update(at=time.time(), result=result)
    return {**result, "current": mine}
