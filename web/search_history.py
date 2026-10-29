"""
search_history.py
=================
Web Search's optional history (Settings > Web Search > Keep search history):
searches and the pages read, in data/search_history.json, kept for the number
of days the user chose. Off by default. Never written in Maximum Privacy or
Local Only, and never from a temporary chat (privacy.py). Local Only searches
it. "Clear search history & cache" deletes it and empties the in-memory caches.
"""
from __future__ import annotations

import json
import os
import threading
import time

import atomic_io
import privacy

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "search_history.json")
MAX_SEARCHES = 500
MAX_PAGES = 300
PAGE_CHARS = 20_000
DEFAULT_DAYS = 30
_lock = threading.Lock()


def enabled(settings: dict, mode: str) -> bool:
    return bool((settings or {}).get("search_history")) and mode in ("standard", "private") and not privacy.active()


def _days(settings: dict) -> float:
    try:
        return max(1.0, min(float((settings or {}).get("search_history_days") or DEFAULT_DAYS), 365.0))
    except (TypeError, ValueError):
        return float(DEFAULT_DAYS)


def load() -> dict:
    try:
        with open(PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {"searches": [], "pages": {}}
    if not isinstance(data, dict):
        return {"searches": [], "pages": {}}
    return {"searches": [s for s in data.get("searches") or [] if isinstance(s, dict)],
            "pages": {u: p for u, p in (data.get("pages") or {}).items() if isinstance(p, dict)}}


def _save(data: dict, settings: dict):
    cutoff = time.time() - _days(settings) * 86400
    data["searches"] = [s for s in data["searches"] if (s.get("t") or 0) >= cutoff][-MAX_SEARCHES:]
    pages = sorted(((u, p) for u, p in data["pages"].items() if (p.get("t") or 0) >= cutoff), key=lambda kv: kv[1].get("t") or 0)
    data["pages"] = dict(pages[-MAX_PAGES:])
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({"version": 1, **data}, f, ensure_ascii=False)
    atomic_io.replace(tmp, PATH)


def record_search(query: str, results: list, backend: str, settings: dict, mode: str):
    if not enabled(settings, mode):
        return
    keep = [{k: r.get(k) for k in ("title", "url", "snippet", "date")} for r in results[:15]]
    with _lock:
        data = load()
        data["searches"].append({"t": time.time(), "q": str(query)[:300], "backend": backend, "mode": mode, "results": keep})
        try:
            _save(data, settings)
        except OSError:
            pass


def record_page(url: str, title: str, text: str, settings: dict, mode: str):
    if not enabled(settings, mode) or not url:
        return
    with _lock:
        data = load()
        data["pages"][url] = {"t": time.time(), "title": str(title or "")[:300], "text": str(text or "")[:PAGE_CHARS]}
        try:
            _save(data, settings)
        except OSError:
            pass


def clear() -> dict:
    """Delete the saved history. Returns what was removed."""
    with _lock:
        data = load()
        try:
            os.remove(PATH)
        except OSError:
            pass
    return {"searches": len(data["searches"]), "pages": len(data["pages"])}


def stats() -> dict:
    data = load()
    return {"searches": len(data["searches"]), "pages": len(data["pages"]),
            "bytes": os.path.getsize(PATH) if os.path.exists(PATH) else 0}
