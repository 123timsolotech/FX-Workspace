"""
sources.py
==========
The sources of one answer: every search result and page the assistant saw
during a turn gets a stable number, [1], [2] ..., which the model cites
inline. The chat turns [n] into a link with a hover card (title, site,
favicon, date, the quoted passage) and lists the sources under the reply;
agent/sessions.py saves them with the message, so they survive a reload.

  canonical(url)        the address without tracking parameters, fragments,
                        www./m./amp mirrors - one number per page, however
                        many results point at it
  current()             this turn's registry (tasks._run_turn starts a new one)
  Registry.add(...)     a result or a page -> its number
  Registry.for_reply()  the sources a reply cites (or, when it cites none,
                        the pages it read), in number order
"""
from __future__ import annotations

import re
import threading
from urllib.parse import parse_qsl, urlencode, urlparse

import core.state as shared

MAX_SOURCES = 80              # per turn (a Registry's default limit); Research raises it for its depth
CITE_MAX = 999                # [n] is 1-3 digits (_CITE_RE, the page's citation links)
_TRACKING = re.compile(r"^(?:utm_\w+|fbclid|gclid|dclid|msclkid|mc_cid|mc_eid|ref|ref_src|ref_url|igshid|si|"
                       r"spm|_ga|_gl|yclid|oly_\w+|cmpid|ncid|sr_share|feature)$", re.I)
_CITE_RE = re.compile(r"\[(\d{1,3})\](?!\()")


def domain(url: str) -> str:
    host = (urlparse(url or "").hostname or "").lower()
    return re.sub(r"^(?:www\d?|m|mobile|amp)\.", "", host)


def canonical(url: str) -> str:
    """One key for the mirrors of a page: scheme, www./m./amp., tracking
    parameters, fragments, trailing slashes and /amp paths don't count."""
    try:
        p = urlparse((url or "").strip())
    except ValueError:
        return (url or "").strip().lower()
    host = domain(url)
    path = re.sub(r"/(?:amp|index\.html?)/?$", "/", p.path or "/")
    path = re.sub(r"/+$", "", path) or "/"
    query = urlencode(sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=False) if not _TRACKING.match(k)))
    return f"{host}{path}{('?' + query) if query else ''}"


class Registry:
    def __init__(self):
        self.items: list = []
        self._by_key: dict = {}
        self._lock = threading.Lock()
        self.limit = MAX_SOURCES               # Research raises it for its depth (never past CITE_MAX)

    def add(self, url: str, title: str = "", snippet: str = "", date: str = "", read: bool = False,
            excerpt: str = "", note: str = "") -> int:
        """The source's number (a new one, or the page's existing one - updated
        with anything new: a date, a title, the passage read)."""
        key = canonical(url)
        with self._lock:
            n = self._by_key.get(key)
            if n is None:
                if len(self.items) >= min(self.limit, CITE_MAX):
                    return 0
                self.items.append({"n": len(self.items) + 1, "url": url, "title": "", "domain": domain(url),
                                   "snippet": "", "date": "", "read": False, "excerpt": "", "note": ""})
                n = self._by_key[key] = len(self.items)
            item = self.items[n - 1]
            for k, v in (("title", title), ("snippet", snippet), ("date", date), ("excerpt", excerpt), ("note", note)):
                if v and (not item[k] or k in ("excerpt", "date")):
                    item[k] = str(v)[:600 if k == "excerpt" else 300]
            if read:
                item["read"] = True
            return n

    def find(self, url: str) -> int:
        return self._by_key.get(canonical(url), 0)

    def alias(self, url: str, n: int):
        """Another address of source n (where a redirect started or ended)."""
        with self._lock:
            if 0 < n <= len(self.items):
                self._by_key.setdefault(canonical(url), n)

    def get(self, n: int) -> dict | None:
        return self.items[n - 1] if 0 < n <= len(self.items) else None

    def for_reply(self, text: str) -> list:
        """What to show under a reply: the sources it cites, else the pages it read."""
        cited = cited_numbers(text)
        chosen = [self.items[n - 1] for n in sorted(cited) if 0 < n <= len(self.items)]
        if not chosen:
            chosen = [i for i in self.items if i["read"]]
        return [{**i, "cited": i["n"] in cited} for i in chosen]


def cited_numbers(text: str) -> set:
    """The [n] citations in a reply (not markdown links like [text](url))."""
    return {int(m.group(1)) for m in _CITE_RE.finditer(str(text or ""))}


def current() -> Registry:
    reg = shared.app_state.get("turn_sources")
    if not isinstance(reg, Registry):
        reg = shared.app_state["turn_sources"] = Registry()
    return reg


def start_turn() -> Registry:
    reg = shared.app_state["turn_sources"] = Registry()
    return reg
