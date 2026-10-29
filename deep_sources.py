"""
deep_sources.py
===============
Sources beyond a web search engine (1.4.4, Deep search part B): the places niche answers live - forums, Q&A sites,
issue trackers, papers - searched through their own public APIs, with no key. Every one was tested from this PC
before it went in (docs/plans/1.4.4.md, B3):

    Hacker News     hn.algolia.com          stories and comments            no key (10,000/h per IP)
    Stack Exchange  api.stackexchange.com   Stack Overflow, Super User...   no key (300/day per IP - tracked)
    Discourse       <forum>/search.json     any Discourse forum             no key
    Lemmy           lemmy.world/api/v3      posts                           no key
    GitHub          api.github.com          issues and pull requests        no key (10 searches a minute)
    Marginalia      api.marginalia.nu       independent small-web index     its public key
    Wikipedia       MediaWiki search API                                    no key
    arXiv           export.arxiv.org                                        no key

Reddit is not here: it refuses keyless requests (403). Sites that refuse are skipped and said so - this module
never pretends to be a browser or works around a block. Every request goes through netlayer's "search" route (so
through Tor when Search through Tor is on), names itself honestly, is paced per source and cached for 15 minutes.

Each adapter returns results shaped like search_engine's: {title, url, snippet, date, source}, plus "kind"
("forum", "q&a", "issue", "paper", "reference", "web") and, where the site gives them, points / answered / comments -
what B6 weighs. route() picks the sources for a question; search() runs them in parallel.
"""
from __future__ import annotations

import concurrent.futures
import html
import re
import threading
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import netlayer
import privacy

UA = "FX-Workspace/1.4 (local assistant; +https://github.com/fuxion64-ctrl/FX-Workspace)"
TIMEOUT = 12.0
SLOW = {"marginalia": 25.0}                                  # measured: Marginalia often takes 10-15 s
CACHE_TTL = 15 * 60
NAMES = {"hackernews": "Hacker News", "stackexchange": "Stack Exchange", "discourse": "Discourse forums",
         "lemmy": "Lemmy", "github": "GitHub issues", "marginalia": "Marginalia", "wikipedia": "Wikipedia",
         "arxiv": "arXiv"}
_GAP = {"hackernews": .3, "stackexchange": 1.0, "discourse": 1.0, "lemmy": .5, "github": 6.5, "marginalia": 1.0,
        "wikipedia": .2, "arxiv": 3.0}                        # seconds between two calls (GitHub: 10 a minute)
# Discourse forums searched when a question is about their subject (a forum found later can be added - B3).
DISCOURSE_FORUMS = {
    "meta.discourse.org": ("discourse",),
    "discuss.python.org": ("python", "pip", "venv", "asyncio", "pyproject"),
    "forum.level1techs.com": ("gpu", "vfio", "passthrough", "linux", "nas", "homelab", "threadripper", "epyc", "zfs"),
    "community.home-assistant.io": ("home assistant", "zigbee", "esphome", "automation", "hass"),
    "forum.obsidian.md": ("obsidian",),
    "forums.developer.nvidia.com": ("cuda", "nvidia", "jetson", "tensorrt"),
    "discuss.pytorch.org": ("pytorch", "torch", "cuda"),
    "community.openai.com": ("openai", "chatgpt api", "gpt-4", "assistants api"),
    "devforum.roblox.com": ("roblox", "luau", "roblox studio"),
    "forum.godotengine.org": ("godot", "gdscript"),
    "discuss.huggingface.co": ("hugging face", "huggingface", "transformers", "gguf"),
    "forum.manjaro.org": ("manjaro",),
    "discourse.nixos.org": ("nixos", "nix flake"),
    "discussion.fedoraproject.org": ("fedora",),
}
STACK_SITES = {"superuser": ("windows", "driver", "bios", "monitor", "keyboard", "laptop", "usb", "wifi", "excel", "gpu",
                             "rtx", "radeon", "cpu", "ssd", "nvme", "ram", "motherboard", "psu", "graphics card"),
               "askubuntu": ("ubuntu", "apt", "gnome"), "unix": ("bash", "linux", "systemd", "grep", "sed", "awk"),
               "gaming": ("game", "steam", "minecraft", "mod"), "electronics": ("arduino", "circuit", "resistor", "esp32")}

_lock = threading.Lock()
_last: dict = {}
_cache: dict = {}
_state = {"se_quota": None}


class Refused(Exception):
    """The site said no (403 / 429 / a block page): skipped, never worked around."""


def _pace(source: str):
    with _lock:
        wait = _last.get(source, 0) + _GAP.get(source, .5) - time.monotonic()
        _last[source] = time.monotonic() + max(0, wait)
    if wait > 0:
        time.sleep(wait)


def _get_json(source: str, url: str, params: dict | None = None):
    _pace(source)
    with netlayer.client("search", timeout=SLOW.get(source, TIMEOUT), headers={"User-Agent": UA, "Accept": "application/json"},
                         follow_redirects=True) as c:
        r = c.get(url, params=params)
    if r.status_code in (403, 429) or (r.status_code == 400 and source == "stackexchange" and "throttle" in r.text):
        raise Refused(f"{NAMES[source]} refused the request ({r.status_code})")
    r.raise_for_status()
    return r


def _text(s: str, n: int = 300) -> str:
    s = html.unescape(re.sub(r"<[^>]+>", " ", str(s or "")))
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n - 1].rsplit(" ", 1)[0] + "…"


def _date(v) -> str:
    try:
        if isinstance(v, (int, float)):
            return datetime.fromtimestamp(v, tz=timezone.utc).date().isoformat()
        return str(v or "")[:10] if re.match(r"\d{4}-\d\d-\d\d", str(v or "")) else ""
    except (OverflowError, OSError, ValueError):
        return ""


# ---- adapters --------------------------------------------------------------------------------------------------
def hackernews(q: str, n: int = 8) -> list:
    data = _get_json("hackernews", "https://hn.algolia.com/api/v1/search",
                     {"query": q, "tags": "(story,comment)", "hitsPerPage": n}).json()
    out = []
    for h in data.get("hits") or []:
        sid = h.get("story_id") or h.get("objectID")
        is_comment = bool(h.get("comment_text"))
        title = h.get("title") or h.get("story_title") or ""
        if not title:
            continue
        out.append({"title": (f"Comment on: {title}" if is_comment else title),
                    "url": f"https://news.ycombinator.com/item?id={h.get('objectID') if is_comment else sid}",
                    "snippet": _text(h.get("comment_text") or h.get("story_text") or h.get("url") or ""),
                    "date": _date(h.get("created_at")), "kind": "forum", "points": h.get("points") or 0,
                    "comments": h.get("num_comments") or 0})
    return out


def stackexchange(q: str, n: int = 8, site: str = "stackoverflow") -> list:
    if _state["se_quota"] is not None and _state["se_quota"] < 5:
        raise Refused("Stack Exchange's keyless daily quota for this IP is used up")
    # "similar" matches question titles loosely and ranks well (tested: it found answers where the full-text
    # search found none); the full-text search is the fallback. Each call costs one of the 300 a day.
    base = {"order": "desc", "sort": "relevance", "site": site, "pagesize": n, "filter": "withbody"}
    data = _get_json("stackexchange", "https://api.stackexchange.com/2.3/similar", {**base, "title": q}).json()
    if len(data.get("items") or []) < 2 and (data.get("quota_remaining") or 0) > 20:
        data = _get_json("stackexchange", "https://api.stackexchange.com/2.3/search/advanced", {**base, "q": q}).json()
    _state["se_quota"] = data.get("quota_remaining")
    out = []
    for it in data.get("items") or []:
        out.append({"title": _text(it.get("title"), 200), "url": it.get("link") or "",
                    "snippet": _text(it.get("body") or ""), "date": _date(it.get("last_activity_date") or it.get("creation_date")),
                    "kind": "q&a", "points": it.get("score") or 0, "answered": bool(it.get("accepted_answer_id")),
                    "comments": it.get("answer_count") or 0, "site": site})
    return out


def discourse(q: str, n: int = 8, forum: str = "meta.discourse.org") -> list:
    base = f"https://{forum}"
    data = _get_json("discourse", f"{base}/search.json", {"q": q}).json()
    topics = {t.get("id"): t for t in data.get("topics") or []}
    out, seen = [], set()
    for p in data.get("posts") or []:
        t = topics.get(p.get("topic_id")) or {}
        if p.get("topic_id") in seen or not t:
            continue
        seen.add(p.get("topic_id"))
        out.append({"title": _text(t.get("title") or t.get("fancy_title"), 200),
                    "url": f"{base}/t/{t.get('slug') or 'topic'}/{t.get('id')}/{p.get('post_number') or 1}",
                    "snippet": _text(p.get("blurb")), "date": _date(p.get("created_at")), "kind": "forum",
                    "answered": bool(t.get("has_accepted_answer")), "comments": t.get("posts_count") or 0, "site": forum})
        if len(out) >= n:
            break
    return out


def lemmy(q: str, n: int = 8) -> list:
    data = _get_json("lemmy", "https://lemmy.world/api/v3/search",
                     {"q": q, "type_": "Posts", "sort": "TopAll", "limit": n, "listing_type": "All"}).json()
    out = []
    for p in data.get("posts") or []:
        post, counts = p.get("post") or {}, p.get("counts") or {}
        out.append({"title": _text(post.get("name"), 200), "url": post.get("ap_id") or "",
                    "snippet": _text(post.get("body") or post.get("url") or ""), "date": _date(post.get("published")),
                    "kind": "forum", "points": counts.get("score") or 0, "comments": counts.get("comments") or 0})
    return out


def github(q: str, n: int = 8) -> list:
    data = _get_json("github", "https://api.github.com/search/issues", {"q": q, "per_page": n}).json()
    out = []
    for it in data.get("items") or []:
        repo = "/".join((it.get("repository_url") or "").split("/")[-2:])
        out.append({"title": f"{_text(it.get('title'), 180)} ({repo} #{it.get('number')}, {it.get('state')})",
                    "url": it.get("html_url") or "", "snippet": _text(it.get("body") or ""),
                    "date": _date(it.get("updated_at")), "kind": "issue", "comments": it.get("comments") or 0,
                    "answered": it.get("state") == "closed"})
    return out


def marginalia(q: str, n: int = 8) -> list:
    data = _get_json("marginalia", f"https://api.marginalia.nu/public/search/{quote(q)}", {"count": n}).json()
    return [{"title": _text(r.get("title"), 200), "url": r.get("url") or "", "snippet": _text(r.get("description")),
             "date": "", "kind": "web"} for r in data.get("results") or []]


def wikipedia(q: str, n: int = 5) -> list:
    data = _get_json("wikipedia", "https://en.wikipedia.org/w/api.php",
                     {"action": "query", "list": "search", "srsearch": q, "format": "json", "srlimit": n}).json()
    return [{"title": r.get("title") or "", "url": "https://en.wikipedia.org/wiki/" + quote((r.get("title") or "").replace(" ", "_")),
             "snippet": _text(r.get("snippet")), "date": _date(r.get("timestamp")), "kind": "reference"}
            for r in (data.get("query") or {}).get("search") or []]


def arxiv(q: str, n: int = 5) -> list:
    words = [w for w in re.findall(r"[\w-]+", q) if len(w) > 2][:8]       # every word must match, not any one
    text = _get_json("arxiv", "https://export.arxiv.org/api/query",
                     {"search_query": " AND ".join(f"all:{w}" for w in words) or "all:" + q, "max_results": n,
                      "sortBy": "relevance"}).text
    out = []
    for entry in re.findall(r"<entry>(.*?)</entry>", text, re.S):
        get = lambda tag: (re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", entry, re.S) or [None, ""])[1]  # noqa: E731
        out.append({"title": _text(get("title"), 200), "url": _text(get("id"), 300).replace("http://", "https://"),
                    "snippet": _text(get("summary")), "date": _date(get("published")), "kind": "paper"})
    return out


ADAPTERS = {"hackernews": hackernews, "stackexchange": stackexchange, "discourse": discourse, "lemmy": lemmy,
            "github": github, "marginalia": marginalia, "wikipedia": wikipedia, "arxiv": arxiv}


# ---- the router ----------------------------------------------------------------------------------------------
_ERROR_RE = re.compile(r"\b(error|exception|traceback|crash(?:es|ing)?|segfault|fails?|failed|failing|bug|broken|"
                       r"doesn'?t work|not working|won'?t (?:start|load|boot|install)|stack ?trace|exit code|0x[0-9a-f]{4,}|"
                       r"errno|cannot|can'?t (?:find|open|load|import)|unavailable|stopped working|keeps? "
                       r"(?:crashing|freezing|disconnecting)|disconnects?|freezes?|stutter(?:s|ing)?)\b", re.I)
_CODE_RE = re.compile(r"\b(python|javascript|typescript|node|npm|pip|rust|cargo|golang|java|c\+\+|c#|\.net|react|"
                      r"docker|kubernetes|git|api|sdk|library|compile|build|cuda|llama\.cpp|gguf|vulkan|sql|regex)\b|"
                      r"[\w.]+\(\)|\bv?\d+\.\d+(?:\.\d+)?\b", re.I)
_EXPERIENCE_RE = re.compile(r"\b(anyone|experience|worth it|should i|recommend|vs\.?|versus|better than|best|review|"
                            r"opinions?|thoughts on|alternatives?|fix(?:ed)?|workaround|solved|issue|problem|why does|"
                            r"how do (?:you|people)|is it (?:safe|normal|possible))\b", re.I)
_SCIENCE_RE = re.compile(r"\b(study|studies|paper|research|evidence|theorem|proof|algorithm|neural|protein|clinical|"
                         r"peer.reviewed|meta.analysis|dataset|benchmark)\b", re.I)


def _forums_for(question: str) -> list:
    q = question.lower()
    return [f for f, words in DISCOURSE_FORUMS.items() if any(re.search(rf"\b{re.escape(w)}\b", q) for w in words)][:2]


def _stack_site(question: str) -> str:
    q = question.lower()
    for site, words in STACK_SITES.items():
        if any(re.search(rf"\b{re.escape(w)}\b", q) for w in words) and not _CODE_RE.search(q):
            return site
    return "stackoverflow"


def route(question: str) -> list:
    """[(source, options)] to search for this question, besides the web search itself."""
    q = question or ""
    out = []
    error, code, exp, sci = (bool(r.search(q)) for r in (_ERROR_RE, _CODE_RE, _EXPERIENCE_RE, _SCIENCE_RE))
    forums = _forums_for(q)
    if forums:                                              # a question about a forum's subject: its forum first
        out += [("discourse", {"forum": f}) for f in forums]
    if error or code:
        out += [("stackexchange", {"site": _stack_site(q)}), ("github", {})]
    if error or code or exp or forums:
        out += [("hackernews", {})]
    if exp:
        out += [("lemmy", {})]
        if not (error or code):
            out += [("stackexchange", {"site": _stack_site(q)})]
    if sci:
        out += [("arxiv", {}), ("wikipedia", {})]
    if not out:
        # Marginalia was tried here and took 10-40 s (eval, 29 Oct 2026): a plain question gets the encyclopedia and
        # Hacker News; Marginalia stays available to Deep search's own calls.
        out = [("wikipedia", {}), ("hackernews", {})]
    seen, uniq = set(), []
    for s, o in out:
        key = (s, tuple(sorted(o.items())))
        if key not in seen:
            seen.add(key)
            uniq.append((s, o))
    return uniq[:6]


# ---- running them --------------------------------------------------------------------------------------------
def run(source: str, q: str, n: int = 8, **opts) -> tuple[list, str]:
    """(results, why none) - cached; a refusal or an error is a reason, never an exception."""
    key = (source, q.lower(), n, tuple(sorted(opts.items())))
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < CACHE_TTL:
        return hit[1], ""
    try:
        got = [r for r in ADAPTERS[source](q, n, **opts) if r.get("url", "").startswith("https://")]
    except netlayer.RouteBlocked:
        raise                                               # the kill switch: nothing goes around it
    except Refused as e:
        return [], str(e)
    except Exception as e:
        return [], f"{NAMES[source]} failed ({type(e).__name__}: {str(e)[:100]})"
    for r in got:
        r["source"] = NAMES[source] + (f" ({opts['forum']})" if opts.get("forum") else
                                       f" ({opts['site']})" if opts.get("site") and opts["site"] != "stackoverflow" else "")
        r["domain"] = urlparse(r["url"]).hostname or ""
    _cache[key] = (time.monotonic(), got)
    return got, ""


def search(question: str, queries: list | None = None, n: int = 6, sources: list | None = None,
           should_cancel=None) -> dict:
    """The routed sources for a question, in parallel. -> {"lists": [[result...]...], "used": [...], "why": [...]}
    Each (source, query) pair is one ranked list - search_engine fuses them with the web's lists (RRF)."""
    plan = sources if sources is not None else route(question)
    qs = [q for q in [question] + list(queries or []) if q][:2]          # the APIs want plain words: two phrasings at most
    jobs = [(s, o, q) for s, o in plan for q in qs]
    lists, used, why = [], [], []
    if not jobs:
        return {"lists": [], "used": [], "why": []}
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(jobs)))
    # Stack Exchange's "similar" matches question titles, so it gets the question as asked; the rest search words.
    futs = {pool.submit(privacy.wrap(run), s, _plain(q) if s == "stackexchange" else _words(q), n, **o): (s, o, q)
            for s, o, q in jobs if not (should_cancel and should_cancel())}            # the caller's scopes go along
    try:
        for f in concurrent.futures.as_completed(futs, timeout=WAIT_S):
            s, o, q = futs[f]
            got, problem = f.result()
            if got:
                lists.append(got)
                used.append(got[0]["source"])
            elif problem:
                why.append(problem)
    except concurrent.futures.TimeoutError:          # a slow source doesn't hold up the answer
        slow = sorted({NAMES[s] for f, (s, o, q) in futs.items() if not f.done()})
        why.append(f"{', '.join(slow)} took too long and {'were' if len(slow) > 1 else 'was'} left out")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return {"lists": lists, "used": sorted(set(used)), "why": list(dict.fromkeys(why))}


WAIT_S = 15.0
_STOP = set("a an the of to in on for and or is are was were be been do does did how what why when where which who "
            "can could should would will my me i you it this that with from by at as about any anyone there their "
            "get got have has had not no so if than then into out up still using use".split())


def _plain(q: str) -> str:
    """A web query as an API wants it: no site:/intitle: operators, no quotes doubled up."""
    q = re.sub(r"\b(?:site|inurl|intitle|filetype):\S+", " ", q)
    return re.sub(r"\s+", " ", q).strip()


def _words(q: str) -> str:
    """Keywords for a forum's own search, which matches words, not sentences ("how does X work" -> "X work")."""
    words = [w for w in re.findall(r"[\w.+#-]+", _plain(q).replace('"', " ")) if w.lower() not in _STOP]
    return " ".join(words[:8]) or _plain(q)


def quota() -> dict:
    return {"stackexchange_remaining": _state["se_quota"]}
