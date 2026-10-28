"""
url_guard.py
============
Prompt-injection defence for addresses the assistant opens (2026-10-27).

untrusted.py catches content that *says* "ignore your instructions" - but a reworded or disguised injection gets
past any wording check, and the usual thing it asks for is to send the user's data somewhere: "open
https://site/?d=<your saved memory>". Once a request has read outside content (any framed tool result - a page, a
search result, a file, MCP output), an address it opens must have been seen already: in the user's message or in
content read earlier in this request (search results, a page's links). An address put together afterwards - the
only way data can ride along in it - asks first. Links the assistant was shown stay automatic.

An address that carries one of the user's own keys is refused outright, read or not; one that only looks like it
carries a key (token=..., sk-...) asks.
"""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit, urlunsplit

import secret_mask

OUTBOUND = {"fetch_page", "web_read", "browser_open", "new_browser_tab", "download_file"}
_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.I)
_KEY_NAME_RE = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASS)S?$", re.I)

_state = {"urls": set(), "user_hosts": set(), "read": False, "request": None}


def _norm(url: str) -> str:
    url = str(url or "").strip().rstrip(".,;:!?)]}>'\"")
    try:
        p = urlsplit(url)
    except ValueError:
        return url
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path or "/", p.query, ""))


def urls_in(text) -> set:
    return {_norm(u) for u in _URL_RE.findall(str(text or ""))}


def _sync() -> None:
    """Follow the request: every way a request starts (a chat turn, a routine, Research) gives it a fresh
    app_state["untrusted_seen"] list, so a new list means a new request - only what the user wrote is known, and
    nothing is read yet unless the request starts with content (a project brief)."""
    import core.state as shared
    seen = shared.app_state.get("untrusted_seen")
    if seen is None or id(seen) == _state["request"]:
        return
    urls = urls_in(shared.app_state.get("current_user_request") or "")
    _state.update(request=id(seen), urls=set(urls), read=bool(seen),
                  user_hosts={urlsplit(u).netloc for u in urls if urlsplit(u).netloc})


def note_read(text) -> None:
    """Outside content reached the model; the addresses in it may be opened."""
    _sync()
    _state["read"] = True
    _state["urls"] |= urls_in(text)


def _targets(name: str, args) -> list:
    args = args if isinstance(args, dict) else {}
    out = [args.get("url")] if args.get("url") else []
    urls = args.get("urls")
    if isinstance(urls, str):
        urls = [urls]
    out += [u for u in (urls or []) if isinstance(u, str)]
    return [u for u in out if isinstance(u, str) and u.strip()]


def _secrets() -> list:
    return [v for k, v in os.environ.items()
            if _KEY_NAME_RE.search(k) and isinstance(v, str) and len(v) >= 12 and "YOUR_" not in v.upper()]


def check(name: str, args) -> tuple | None:
    """None when the call may go ahead as its permission settings say, else ("ask" | "deny", reason)."""
    if name not in OUTBOUND:
        return None
    _sync()
    targets = _targets(name, args)
    for url in targets:
        if any(s in url for s in _secrets()):
            return "deny", "the address carries one of your API keys"
        if secret_mask.mask(url) != url:
            return "ask", "the address carries what looks like a password or key"
    if not _state["read"]:
        return None
    for url in targets:
        n = _norm(url)
        if n in _state["urls"]:
            continue
        p = urlsplit(n)
        if p.netloc in _state["user_hosts"] and not p.query:
            continue                        # the site the user named, without anything tacked on
        return "ask", ("this address wasn't in your message or in what was read - a page could have put it "
                       f"together to send data out ({p.netloc or url[:60]})")
    return None
