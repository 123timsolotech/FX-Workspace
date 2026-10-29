"""
roblox_places.py
================
Resolves a loose name ("open Test1", "open my obby") to a Roblox place file
(.rbxl / .rbxlx) on disk, so "open <place>" works the same way "open <app>"
does through app_registry.

main.py and desktop_router.py both imported this module, but it was missing
from the workspace - the server could not even start. This is the
implementation both call sites expect:

    resolve_place(name) -> (match | None, candidates: list[dict])
    _default_dirs()     -> list[str]   (folders that are searched)

Each place dict is {"display": <file stem>, "path": <absolute path>,
"kind": "roblox_place"} - the same shape app_registry uses, so callers can
merge the two candidate lists.

Folders searched (only those that exist):
  * the open project's folder (extra_dirs, set by agent/sessions.py), first
  * every entry of ROBLOX_PLACES_DIRS (os.pathsep-separated)
  * ~/Documents/ROBLOX, ~/OneDrive/Documents/ROBLOX, ~/Documents, ~/Desktop,
    ~/OneDrive/Desktop, ~/Downloads
The walk is shallow (depth 3) and cached for a minute; place files are small
in number, so this stays cheap enough to run inline before an app launch.
"""
from __future__ import annotations

import difflib
import os
import re
import time

PLACE_EXTENSIONS = (".rbxl", ".rbxlx")
_MAX_DEPTH = 3
_CACHE_TTL = 60.0
_SKIP_DIRS = {"node_modules", ".git", ".venv", "venv", "__pycache__", "AppData", "$RECYCLE.BIN"}
_cache = {"at": 0.0, "dirs": (), "places": []}
# () -> folders to search before the defaults (the open project's folder).
extra_dirs = lambda: []


def place_key(name) -> str:
    """A place name as compared, not as typed: without quotes, case, extra spaces or the " (placeId: 123)"
    Roblox's Studio MCP adds. A project saved with the place "\\"Scratch Test\\"" (quotes typed in) made every
    request in it say Studio had a different place open - and "ask before changing anything" (2026-09-29)."""
    text = re.sub(r"\s*\(placeId:\s*\d+\)\s*$", "", str(name or ""), flags=re.I)
    return " ".join(text.strip().strip("\"'“”‘’`").split()).casefold()


def _default_dirs() -> list[str]:
    home = os.path.expanduser("~")
    configured = [p.strip() for p in os.environ.get("ROBLOX_PLACES_DIRS", "").split(os.pathsep) if p.strip()]
    try:
        project = [p for p in extra_dirs() or [] if p]
    except Exception:
        project = []
    candidates = project + configured + [
        os.path.join(home, "Documents", "ROBLOX"),
        os.path.join(home, "OneDrive", "Documents", "ROBLOX"),
        os.path.join(home, "Documents"),
        os.path.join(home, "Desktop"),
        os.path.join(home, "OneDrive", "Desktop"),
        os.path.join(home, "Downloads"),
    ]
    out, seen = [], set()
    for path in candidates:
        path = os.path.abspath(os.path.expanduser(path))
        key = os.path.normcase(path)
        if key in seen or not os.path.isdir(path):
            continue
        seen.add(key)
        out.append(path)
    return out


def _walk(root: str, depth: int = 0):
    try:
        with os.scandir(root) as it:
            for entry in it:
                try:
                    if entry.is_file() and entry.name.lower().endswith(PLACE_EXTENSIONS):
                        yield entry.path
                    elif entry.is_dir(follow_symlinks=False) and depth < _MAX_DEPTH \
                            and entry.name not in _SKIP_DIRS and not entry.name.startswith("."):
                        yield from _walk(entry.path, depth + 1)
                except OSError:
                    continue
    except OSError:
        return


def list_places(force: bool = False) -> list[dict]:
    dirs = tuple(_default_dirs())
    now = time.time()
    if not force and _cache["dirs"] == dirs and now - _cache["at"] < _CACHE_TTL:
        return _cache["places"]
    places, seen = [], set()
    for root in dirs:
        for path in _walk(root):
            key = os.path.normcase(os.path.abspath(path))
            if key in seen:
                continue
            seen.add(key)
            stem = os.path.splitext(os.path.basename(path))[0]
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0.0
            places.append({"display": stem, "path": path, "kind": "roblox_place", "mtime": mtime})
    # Most recently edited first: when two folders hold a "Test1", the one
    # the user touched last is the one they mean.
    places.sort(key=lambda p: p["mtime"], reverse=True)
    _cache.update(at=now, dirs=dirs, places=places)
    return places


def _key(value: str) -> str:
    value = (value or "").lower().strip()
    value = re.sub(r"\.(rbxlx?)$", "", value)
    value = re.sub(r"\b(?:the|roblox|place|game|studio|project|file)\b", " ", value)
    return re.sub(r"[^a-z0-9]+", "", value)


def resolve_place(name: str, top_n: int = 5):
    """Exact (normalized) name match wins outright; otherwise return close
    candidates for the caller to offer as a clarification. Never guesses."""
    wanted = _key(name)
    if not wanted:
        return None, []
    places = list_places()
    if not places:
        return None, []
    by_key: dict[str, list[dict]] = {}
    for place in places:
        by_key.setdefault(_key(place["display"]), []).append(place)
    exact = by_key.get(wanted)
    if exact:
        return exact[0], []
    close = difflib.get_close_matches(wanted, list(by_key), n=top_n, cutoff=0.72)
    contains = [k for k in by_key if len(wanted) >= 4 and (wanted in k or k in wanted) and k not in close]
    keys = (close + contains)[:top_n]
    return None, [by_key[k][0] for k in keys]
