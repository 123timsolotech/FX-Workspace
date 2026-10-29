"""
search_engine.py
================
Web search and page reading for the web_search / fetch_page tools, grounded
answers and Deep Research (agent/research.py).

  Providers - a fallback chain, first answer wins:
      Brave, Tavily     litellm's search integrations, when the user added a
                        key (Settings > Agent > Web; kept in data/search_keys.json
                        or the BRAVE_API_KEY / TAVILY_API_KEY environment variables)
      SearXNG           the user's own instance, when chosen (litellm)
      DuckDuckGo        its HTML results page - no key, so search always works
                        (web_tools._duckduckgo, paced; the assistant's browser
                        when DuckDuckGo refuses and the setting allows it)
      DuckDuckGo IA     litellm's DuckDuckGo integration: the Instant Answer API,
                        an encyclopedia abstract - a last resort, not a web search
  Options - recency (day/week/month/year/any; chosen automatically for
      news-like questions), site (one domain), region, and up to four queries
      at once whose results are merged.
  Results - title, url, snippet, date, domain; mirrors merged (sources.canonical);
      ranked: primary and authoritative sources up, content farms down, the
      site the question is about up, recent dated pages up when the question
      is time-sensitive.
  Resilience - per-provider pacing, timeouts, retries with backoff on
      transient errors, a pause after a rate limit, a 15-minute cache per
      (query, options); Stop is checked between calls; a failure is a result,
      never an exception. API keys are never logged or sent to the page.
Every result and page read gets a number in the turn's sources (sources.py),
which the model cites as [n].
"""
from __future__ import annotations

import contextlib
import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta

import atomic_io
import netlayer
import privacy
import search_history as history
import sources
import web_tools

import core.state as shared

RECENCY = ("any", "day", "week", "month", "year")
MAX_QUERIES = 4
MAX_RESULTS = 15
CACHE_TTL = 15 * 60
PROVIDER_TIMEOUT = 12.0
NAMES = {"brave": "Brave", "tavily": "Tavily", "searxng": "SearXNG", "duckduckgo": "DuckDuckGo",
         "ddg_instant": "DuckDuckGo Instant Answers"}
_GAP = {"brave": 1.1, "tavily": 0.4, "searxng": 0.3, "ddg_instant": 1.0}      # seconds between two calls
_RATE_PAUSE = 60.0                                                          # after a 429
DDG_UNREACHABLE_PAUSE = 60.0                                                # after DuckDuckGo timed out

# DuckDuckGo region codes (kl); anything already like "us-en" passes through.
_REGIONS = {"us": "us-en", "gb": "uk-en", "uk": "uk-en", "ca": "ca-en", "au": "au-en", "in": "in-en", "ie": "ie-en",
            "de": "de-de", "fr": "fr-fr", "es": "es-es", "it": "it-it", "nl": "nl-nl", "jp": "jp-jp", "br": "br-pt",
            "il": "il-he", "mx": "mx-es", "se": "se-sv", "pl": "pl-pl", "kr": "kr-kr", "cn": "cn-zh"}


# ==============================================================================
# API keys (optional upgrades)
# ==============================================================================
class KeyStore:
    """Brave / Tavily keys in data/search_keys.json (not settings.json, which the
    page reads). status() tells the page only whether a key is set and its last
    four characters; the keys themselves never leave this module except in the
    provider's own request header."""
    PROVIDERS = ("brave", "tavily")                       # search providers (the chain)
    FINANCE = ("fred", "finnhub", "alphavantage")         # finance.py's optional data keys - never in the chain
    ALL = PROVIDERS + FINANCE
    ENV = {"brave": "BRAVE_API_KEY", "tavily": "TAVILY_API_KEY", "fred": "FRED_API_KEY",
           "finnhub": "FINNHUB_API_KEY", "alphavantage": "ALPHAVANTAGE_API_KEY"}
    _KEY_RE = re.compile(r"^[A-Za-z0-9_\-.:]{8,200}$")

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return {k: v for k, v in data.items() if k in self.ALL and isinstance(v, str)} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def get(self, provider: str) -> str:
        return self._load().get(provider) or os.environ.get(self.ENV.get(provider, ""), "").strip()

    def set(self, provider: str, key: str):
        """An empty key removes the saved one. Raises ValueError."""
        if provider not in self.ALL:
            raise ValueError("Unknown search provider.")
        key = (key or "").strip()
        if key and not self._KEY_RE.match(key):
            raise ValueError("That doesn't look like an API key (letters, digits and - _ . : only).")
        with self._lock:
            data = self._load()
            if key:
                data[provider] = key
            else:
                data.pop(provider, None)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            atomic_io.replace(tmp, self.path)

    def status(self) -> dict:
        saved = self._load()
        out = {}
        for p in self.ALL:
            key = saved.get(p) or os.environ.get(self.ENV[p], "").strip()
            out[p] = {"set": bool(key), "source": "settings" if saved.get(p) else "environment" if key else "",
                      "hint": ("…" + key[-4:]) if len(key) >= 8 else ""}
        return out

    def secrets(self) -> list:
        return [k for k in (self.get(p) for p in self.ALL) if k]


KEYS = KeyStore(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "search_keys.json"))


def mask(text: str, keys: list) -> str:
    """text with any of the keys replaced - for messages that may echo a request."""
    for k in keys:
        if k:
            text = text.replace(k, "[key hidden]")
    return text


# ==============================================================================
# Query analysis
# ==============================================================================
_NEWS_RE = re.compile(r"\b(news|headlines?|breaking|today'?s?|tonight|yesterday|this (?:morning|week)|right now|live|"
                      r"scores?|election results?|outage|earthquake|just (?:announced|released))\b", re.I)
_DAY_RE = re.compile(r"\b(today'?s?|tonight|yesterday|breaking|right now|live|scores?)\b", re.I)
_FRESH_RE = re.compile(r"\b(latest|newest|current(?:ly)?|recent(?:ly)?|now|this (?:month|year)|upcoming|releases?d?|"
                       r"release date|versions?|prices?|costs?|rates?|stocks?|weather|forecast|updates?d?|changelog|"
                       r"patch notes|deadline|schedule|20[2-3]\d)\b", re.I)


def auto_recency(query: str) -> str:
    """The recency a news-like question needs; "any" otherwise (a hard filter
    on "latest version" could hide the official page - freshness is a ranking
    boost there instead)."""
    if _NEWS_RE.search(query or ""):
        return "day" if _DAY_RE.search(query or "") else "week"
    return "any"


def time_sensitive(query: str) -> bool:
    return bool(_NEWS_RE.search(query or "") or _FRESH_RE.search(query or ""))


def clean_site(site: str) -> str:
    s = re.sub(r"^\w+://", "", str(site or "").strip().lower()).split("/")[0]
    s = re.sub(r"^site:", "", s)
    return s if re.fullmatch(r"[a-z0-9.-]+\.[a-z]{2,}", s) else ""


def clean_region(region: str) -> str:
    r = str(region or "").strip().lower()
    return r if re.fullmatch(r"[a-z]{2}", r) or re.fullmatch(r"[a-z]{2}-[a-z]{2}", r) else ""


_MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def parse_date(text: str, today: date | None = None) -> date | None:
    """A result's date from "2026-08-30", "Aug 30, 2026", "30 August 2026" or "3 days ago"."""
    t = str(text or "").strip().lower()
    today = today or date.today()
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", t)
    try:
        if m:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        m = re.search(r"\b([a-z]{3})[a-z]*\.? (\d{1,2}),? (\d{4})", t)
        if m and m.group(1) in _MONTHS:
            return date(int(m.group(3)), _MONTHS[m.group(1)], int(m.group(2)))
        m = re.search(r"\b(\d{1,2}) ([a-z]{3})[a-z]*\.? (\d{4})", t)
        if m and m.group(2) in _MONTHS:
            return date(int(m.group(3)), _MONTHS[m.group(2)], int(m.group(1)))
    except ValueError:
        return None
    m = re.search(r"\b(\d{1,3}) (minute|hour|day|week|month|year)s? ago", t)
    if m:
        n = int(m.group(1))
        days = {"minute": 0, "hour": 0, "day": n, "week": 7 * n, "month": 30 * n, "year": 365 * n}[m.group(2)]
        return today - timedelta(days=days)
    return None


# ==============================================================================
# Ranking
# ==============================================================================
# Reference and primary sources: official documentation, standards bodies,
# science publishers, encyclopedias and wire services.
_AUTHORITY = {
    "wikipedia.org": .3, "arxiv.org": .5, "nature.com": .5, "science.org": .5, "nih.gov": .5, "who.int": .5,
    "cdc.gov": .5, "europa.eu": .4, "un.org": .4, "w3.org": .5, "ietf.org": .5, "iso.org": .4, "ieee.org": .4,
    "acm.org": .4, "springer.com": .3, "sciencedirect.com": .3, "pubmed.ncbi.nlm.nih.gov": .5,
    "python.org": .5, "developer.mozilla.org": .5, "learn.microsoft.com": .5, "docs.microsoft.com": .5,
    "support.microsoft.com": .4, "github.com": .25, "stackoverflow.com": .15, "create.roblox.com": .5,
    "devforum.roblox.com": .2, "reuters.com": .35, "apnews.com": .35, "bbc.co.uk": .3, "bbc.com": .3, "npr.org": .3,
    "theguardian.com": .25, "nytimes.com": .25, "wsj.com": .25, "ft.com": .25, "economist.com": .25,
    "bloomberg.com": .25, "aljazeera.com": .2, "washingtonpost.com": .25, "arstechnica.com": .2, "theverge.com": .15,
    "britannica.com": .3, "sec.gov": .5, "nasa.gov": .5, "noaa.gov": .5,
}
# Content farms, answer mills, paywalled homework sites and download portals:
# rarely the primary source of a fact.
_LOW = {
    "pinterest.com": -.6, "quora.com": -.35, "answers.com": -.5, "ehow.com": -.4, "reference.com": -.4, "ask.com": -.5,
    "chegg.com": -.5, "coursehero.com": -.5, "scribd.com": -.3, "slideshare.net": -.3, "softonic.com": -.5,
    "filehippo.com": -.4, "uptodown.com": -.4, "malavida.com": -.5, "wikihow.com": -.15, "brainly.com": -.5,
    "studocu.com": -.4, "bartleby.com": -.4, "facebook.com": -.35, "instagram.com": -.4, "tiktok.com": -.4,
    "x.com": -.2, "twitter.com": -.2, "linkedin.com": -.2, "yahoo.com": -.1, "msn.com": -.1, "releasebytes.com": -.2,
    "alternativeto.net": -.2, "g2.com": -.15, "capterra.com": -.15,
}
_SEO_TITLE_RE = re.compile(r"\b(top \d+|\d+ best|best .* \(?(?:updated|20\d\d)\)?|ultimate guide|you won'?t believe|"
                           r"must[- ]know|free download|cheap|coupon|promo code)\b", re.I)
_OFFICIAL_HOST_RE = re.compile(r"^(?:docs|developer|developers|learn|support|help|api|dev|devblogs|blog)\.", re.I)


def _suffix_score(host: str, table: dict) -> float:
    parts = host.split(".")
    for i in range(len(parts) - 1):
        if ".".join(parts[i:]) in table:
            return table[".".join(parts[i:])]
    return 0.0


def authority(url: str, queries: list) -> float:
    host = sources.domain(url)
    score = _suffix_score(host, _AUTHORITY) + _suffix_score(host, _LOW)
    if re.search(r"\.(?:gov|mil|edu)(?:\.[a-z]{2})?$|\.ac\.[a-z]{2}$|\.gov\.[a-z]{2}$", host):
        score += .5
    if _OFFICIAL_HOST_RE.match(host) or re.search(r"/(?:docs|documentation|reference|manual|api)(?:/|$)", url, re.I):
        score += .25
    # The site the question is about is its primary source ("roblox studio api" -> roblox.com).
    labels = set(host.split(".")[:-1])
    words = {w for q in queries for w in re.findall(r"[a-z0-9]{4,}", q.lower())}
    if labels & words:
        score += .35
    return score


# ==============================================================================
# Deep search (1.4.4, part B): more phrasings, more kinds of source, one fused ranking
# ==============================================================================
RRF_K = 60                      # reciprocal rank fusion's constant (the common choice in the literature)
# Web searches per Deep round: DuckDuckGo refused (and then dropped this PC's address for hours) when an evaluation
# sent six in a row per question, even over Tor (29 Oct 2026). More phrasings go to the forum APIs, which don't.
DEEP_WEB_QUERIES = 3
# A forum / Q&A source list's vote against a web list's. Tuned on tools/search_eval (18 questions, 29 Oct 2026): at 1.0
# the six-odd source lists crowded good web results out of the top 10 (7.4 -> 5.2 matching); at 0.35 every question had
# an answer in the top 5 and Deep the most matching results (7.4) - lower changed nothing.
SOURCE_WEIGHT = 0.35
EXTRA_FIELDS = ("kind", "source", "points", "answered", "comments", "site")
_STOP = set("a an the of to in on for and or is are was were be been do does did how what why when where which who "
            "can could should would will my me i you it this that with from by at as about any anyone there their "
            "get got have has had not no yes so if than then into out up".split())
_QUOTE_RE = re.compile(r'"([^"]{4,120})"|\'([^\']{8,120})\'')
_ERRORISH_RE = re.compile(r"((?:[A-Z]\w+(?:Error|Exception))[:\s][^.\n]{0,100}|error[:\s][^.\n]{4,100}|0x[0-9a-fA-F]{6,}|"
                          r"E\d{3,5}\b[^.\n]{0,60})")


def keywords(text: str, n: int = 8) -> str:
    """A question as search keywords: its meaningful words, in order."""
    words = [w for w in re.findall(r"[\w.+#/-]+", text or "") if w.lower() not in _STOP and len(w) > 1]
    return " ".join(words[:n])


def fanout(question: str, queries: list | None = None, limit: int = 6) -> list:
    """The searches for one question, of different kinds (docs/plans/1.4.4.md, B2): the model's own phrasings, the
    question as keywords, the exact phrase (an error message, a quoted line), and - for experience and error
    questions - the forum-shaped searches where fixes are written up."""
    import deep_sources
    q = re.sub(r"\s+", " ", question or "").strip()
    out = [x for x in [q] + list(queries or []) if x]
    core = keywords(q)
    if core and core.lower() != q.lower() and len(q.split()) > 6:      # a short question is its own keywords
        out.append(core)
    m = _QUOTE_RE.search(q) or _ERRORISH_RE.search(q)
    if m:
        phrase = (m.group(1) or (m.group(2) if m.lastindex and m.lastindex >= 2 else "") or m.group(0)).strip(" :.")
        if phrase:
            out.append(f'"{phrase[:100]}"')
    if deep_sources._ERROR_RE.search(q) or deep_sources._EXPERIENCE_RE.search(q):
        short = keywords(q, 6)
        out += [f"{short} forum", f"{short} solved" if deep_sources._ERROR_RE.search(q) else f"{short} reddit"]
    seen, uniq = set(), []
    for x in out:
        if x.lower() not in seen:
            seen.add(x.lower())
            uniq.append(x)
    return uniq[:limit]


def fuse(lists: list, k: int = RRF_K, weights: list | None = None) -> list:
    """Reciprocal rank fusion: every ranked list (one per query and source) votes weight/(k + rank) for each result;
    a page several searches agree on rises. weights: one per list (default 1). Mirrors fold together
    (sources.canonical). Returns the merged results in fused order, each with "rrf" and "hits" (lists that found it)."""
    merged: dict = {}
    for li, lst in enumerate(lists):
        w = weights[li] if weights and li < len(weights) else 1.0
        for rank_i, r in enumerate(lst or []):
            url = str(r.get("url") or "").strip()
            if not web_tools.is_web_url(url):
                continue
            key = sources.canonical(url)
            m = merged.get(key)
            if m is None:
                m = merged[key] = {"title": r.get("title") or url, "url": url, "snippet": r.get("snippet") or "",
                                   "date": r.get("date") or "", "rrf": 0.0, "hits": 0,
                                   **{f: r[f] for f in EXTRA_FIELDS if r.get(f) not in (None, "")}}
            else:
                if len(r.get("snippet") or "") > len(m["snippet"]):
                    m["snippet"] = r.get("snippet") or ""
                for f in EXTRA_FIELDS:                      # a forum source knows what a web result didn't
                    if r.get(f) not in (None, "") and f not in m:
                        m[f] = r[f]
            m["rrf"] += w / (k + rank_i + 1)
            m["hits"] += 1
            m["date"] = m["date"] or r.get("date") or ""
    return sorted(merged.values(), key=lambda m: m["rrf"], reverse=True)


RERANK_SYSTEM = ("You rank search results for a question. Given the question and numbered results (title, site, "
                 "kind, snippet), return JSON {\"order\": [numbers]} - the results most likely to contain a correct, "
                 "specific answer first. Prefer: the page that answers this exact question; primary sources and "
                 "official docs; forum or Q&A threads where someone reports the same problem fixed; recent pages for "
                 "time-sensitive questions. Push down: generic listicles, SEO pages, pages about something else. "
                 "Include every number once.")


def rerank(question: str, results: list, ask=None, top: int = 30, batch: int = 15) -> list:
    """The fused list's top results re-ordered by the model, in batches (B4). ask(system, user) -> dict or None; when
    the model doesn't answer, the order stays as it was. The rest keep their place after the re-ranked ones."""
    if ask is None:
        from agent import models as llm
        ask = lambda s, u: llm.model_json(s, u, max_tokens=300)            # noqa: E731
    head, tail = results[:top], results[top:]
    out = []
    for i in range(0, len(head), batch):
        part = head[i:i + batch]
        listing = "\n".join(f"[{j + 1}] {r['title']} - {sources.domain(r['url'])}" + (f" · {r['kind']}" if r.get("kind") else "")
                            + (f"\n    {r['snippet'][:220]}" if r.get("snippet") else "") for j, r in enumerate(part))
        got = None
        try:
            got = ask(RERANK_SYSTEM, f"Question: {question}\n\nResults:\n{listing}")
        except Exception:
            got = None
        order = [int(x) - 1 for x in (got or {}).get("order") or [] if str(x).isdigit() and 0 < int(x) <= len(part)]
        order = list(dict.fromkeys(order)) + [j for j in range(len(part)) if j not in order]
        out += [part[j] for j in order]
    return out + tail


_KIND_WORDS = {"forum": "forum thread", "q&a": "Q&A", "issue": "issue tracker", "paper": "paper", "reference": "encyclopedia"}


def _thread_note(r: dict) -> str:
    """" · Q&A · accepted answer · score 42" - what a forum or Q&A result is, so the model can weigh it (B6)."""
    if r.get("kind") not in _KIND_WORDS:
        return ""
    bits = [_KIND_WORDS[r["kind"]]]
    if r.get("answered"):
        bits.append("closed" if r["kind"] == "issue" else "accepted answer" if r["kind"] == "q&a" else "solved")
    if r.get("points"):
        bits.append(f"score {r['points']}")
    if r.get("comments"):
        bits.append(f"{r['comments']} {'answers' if r['kind'] == 'q&a' else 'replies'}")
    return " · " + " · ".join(bits)


def rank(results: list, queries: list, sensitive: bool, site: str = "", today: date | None = None) -> list:
    """Merge mirrors and rank: position, repeated hits across queries, authority,
    low-quality and SEO-bait penalties, and freshness for time-sensitive questions."""
    today = today or date.today()
    merged: dict = {}
    for pos, r in enumerate(results):
        url = str(r.get("url") or "").strip()
        if not web_tools.is_web_url(url):
            continue
        key = sources.canonical(url)
        if key in merged:
            m = merged[key]
            m["hits"] += r.get("hits") or 1
            if len(r.get("snippet") or "") > len(m["snippet"]):
                m["snippet"] = r.get("snippet") or ""
            m["date"] = m["date"] or r.get("date") or ""
            continue
        merged[key] = {"title": r.get("title") or url, "url": url, "snippet": r.get("snippet") or "",
                       "date": r.get("date") or "", "domain": sources.domain(url), "pos": pos, "hits": r.get("hits") or 1,
                       **{f: r[f] for f in EXTRA_FIELDS if r.get(f) not in (None, "")}}
    # The same title on the same site (print / mobile / localized copies) is one result.
    seen_titles: dict = {}
    for key, m in list(merged.items()):
        tkey = (m["domain"], re.sub(r"\W+", " ", m["title"].lower()).strip())
        if tkey in seen_titles and tkey[1]:
            other = merged[seen_titles[tkey]]
            other["hits"] += m["hits"]
            del merged[key]
        else:
            seen_titles[tkey] = key
    out = []
    for m in merged.values():
        d = parse_date(m["date"] or m["snippet"][:40], today)
        m["date"] = d.isoformat() if d else ""
        score = 1.0 / (1 + .12 * m["pos"]) + .15 * min(m["hits"] - 1, 4) + authority(m["url"], queries)
        if _SEO_TITLE_RE.search(m["title"]):
            score -= .15
        if site and not (m["domain"] == site or m["domain"].endswith("." + site)):
            score -= .5
        if sensitive:
            if d:
                age = max(0, (today - d).days)
                score += .5 * (1 - min(age, 365) / 365)
            else:
                score -= .1
        m["score"] = round(score, 3)
        out.append(m)
    out.sort(key=lambda m: m["score"], reverse=True)
    return out


# What kind of source a site is - Bias check shows it beside each result, so the model can weigh trust.
_KINDS = (
    ("social media", ("x.com", "twitter.com", "facebook.com", "instagram.com", "tiktok.com", "threads.net", "t.me",
                      "reddit.com", "youtube.com", "linkedin.com", "pinterest.com", "truthsocial.com", "bsky.app")),
    ("forum / Q&A", ("quora.com", "stackexchange.com", "stackoverflow.com", "answers.com", "brainly.com")),
    ("encyclopedia", ("wikipedia.org", "britannica.com")),
    ("research / academic", ("arxiv.org", "nature.com", "science.org", "pubmed.ncbi.nlm.nih.gov", "springer.com",
                             "sciencedirect.com", "acm.org", "ieee.org", "ssrn.com", "jstor.org")),
    ("news agency / major outlet", ("reuters.com", "apnews.com", "bbc.co.uk", "bbc.com", "npr.org", "nytimes.com",
                                    "wsj.com", "ft.com", "economist.com", "bloomberg.com", "theguardian.com",
                                    "washingtonpost.com", "aljazeera.com", "cnn.com", "foxnews.com", "afp.com")),
    ("fact-checker", ("snopes.com", "politifact.com", "factcheck.org", "fullfact.org")),
)


def source_kind(url: str) -> str:
    host = sources.domain(url)
    for kind, sites in _KINDS:
        if any(host == s or host.endswith("." + s) for s in sites):
            return kind
    if re.search(r"\.(?:gov|mil)(?:\.[a-z]{2})?$|\.gov\.[a-z]{2}$|(?:^|\.)(?:who\.int|un\.org|europa\.eu)$", host):
        return "government / official"
    if re.search(r"\.edu(?:\.[a-z]{2})?$|\.ac\.[a-z]{2}$", host):
        return "research / academic"
    if _OFFICIAL_HOST_RE.match(host):
        return "official docs"
    return "website / blog"


def low_quality(result: dict) -> bool:
    return _suffix_score(result.get("domain") or sources.domain(result.get("url", "")), _LOW) <= -.3


# ==============================================================================
# Providers
# ==============================================================================
_pace_lock = threading.Lock()
_last_call: dict = {}
_paused_until: dict = {}
_cache: dict = {}


class _Refused(Exception):
    """The provider refused (bad key, rate limit): try the next one, don't retry this one."""


def reset():
    """Forget pacing, pauses and cached searches (tests)."""
    with _pace_lock:
        _last_call.clear()
        _paused_until.clear()
    _cache.clear()


def _pace(provider: str):
    gap = _GAP.get(provider, 0)
    with _pace_lock:
        wait = _last_call.get(provider, 0) + gap - time.monotonic()
        _last_call[provider] = time.monotonic() + max(0.0, wait)
    if wait > 0:
        time.sleep(wait)


class ProviderError(Exception):
    """A search API answered with an error status (its status_code decides retry / skip / pause)."""

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _raise_for(resp, name: str):
    if resp.status_code == 429:
        raise ProviderError(429, f"{name}: 429 Too Many Requests")
    if resp.status_code in (401, 402, 403):
        raise ProviderError(resp.status_code, f"{name}: {resp.status_code} unauthorized - the key or the instance refused")
    if resp.status_code >= 400:
        raise ProviderError(resp.status_code, f"{name} answered {resp.status_code}")


def searxng_request(base: str, query: str, recency: str = "any", region: str = "", client=None) -> list:
    """The user's SearXNG through its JSON API (/search?format=json), not by scraping its page.
    A 403, or HTML instead of JSON, means the instance has JSON output turned off."""
    params = {"q": query, "format": "json"}
    if recency != "any":
        params["time_range"] = recency
    if region:
        params["language"] = region
    own = client is None
    client = client or netlayer.client("search", timeout=PROVIDER_TIMEOUT)
    try:
        resp = client.get(base.rstrip("/") + "/search", params=params, headers={"Accept": "application/json"})
    finally:
        if own:
            client.close()
    if resp.status_code == 403:
        raise ProviderError(403, "SearXNG refused the JSON API: the instance has JSON output turned off. Add json to "
                                 "search: formats: [html, json] in its settings.yml and restart it.")
    _raise_for(resp, "SearXNG")
    try:
        data = resp.json()
    except ValueError:
        raise ProviderError(415, "SearXNG answered with a web page, not JSON - its JSON output is probably turned off "
                                 "(search: formats: [html, json] in settings.yml).")
    return [{"title": r.get("title") or "", "url": r.get("url") or "", "snippet": r.get("content") or "",
             "date": str(r.get("publishedDate") or "")[:25]} for r in (data.get("results") or []) if isinstance(r, dict)]


def _api_search(provider: str, query: str, n: int, recency: str, site: str, region: str,
                key: str = "", api_base: str = "") -> list:
    """One request to a search API (Brave, Tavily), the user's SearXNG or DuckDuckGo's
    Instant Answers - through the network layer (netlayer.py: route, kill switch, log)."""
    q = f"{query} site:{site}" if site and provider in ("brave", "searxng") else query
    with netlayer.client("search", timeout=PROVIDER_TIMEOUT) as c:
        if provider == "searxng":
            return searxng_request(api_base, q, recency, region, client=c)
        if provider == "brave":
            params = {"q": q, "count": min(n, 20)}
            if recency != "any":
                params["freshness"] = {"day": "pd", "week": "pw", "month": "pm", "year": "py"}[recency]
            if region:
                params["country"] = region[:2].upper()
            resp = c.get("https://api.search.brave.com/res/v1/web/search", params=params,
                         headers={"Accept": "application/json", "X-Subscription-Token": key})
            _raise_for(resp, "Brave")
            items = (resp.json().get("web") or {}).get("results") or []
            return [{"title": i.get("title") or "", "url": i.get("url") or "", "snippet": i.get("description") or "",
                     "date": str(i.get("page_age") or i.get("age") or "")[:25]} for i in items if isinstance(i, dict)]
        if provider == "tavily":
            body = {"query": q, "max_results": min(n, 20)}
            if recency != "any":
                body["time_range"] = recency
                if recency in ("day", "week"):
                    body["topic"] = "news"
            if site:
                body["include_domains"] = [site]
            resp = c.post("https://api.tavily.com/search", json=body, headers={"Authorization": f"Bearer {key}"})
            _raise_for(resp, "Tavily")
            return [{"title": i.get("title") or "", "url": i.get("url") or "", "snippet": i.get("content") or "",
                     "date": str(i.get("published_date") or "")[:25]} for i in (resp.json().get("results") or [])
                    if isinstance(i, dict)]
        if provider == "ddg_instant":
            resp = c.get("https://api.duckduckgo.com/", params={"q": q, "format": "json", "no_html": 1, "skip_disambig": 1})
            _raise_for(resp, "DuckDuckGo Instant Answers")
            data = resp.json()
            out = []
            if data.get("AbstractURL"):
                out.append({"title": data.get("Heading") or query, "url": data["AbstractURL"],
                            "snippet": data.get("AbstractText") or "", "date": ""})
            for t in data.get("RelatedTopics") or []:
                if isinstance(t, dict) and t.get("FirstURL"):
                    out.append({"title": (t.get("Text") or "")[:90], "url": t["FirstURL"], "snippet": t.get("Text") or "",
                                "date": ""})
            return out[:n]
    raise ProviderError(400, f"Unknown provider {provider}")


def _classify_error(e: Exception) -> str:
    """'refused' (key / quota - skip this provider), 'rate' (pause it) or 'transient' (retry)."""
    status = getattr(e, "status_code", None)
    name = type(e).__name__
    text = str(e).lower()
    if status == 429 or name == "RateLimitError" or "rate limit" in text or "too many requests" in text:
        return "rate"
    if status in (401, 402, 403) or name in ("AuthenticationError", "PermissionDeniedError") or "invalid api key" in text \
            or "unauthorized" in text or "subscription" in text:
        return "refused"
    if status and 400 <= status < 500:
        return "refused"
    return "transient"


def _run_provider(provider: str, query: str, n: int, recency: str, site: str, region: str, settings: dict,
                  keys: KeyStore, browser_fallback: bool, should_cancel) -> tuple[list, str]:
    """(results, why none) from one provider, with retries on transient errors."""
    now = time.monotonic()
    if _paused_until.get(provider, 0) > now:
        return [], f"{NAMES[provider]} is paused after a rate limit ({int(_paused_until[provider] - now)} s more)"
    if provider == "duckduckgo":
        q = f"{query} site:{site}" if site else query
        df = {"day": "d", "week": "w", "month": "m", "year": "y"}.get(recency, "")
        kl = _REGIONS.get(region, region if "-" in region else "")
        try:
            results, why = web_tools._duckduckgo(q, df, kl)
        except web_tools.BlockedAddress as e:
            return [], str(e)
        except netlayer.RouteBlocked:
            raise                                    # the kill switch: no provider may go around it
        except Exception as e:                       # httpx errors: DuckDuckGo unreachable
            results, why = [], f"DuckDuckGo could not be reached ({str(e)[:120]})"
            # Unreachable is how a block often looks (it drops the connection): skip it for a minute instead of
            # every search waiting ~20 s for it to time out again (1.4.4) - its Instant Answers too.
            web_tools._ddg["until"] = max(web_tools._ddg["until"], time.monotonic() + DDG_UNREACHABLE_PAUSE)
            _paused_until["ddg_instant"] = time.monotonic() + DDG_UNREACHABLE_PAUSE
        if not results and why and browser_fallback and not should_cancel():
            found, engine, trouble = web_tools._browser_search(q)
            if found:
                return web_tools._clean_results(found), ""
            why += "; " + trouble
        return web_tools._clean_results(results), why
    key = keys.get(provider) if provider in KeyStore.PROVIDERS else ""
    api_base = str(settings.get("searxng_url") or "") if provider == "searxng" else ""
    last = ""
    for attempt in range(3):
        if should_cancel():
            return [], "stopped by you"
        _pace(provider)
        try:
            return _api_search(provider, query, n, recency, site, region, key, api_base), ""
        except netlayer.RouteBlocked:
            raise
        except Exception as e:
            kind = _classify_error(e)
            last = mask(str(e), keys.secrets())[:200]
            if kind == "rate":
                _paused_until[provider] = time.monotonic() + _RATE_PAUSE
                return [], f"{NAMES[provider]} rate-limited this search"
            if kind == "refused":
                hint = (" - check the key in Settings > Web Search" if provider in KeyStore.PROVIDERS else "")
                return [], f"{NAMES[provider]} refused the search ({last[:120]}){hint}"
            if attempt < 2:
                time.sleep(0.7 * (2 ** attempt))
    return [], f"{NAMES[provider]} failed ({last})"


def chain(settings: dict, keys: KeyStore, mode: str = "standard") -> list:
    """The providers to try, in order, for a privacy mode (MODES).
    Private prefers privacy-respecting backends (the user's SearXNG, Brave,
    DuckDuckGo) and leaves out Tavily; Maximum Privacy uses only the user's
    SearXNG - there is no chain to fall down silently."""
    if mode == "max":
        return ["searxng"] if settings.get("searxng_url") else []
    if mode in ("local", "disabled"):
        return []
    keyed = [p for p in KeyStore.PROVIDERS if keys.get(p)]
    if mode == "private":
        order = (["searxng"] if settings.get("searxng_url") else []) + (["brave"] if "brave" in keyed else [])
        return order + ["duckduckgo", "ddg_instant"]
    pref = settings.get("web_search_provider") or "duckduckgo"
    order = []
    if pref == "searxng" and settings.get("searxng_url"):
        order.append("searxng")
    if pref in keyed:
        order.append(pref)
    order += [p for p in keyed if p not in order]
    return order + ["duckduckgo", "ddg_instant"]


def cache_ttl(settings: dict, mode: str) -> float:
    """Seconds a search's results are reused: the setting (0 = never); never in Maximum Privacy."""
    if mode == "max":
        return 0.0
    try:
        return max(0.0, min(float(settings.get("search_cache_minutes", CACHE_TTL / 60)), 24 * 60)) * 60
    except (TypeError, ValueError):
        return CACHE_TTL


def search(query: str = "", *, queries: list | None = None, num_results: int = 8, recency: str = "auto",
           site: str = "", region: str = "", settings: dict | None = None, keys: KeyStore | None = None,
           browser_fallback: bool = False, should_cancel=None, progress=None, mode: str = "standard",
           extra_lists: list | None = None, max_queries: int = MAX_QUERIES) -> dict:
    """Up to MAX_QUERIES queries through the provider chain of a privacy mode,
    merged and ranked. Local Only searches this PC (local_search); a request the
    network layer blocked (kill switch, no VPN) stops the search - nothing goes
    around it. progress(web={...}) hears which query is being searched.
    -> {"results", "queries", "providers", "recency", "sensitive", "why", "cached", "blocked", "mode"}"""
    settings = settings or {}
    keys = keys or KEYS
    should_cancel = should_cancel or (lambda: False)
    mode = mode if mode in MODES else "standard"
    qs = []
    for q in [query] + list(queries or []):
        q = re.sub(r"\s+", " ", str(q or "")).strip()[:300]
        if q and q.lower() not in {x.lower() for x in qs}:
            qs.append(q)
    qs = qs[:max(1, min(int(max_queries or MAX_QUERIES), 8))]
    n = max(1, min(int(num_results or 8), MAX_RESULTS))
    site, region = clean_site(site), clean_region(region)
    sensitive = any(time_sensitive(q) for q in qs)
    recency = recency if recency in RECENCY else (auto_recency(qs[0]) if qs else "any")
    if mode in ("local", "disabled"):
        found = local_search(qs, n) if mode == "local" and qs else []
        return {"results": found, "queries": qs, "providers": ["This PC"] if mode == "local" else [], "recency": recency,
                "sensitive": sensitive, "why": [] if found or mode == "local" else ["web search is disabled"],
                "cached": 0, "blocked": None, "mode": mode}
    order = chain(settings, keys, mode)
    ttl = cache_ttl(settings, mode)
    per_query, providers, why, cached, blocked = [], [], [], 0, None
    for i, q in enumerate(qs):
        if should_cancel():
            why.append("stopped by you")
            break
        if progress:
            progress(web={"op": "search", "queries": qs, "current": i, "recency": recency})
        key = (tuple(order), q.lower(), recency, site, region, n)
        hit = None if privacy.active() or not ttl else _cache.get(key)   # a temporary chat bypasses the cache
        if hit and time.monotonic() - hit[0] < ttl:
            per_query.append(hit[1])
            providers.append(hit[2])
            cached += 1
            continue
        got, used = [], ""
        try:
            for provider in order:
                if should_cancel():
                    break
                results, problem = _run_provider(provider, q, n, recency, site, region, settings, keys,
                                                 browser_fallback, should_cancel)
                results = [r for r in results if web_tools.is_web_url(str(r.get("url") or ""))]
                if results:
                    got, used = results, NAMES[provider]
                    break
                if problem:
                    why.append(problem)
        except netlayer.RouteBlocked as e:
            blocked = {"reason": str(e), "kind": e.kind}
            why.append(str(e))
            per_query.append([])
            providers.append("")
            break
        if not order:
            why.append("Maximum Privacy needs your own SearXNG instance - set its address in Settings > Web Search"
                       if mode == "max" else "no search backend")
        if got and ttl and not privacy.active():
            _cache[key] = (time.monotonic(), got, used)
            for k in [k for k, v in _cache.items() if time.monotonic() - v[0] > ttl]:
                _cache.pop(k, None)
        if got:
            history.record_search(q, got, used, settings, mode)
        per_query.append(got)
        providers.append(used)
    # Every query's list - and every forum or Q&A source's (deep_sources) - fused by reciprocal rank (1.4.4): a page
    # several of them found rises; each list's best still comes early.
    extra_lists = list(extra_lists or [])
    merged = fuse(per_query + extra_lists, weights=[1.0] * len(per_query) + [SOURCE_WEIGHT] * len(extra_lists))
    ranked = rank(merged, qs, sensitive, site)
    lists = len(qs) + len(extra_lists or [])
    limit = min(MAX_RESULTS, n if lists == 1 else max(n, min(MAX_RESULTS, n + 2 * (lists - 1))))
    return {"results": ranked[:limit], "queries": qs, "providers": sorted({p for p in providers if p}),
            "recency": recency, "sensitive": sensitive, "why": list(dict.fromkeys(why)), "cached": cached,
            "blocked": blocked, "mode": mode}


# ==============================================================================
# Privacy modes (Settings > Web Search)
# ==============================================================================
MODES = ("standard", "private", "max", "local", "disabled")
MODE_INFO = {
    "standard": {"label": "Standard", "short": "Standard",
                 "protects": "Nothing beyond your network route: it's tuned for the best results.",
                 "leaves": "Your search words go to the search engine (DuckDuckGo, or the provider you chose), and pages "
                           "are read with a normal browser signature and cookies for that one request."},
    "private": {"label": "Private", "short": "Private",
                "protects": "No cookies or sign-ins, no Referer, a generic browser signature, tracking parameters "
                            "removed from links, and personal details (file paths, your name, IDs, e-mail addresses, "
                            "the project's name) taken out of each query before it's sent. Searches aren't kept "
                            "unless you turn history on.",
                "leaves": "The cleaned search words still go to the search engine, and every page you read sees a "
                          "visit from your route's address (your own, a proxy's or a Tor exit's)."},
    "max": {"label": "Maximum Privacy", "short": "Max Privacy",
            "protects": "Everything in Private, plus: only your own SearXNG instance (its JSON API), all web traffic "
                        "through Tor or your proxy with names resolved by the proxy, no search history, no cache - "
                        "and never a silent fallback: if it fails, you're asked before anything less private is used.",
            "leaves": "Your SearXNG instance still sends the cleaned query to the engines it uses, and the pages you "
                      "read see your proxy's or Tor exit's address. This is not 'completely private'."},
    "local": {"label": "Local Only", "short": "Local Only",
              "protects": "The only mode where queries never leave this PC: the search tools can't open a network "
                          "connection at all (the network layer refuses them).",
              "leaves": "Nothing - which also means no new information from the live web. It searches pages read "
                        "before, your saved search history (if on), the open project's files and notes, and memory."},
    "disabled": {"label": "Disabled", "short": "Search off",
                 "protects": "The model gets no web search, page-reading or browser tools at all.",
                 "leaves": "Nothing from search. (Cloud models and other features have their own network use.)"},
}


def turn_mode(settings: dict | None = None) -> str:
    """This message's search mode: the composer's per-message override, else the setting."""
    override = shared.app_state.get("turn_search_mode")
    if override in MODES:
        return override
    s = settings if settings is not None else (shared.app_state.get("settings") or {})
    return s.get("search_mode") if s.get("search_mode") in MODES else "standard"


_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|\\\\[\w.-]+\\|~[/\\]|/(?:home|Users|users|mnt|var|etc|tmp|root)/)[^\s\"'<>|]*")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_ID_RE = re.compile(r"\btmp-[0-9a-f]{12}\b|\b[0-9a-f]{12,}\b", re.I)
_LONG_NUMBER_RE = re.compile(r"(?<![\w.])\+?\d[\d -]{7,}\d\b")
_URL_QUERY_RE = re.compile(r"(https?://[^\s?#]+)[?#][^\s]*")
MAX_QUERY_WORDS = 24


def sanitize_query(query: str, extra: list | None = None) -> tuple[str, list]:
    """The query as Private / Maximum Privacy send it: file paths, e-mail
    addresses, chat and other ids, long numbers, URL parameters and the given
    personal terms (the user's name, the Windows account and PC name, the open
    project's name and folder) removed; at most MAX_QUERY_WORDS words. This runs
    between the model and the network - not a request to the model.
    -> (clean query, what was removed - kinds, never the values)"""
    q = str(query or "")
    removed = []
    for kind, pattern in (("a file path", _PATH_RE), ("an e-mail address", _EMAIL_RE), ("an id", _ID_RE),
                          ("a long number", _LONG_NUMBER_RE)):
        if pattern.search(q):
            q = pattern.sub(" ", q)
            removed.append(kind)
    if _URL_QUERY_RE.search(q):
        q = _URL_QUERY_RE.sub(r"\1", q)
        removed.append("link parameters")
    for term in extra or []:
        term = str(term or "").strip()
        if len(term) >= 3 and re.search(r"(?<!\w)" + re.escape(term) + r"(?!\w)", q, re.I):
            q = re.sub(r"(?<!\w)" + re.escape(term) + r"(?!\w)", " ", q, flags=re.I)
            removed.append("a personal name or project name")
    words = q.split()
    if len(words) > MAX_QUERY_WORDS:
        words = words[:MAX_QUERY_WORDS]
        removed.append("words past the first 24")
    return " ".join(words).strip(" -,:;"), list(dict.fromkeys(removed))


def personal_terms(settings: dict | None = None) -> list:
    """What must never be in a Private query: the user's name, the Windows account and PC name, the open project."""
    s = settings if settings is not None else (shared.app_state.get("settings") or {})
    project = shared.app_state.get("active_project") if isinstance(shared.app_state.get("active_project"), dict) else {}
    terms = [s.get("user_name"), os.environ.get("USERNAME"), os.environ.get("COMPUTERNAME"),
             (project or {}).get("name"), os.path.basename(str((project or {}).get("folder") or "").rstrip("\\/"))]
    return [t for t in terms if t and len(str(t)) >= 3]


# ==============================================================================
# Local Only: what this PC already has
# ==============================================================================
_LOCAL_STOP = set("the and for with that this from what when where which who how why does are was were can could "
                  "should would will about into than then there their your you our they them its not".split())


def _terms(text: str) -> set:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9_+#.-]{2,}", str(text or "").lower()) if w not in _LOCAL_STOP}


def _best_line(text: str, terms: set) -> str:
    best, score = "", 0
    for line in re.split(r"(?<=[.!?])\s+|\n+", str(text or "")):
        s = len(terms & _terms(line))
        if s > score:
            best, score = line.strip(), s
    return re.sub(r"\s+", " ", best or str(text or "")[:200])[:280]


def local_search(queries: list, n: int = 8) -> list:
    """Local Only: pages read before (in memory, and the saved history if on),
    saved search results, the open project's notes and files, and memory -
    scored by the query's words. Never touches the network."""
    terms = set().union(*(_terms(q) for q in queries)) if queries else set()
    if not terms:
        return []
    candidates = []                                    # (kind label, url, title, text)
    for url, (_t, got) in list(web_tools._page_cache.items()):
        page = (got or {}).get("page") or {}
        candidates.append(("page read earlier", url, page.get("title") or url, page.get("text") or ""))
    saved = history.load()
    for url, p in saved["pages"].items():
        candidates.append(("saved page", url, p.get("title") or url, p.get("text") or ""))
    for s in saved["searches"]:
        for r in s.get("results") or []:
            candidates.append(("saved search result", r.get("url") or "", r.get("title") or "", r.get("snippet") or ""))
    project = shared.app_state.get("active_project") if isinstance(shared.app_state.get("active_project"), dict) else None
    if project:
        notes = "\n".join(str(project.get(k) or "") for k in ("instructions", "notes"))
        if notes.strip():
            candidates.append(("project notes", f"project:{project.get('id')}", f"{project.get('name')} - notes", notes))
        folder = project.get("folder")
        if folder:
            try:
                import projects
                index = shared.folder_index.get(folder)
                for s in projects._snippets(index, " ".join(queries), 6000, max_files=5) if index else []:
                    path = os.path.join(folder, s["path"])
                    candidates.append(("project file", "file:///" + path.replace("\\", "/"), f"{s['path']} (lines {s['lines']})",
                                       s["text"]))
            except Exception:
                pass
    try:
        for e in shared.memory_store.list():
            candidates.append(("saved memory", f"memory:{e['id']}", "Saved memory", e["text"]))
    except Exception:
        pass
    scored, seen = [], set()
    for kind, url, title, text in candidates:
        if not url or url in seen:
            continue
        hit_title, hit_text = len(terms & _terms(title)), len(terms & _terms(text))
        if not hit_title and not hit_text:
            continue
        seen.add(url)
        scored.append((hit_title * 2 + hit_text, kind, url, title, text))
    scored.sort(key=lambda s: -s[0])
    return [{"title": title[:160], "url": url, "snippet": _best_line(text, terms), "date": "", "domain": kind,
             "local": True, "score": score} for score, kind, url, title, text in scored[:n]]


def local_pages(urls: list) -> list:
    """fetch_page in Local Only: a page only if it was read before (memory or the saved history)."""
    saved = history.load()["pages"]
    out = []
    for u in urls:
        target = web_tools._normalize_url(u)
        cached = web_tools._page_cache.get(target)
        if cached:
            out.append({"url": u, **cached[1], "note": "(A copy read earlier - Local Only doesn't go online.)"})
        elif target in saved:
            p = saved[target]
            out.append({"url": u, "final": target, "note": "(A saved copy - Local Only doesn't go online.)",
                        "page": {"title": p.get("title") or target, "text": p.get("text") or "", "links": [], "date": ""}})
        else:
            out.append({"url": u, "error": "Not read: Local Only search mode doesn't go online, and this page wasn't "
                                           "read before."})
    return out


# ==============================================================================
# The tools
# ==============================================================================
@contextlib.contextmanager
def scope(mode: str):
    """The network rules of a search mode, for everything the search tools do inside:
    the "search" component; Private / Maximum Privacy requests (no cookies, no
    Referer, a generic User-Agent, no tracking parameters); Local Only and Disabled
    offline - the network layer refuses every request and socket."""
    with netlayer.component("search"), netlayer.private_requests(mode in ("private", "max")):
        if mode in ("local", "disabled"):
            with netlayer.offline(f"{MODE_INFO[mode]['label']} search mode: the search tools don't use the network."):
                yield
        else:
            yield


def max_privacy_route_problem() -> str:
    """Maximum Privacy sends web traffic only through Tor or a proxy."""
    route = netlayer.route_for("search")
    if route.routed or netlayer.session_direct():
        return ""
    return ("Maximum Privacy sends web traffic only through Tor or a proxy, and the route for web search is "
            f"{route.label}. Choose Tor or a proxy in Settings > Network (or use Private search).")


def ask_fallback(got: dict, settings: dict, should_cancel=None) -> str | None:
    """Maximum Privacy failed: ask - never fall back silently - whether to use
    something less private for this one search. -> "private" | "direct" | None."""
    blocked = got.get("blocked") or {}
    if blocked.get("kind") in ("offline", "vpn"):
        return None                                # nothing to offer: connect the VPN / change the mode
    route = netlayer.route_for("search")
    if blocked.get("kind") == "kill":
        option = "direct"
        text = (f"Maximum Privacy couldn't search: {blocked.get('reason')}\n\nSearch once going DIRECT instead - from "
                "your own IP address, without Tor or the proxy - with Private's other protections?")
    else:
        option = "private"
        backends = ", ".join(NAMES[p] for p in chain(settings, KEYS, "private") if p != "searxng")
        text = (f"Maximum Privacy couldn't search: {'; '.join(got.get('why') or [])[:300] or 'SearXNG found nothing'}."
                f"\n\nSearch once with Private instead ({backends}) through {route.label}? Those services see the "
                "cleaned query.")
    import prompt_prep
    answer = prompt_prep.gate.request("search_fallback", {"text": text, "option": option, "command": None},
                                      cancelled=should_cancel or (lambda: False))
    return option if answer and answer.get("action") == "send" else None


def web_search_tool(args: dict, settings: dict, keys: KeyStore | None = None, browser_fallback: bool = False,
                    should_cancel=None, progress=None) -> dict:
    mode = turn_mode(settings)
    info = MODE_INFO[mode]
    if mode == "disabled":
        return {"ok": False, "status": "permanent_failure", "message": "Web search is turned off (Settings > Web Search)."}
    queries = [str(q) for q in [args.get("query") or ""] + list(args.get("queries") or []) if str(q or "").strip()]
    removed: list = []
    if mode in ("private", "max"):                  # sanitised here, between the model and the network
        cleaned = []
        for q in queries:
            c, r = sanitize_query(q, personal_terms(settings))
            removed += r
            if c:
                cleaned.append(c)
        queries = cleaned
    if not queries:
        return {"ok": False, "message": "No search query given" + (" (nothing was left after removing personal details)."
                                                                   if removed else ".")}
    if mode == "max":
        problem = max_privacy_route_problem()
        if problem:
            return {"ok": False, "status": "permanent_failure", "message": problem}
    route = netlayer.route_for("search")
    if settings.get("search_preview") and mode != "local":
        import prompt_prep
        answer = prompt_prep.gate.request("search_preview", {
            "text": "\n".join(queries), "mode": info["label"], "route": route.label, "command": None,
            "backends": [NAMES[p] for p in chain(settings, keys or KEYS, mode)], "removed": removed},
            cancelled=should_cancel or (lambda: False))
        if not answer or answer.get("action") == "cancel":
            return {"ok": False, "status": "permanent_failure", "cancelled": True,
                    "message": "The user cancelled this search before it was sent. Don't search for it again unless they ask."}
        edited = [l.strip() for l in str(answer.get("text") or "").splitlines() if l.strip()]
        queries = edited[:MAX_QUERIES] or queries
    # Private and Maximum Privacy never use the assistant's browser: it keeps cookies and a history.
    fallback = browser_fallback and mode == "standard"
    deep = bool(args.get("deep"))
    if deep:                                         # Deep search: more phrasings of different kinds (B2)
        queries = fanout(queries[0], queries[1:], limit=DEEP_WEB_QUERIES)
    kwargs = dict(num_results=args.get("num_results") or args.get("limit") or 8, recency=args.get("recency") or "auto",
                  site=args.get("site") or "", region=args.get("region") or "", settings=settings, keys=keys,
                  browser_fallback=fallback, should_cancel=should_cancel, progress=progress)
    # Forums, Q&A sites, issue trackers and papers (deep_sources, 1.4.4), searched alongside the web when the
    # question fits them - Standard mode only (Private and Maximum Privacy send the words to fewer places).
    sourced, pool = None, None
    # (On only when the setting says so: the app's settings always carry it - on by default, core/config.)
    if mode == "standard" and settings.get("search_sources") is True and not args.get("site"):
        import concurrent.futures
        import deep_sources
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        sourced = pool.submit(privacy.wrap(deep_sources.search), queries[0], queries[1:2], 6, None, should_cancel)
    with scope(mode):
        extra = None
        if sourced is not None:
            try:
                extra = sourced.result(timeout=40)
            except netlayer.RouteBlocked:
                raise
            except Exception:
                extra = None
            finally:
                pool.shutdown(wait=False)
        got = search(queries[0], queries=queries[1:], mode=mode, extra_lists=(extra or {}).get("lists"),
                     max_queries=DEEP_WEB_QUERIES if deep else MAX_QUERIES, **kwargs)
        if extra:
            got["providers"] = sorted(set(got["providers"]) | set(extra["used"]))
            got["why"] = got["why"] + [w for w in extra["why"] if w not in got["why"]]
        if deep and got["results"]:                  # the model re-ranks the fused top (B4)
            got["results"] = rerank(queries[0], got["results"])
    used_mode, note, route_label = mode, "", route.label
    if mode == "max" and not got["results"] and (got["why"] or got["blocked"]):
        choice = ask_fallback(got, settings, should_cancel)
        if choice:
            # Private, without the SearXNG instance that just failed (it isn't asked twice).
            retry = {**kwargs, "settings": {**settings, "searxng_url": ""}} if choice == "private" else kwargs
            with scope("private"), (netlayer.allow_direct() if choice == "direct" else contextlib.nullcontext()):
                got = search(queries[0], queries=queries[1:], mode="private", **retry)
            used_mode = "private"
            if choice == "direct":
                route_label = "Direct (your choice, this search only)"
            note = ("The user allowed this one search to go direct (their own IP address)." if choice == "direct"
                    else "The user allowed this one search to use Private instead of Maximum Privacy.")
    qs = got["queries"]
    if not qs:
        return {"ok": False, "message": "No search query given."}
    reg = sources.current()
    rows = []
    # Research reads pages, not snippets: its searches don't number results (a page gets its
    # number when it is read), so hundreds of searches never use up the [n] range.
    unnumbered = bool(args.get("unnumbered"))
    for r in got["results"]:
        r["n"] = 0 if unnumbered else reg.add(r["url"], r["title"], r["snippet"], r["date"])
    listed = got["results"] if unnumbered else [r for r in got["results"] if r["n"]]
    event = {"web": {"op": "search", "queries": qs, "providers": got["providers"], "recency": got["recency"],
                     "results": [{k: r[k] for k in ("n", "title", "url", "domain", "date")} for r in listed],
                     "why": got["why"][:3], "mode": MODE_INFO[used_mode]["label"],
                     "route": "This PC (no network)" if used_mode == "local" else route_label,
                     "removed": removed, "blocked": got.get("blocked"), "fallback": note}}
    quoted = " · ".join(f'"{q}"' for q in qs)
    if not listed:
        why = "; ".join(got["why"][:3]) or ("nothing on this PC matched - Local Only can't search the live web"
                                            if used_mode == "local" else "no page matched")
        return {"ok": False, "status": "permanent_failure", "refused": bool(got["why"]), "event": event,
                "message": (f"No search results for {quoted} ({why}). Try different words, or tell the user you could not "
                            "find a reliable source - do not guess or present unverified facts as found.")}
    opts = [p for p in (", ".join(got["providers"]), f"{MODE_INFO[used_mode]['label']} search" if used_mode != "standard" else "",
                        f"recency: past {got['recency']}" if got["recency"] != "any" else "",
                        f"site: {clean_site(args.get('site') or '')}" if args.get("site") else "",
                        "time-sensitive: recent dated pages ranked first" if got["sensitive"] else "") if p]
    lines = [f"Web results for {quoted} ({'; '.join(opts)}). These numbers are this answer's sources: cite a claim "
             "as [n] right after it. Snippets are previews - read the pages you rely on with fetch_page (several at "
             "once) before stating specifics."]
    kinds = "biascheck" in (shared.app_state.get("turn_tools") or ())      # Bias check: say what kind of source each is
    for r in listed:
        rows.append(f"[{r['n']}] {r['title']} - {r['domain']}" + (f" · {source_kind(r['url'])}" if kinds else "")
                    + _thread_note(r)
                    + (f" · {r['date']}" if r["date"] else "")
                    + f"\n    {r['url']}" + (f"\n    {r['snippet']}" if r["snippet"] else ""))
    lines += rows
    if all(low_quality(r) for r in listed) and used_mode != "local":
        lines.append("Note: every result is from a low-authority site - say how reliable this is, or search again.")
    if used_mode == "local":
        lines.append("(Local Only: these are pages read before, the project's files and notes, and memory - not the live web.)")
    if got["cached"]:
        lines.append("(From a search a few minutes ago.)")
    if note:
        lines.append(f"({note})")
    return {"ok": True, "message": "\n".join(lines), "event": event,
            "data": {"results": [{k: r[k] for k in ("n", "title", "url", "snippet", "date", "domain")} for r in listed],
                     "queries": qs, "providers": got["providers"]}}


_LINK_MARK_RE = re.compile(r" \[(?:[1-9]|[1-5]\d|60)\](?!\()")          # web_tools._Readable's [n]
MAX_PAGES = 6
PAGE_BUDGET = 6000          # characters per page by default
TOTAL_BUDGET = 24000        # characters for one fetch_page call


def fetch_page_tool(args: dict, browser_fallback: bool = False, should_cancel=None, progress=None) -> dict:
    urls, seen = [], set()
    for u in ([args.get("url")] if args.get("url") else []) + list(args.get("urls") or []):
        u = web_tools._normalize_url(str(u or ""))
        if u and sources.canonical(u) not in seen:
            seen.add(sources.canonical(u))
            urls.append(u)
    if not urls:
        return {"ok": False, "message": "No URL given."}
    dropped = urls[MAX_PAGES:]
    urls = urls[:MAX_PAGES]
    query = str(args.get("query") or "")
    per_page = max(1500, min(int(args.get("max_chars") or PAGE_BUDGET), 12000, TOTAL_BUDGET // len(urls)))
    progress = progress or (lambda **_: None)
    live = [{"url": u, "domain": sources.domain(u), "state": "reading"} for u in urls]

    def done(entry: dict):
        for item in live:
            if item["url"] == entry.get("url") and item["state"] == "reading":
                item["state"] = "read" if "page" in entry else "failed"
        progress(web={"op": "read", "query": query, "pages": [dict(x) for x in live]})

    settings = shared.app_state.get("settings") or {}
    mode = turn_mode(settings)
    if mode == "disabled":
        return {"ok": False, "status": "permanent_failure", "message": "Reading web pages is turned off (Settings > Web Search)."}
    if mode == "max":
        problem = max_privacy_route_problem()
        if problem:
            return {"ok": False, "status": "permanent_failure", "message": problem}
    progress(web={"op": "read", "query": query, "pages": [dict(x) for x in live]})
    with scope(mode):
        if mode == "local":
            pages = local_pages(urls)
            for p in pages:
                done(p)
        else:
            # Private and Maximum Privacy never use the assistant's browser (it keeps cookies and a history).
            pages = web_tools.read_pages(urls, browser_fallback=browser_fallback and mode == "standard",
                                         should_cancel=should_cancel, on_done=done)
    for p in pages:
        if "page" in p:
            history.record_page(p.get("final") or p["url"], p["page"].get("title"), p["page"].get("text"), settings, mode)
    reg = sources.current()
    blocks, events, ok_count = [], [], 0
    for p in pages:
        if "page" not in p:
            n = reg.add(p["url"])
            events.append({"n": n, "url": p["url"], "domain": sources.domain(p["url"]), "ok": False,
                           "why": str(p.get("error") or "")[:200]})
            blocks.append(f"[{n}] {p['url']}\n(Could not read it: {p.get('error')})")
            continue
        ok_count += 1
        page, final = p["page"], p["final"]
        # The page's own link markers ("PEP 779 [2]") would read as citations of source [2].
        text = _LINK_MARK_RE.sub("", page["text"]) if page.get("links") else page["text"]
        sel = web_tools.select_passages(text, query, per_page)
        # A page found by a search keeps that result's number, wherever it redirected.
        known = reg.find(p["url"]) or reg.find(final)
        if known:
            reg.alias(final, known)
        n = reg.add(final, page["title"], page.get("description") or "", page.get("date") or "", read=True,
                    excerpt=sel["excerpt"], note=p.get("note") or "")
        reg.alias(p["url"], n)
        head = f"[{n}] {page['title'] or final} - {sources.domain(final)}" + (
            f" · published {page['date']}" if page.get("date") else "")
        shown = (f"(Showing {sel['shown']:,} of {sel['total']:,} characters"
                 + (f", the parts about \"{query}\"" if query and sel["shown"] < sel["total"] else "")
                 + (f". web_read url=\"{final}\" reads the page from the start." if sel["shown"] < sel["total"] else ".")
                 + ")")
        blocks.append("\n".join(x for x in (head, final, p.get("note") or "", "", sel["text"], shown) if x is not None))
        events.append({"n": n, "url": final, "title": page["title"], "domain": sources.domain(final), "ok": True,
                       "date": page.get("date") or ""})
    lead = (f"Read {ok_count} of {len(urls)} page{'s' if len(urls) != 1 else ''}. Cite what you use as [n]; if these "
            "sources disagree, say so and give both sides; give the date of time-sensitive facts.")
    if dropped:
        lead += f" Not read (at most {MAX_PAGES} per call): {', '.join(dropped)}."
    message = lead + "\n\n" + "\n\n---\n\n".join(blocks)
    event = {"web": {"op": "read", "pages": events, "query": query}}
    if not ok_count:
        return {"ok": False, "status": "permanent_failure", "message": message, "event": event}
    return {"ok": True, "message": message, "event": event}
