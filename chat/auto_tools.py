"""
auto_tools.py
=============
Tools the assistant turns on by itself (Settings > Agent > "Turn on helpful tools by itself", on by default).

Two ways, both limited to tools that only read or make things - nothing that changes the PC or runs code:

  pick(...)        before the turn, from the message (agent/tasks._begin_composer_tools): a question about
                   current facts gets Grounded answers, numbers to plot get Charts, "who reported it first" gets
                   Claim timelines, "is it true that" gets Bias check, a bare link gets Summarize, prices and
                   tickers get Market data, "research ... in depth" gets Research (which still shows its plan
                   for approval), "a dashboard/landing page" gets Canvas. With Careful and honest on, any
                   question about specific checkable facts gets Grounded answers too (careful.checkable).
  use_tools        a tool the model can call mid-answer when it finds it needs one of SAFE - the next step
                   is offered that tool's tools and instructions.

Every tool turned on this way is listed with why under the reply (sessions extras "au"), so it is never
silent; the user's own choices are never turned off. Web tools are skipped when Web Search is off.
"""
from __future__ import annotations

import re

DEFAULT_SETTINGS = {"auto_tools": True}

# What use_tools may turn on: they only read or make things (no code runs, nothing on the PC changes).
SAFE = ("grounded", "charts", "timeline", "biascheck", "summarize", "market", "canvas", "visuals")
WEB = {"grounded", "timeline", "biascheck", "summarize", "market", "research"}
LABELS = {"grounded": "Grounded answers", "charts": "Charts and statistics", "timeline": "Claim timelines",
          "biascheck": "Bias check", "summarize": "Summarize a link", "market": "Market data", "canvas": "Canvas",
          "research": "Research", "image": "Image", "visuals": "Visuals"}

_URL = re.compile(r"https?://\S+", re.I)
_RULES = (
    ("research", re.compile(r"\b(?:research (?:this|the|whether|how|why|what|into)|deep[- ]dive|in[- ]depth (?:report|analysis|look)|"
                            r"comprehensive (?:report|overview|analysis)|literature review|thorough(?:ly)? research)\b", re.I),
     "you asked for research - it shows its plan for your OK first"),
    ("charts", re.compile(r"\b(?:chart|graph|plot|visuali[sz]e|histogram|pie|bar chart|line chart|scatter|trend line|"
                          r"average|mean|median|standard deviation|std ?dev|variance|percentile|correlat\w*|regression|"
                          r"growth rate|cagr|percent(?:age)? change|statistics? (?:of|for|on)|crunch the numbers)\b"
                          r"|גרף|תרשים|ממוצע|חציון|סטטיסטיק|график|диаграмм|среднее|медиан", re.I),
     "the request is about numbers to compute or plot"),
    ("timeline", re.compile(r"\b(?:who (?:reported|said) (?:it|this) first|first reported|timeline of|how (?:did|has) the "
                            r"story (?:develop|unfold|changed)|when did (?:this|the news) (?:start|break))\b", re.I),
     "it asks who reported what, and when"),
    ("biascheck", re.compile(r"\b(?:is it true that|fact[- ]?check|both sides|is (?:this|that|it) (?:biased|propaganda|"
                             r"misinformation|fake news)|debunk|verify (?:this|the|that) claim)\b|האם זה נכון|правда ли", re.I),
     "it asks whether a claim is true"),
    ("market", re.compile(r"\b(?:stock|shares? price|ticker|market cap|earnings|dividend|s&p ?500|nasdaq|dow jones|"
                          r"inflation rate|interest rates?|cpi|treasury yield|exchange rate|forex|p/e)\b|\$[A-Z]{1,5}\b", re.I),
     "it asks about markets or the economy"),
    ("canvas", re.compile(r"\b(?:make|build|create|design|mock ?up) (?:me )?(?:a|an|the) (?:web ?page|landing page|"
                          r"dashboard|html page|flow ?chart|diagram|infographic|mockup|ui)\b", re.I),
     "it asks for a page or a diagram to look at"),
    # Interactive replies (D50): an explanation, a comparison or a formula - a chart, explainer or choices may help.
    ("visuals", re.compile(r"\b(?:explain|how (?:does|do|did|is|are|can) .{0,40}\bwork|why (?:does|do|is|are)|what(?:'s| is) the "
                           r"difference|difference between|compare|comparison|versus|vs\.?|pros and cons|trade-?offs?|"
                           r"over time|trend|timeline|history of|step[- ]by[- ]step|walk me through|life ?cycle|visuali[sz]e|"
                           r"diagram|formula|equation|function of|y ?= ?[^,]{1,30}x|derivative|integral|probability|"
                           r"distribution|help me (?:choose|decide|pick|understand)|which (?:one|should i)|teach me)\b"
                           r"|תסביר|הסבר|השווה|ההבדל|объясни|сравни|разница", re.I),
     "a chart, an explainer or choices may make this clearer"),
    ("grounded", re.compile(r"\b(?:latest|newest|current(?:ly)?|right now|today|tonight|yesterday|this (?:week|month|year)|"
                            r"news|recent(?:ly)?|just (?:announced|released)|release date|who won|score|schedule|"
                            r"price of|how much (?:is|does|are) .{1,30} cost|weather|forecast|still (?:alive|open|available))\b"
                            r"|חדשות|היום|עכשיו|новости|сегодня|сейчас", re.I),
     "it depends on current facts"),
)


def clean_settings(s: dict) -> dict:
    return {"auto_tools": s.get("auto_tools") if isinstance(s.get("auto_tools"), bool) else True}


def on(settings: dict | None) -> bool:
    return clean_settings(settings or {})["auto_tools"]


def web_ok(settings: dict | None) -> bool:
    """Web tools may be turned on by itself only while Web Search isn't off and "Search the web when a question
    needs it" (web_auto_search) is on - turning that off means: no web for a question unless I ask."""
    import search_engine
    return search_engine.turn_mode() != "disabled" and (settings or {}).get("web_auto_search", True) is not False


def pick(message: str, chosen: set, settings: dict, web_ok: bool, has_table: bool = False) -> list:
    """[{"id", "why"}] to turn on for this message (never one already chosen)."""
    if not on(settings):
        return []
    import careful
    msg = str(message or "")
    out, have = [], set(chosen or ())

    def add(tid, why):
        if tid not in have and (web_ok or tid not in WEB):
            out.append({"id": tid, "why": why})
            have.add(tid)

    if {"research", "invest"} & have:          # Research does its own searching and charts
        return []
    for tid, rx, why in _RULES:
        if rx.search(msg):
            add(tid, why)
    if has_table:
        add("charts", "a table is attached")
    urls = _URL.findall(msg)
    if urls and (len(msg.strip()) - sum(len(u) for u in urls) < 40
                 or re.search(r"\b(?:summari[sz]e|tl;?dr|gist|what does (?:this|it) say)\b", msg, re.I)):
        add("summarize", "you gave a link to read")
    if careful.on(settings) and careful.checkable(msg):
        add("grounded", "Careful and honest: it checks facts like these instead of answering from memory")
    if "research" in have:                      # Research covers searching; don't also force a grounded answer
        out = [a for a in out if a["id"] not in ("grounded", "summarize", "timeline", "biascheck")]
    return out


def use_tools_tool(args: dict) -> dict:
    """The model's use_tools: turn on SAFE tools for the rest of this answer."""
    import search_engine
    import core.state as shared
    asked = args.get("tools") if isinstance(args.get("tools"), list) else [args.get("tools")]
    why = " ".join(str(args.get("why") or "").split())[:200] or "the model found it needed it"
    turn = shared.app_state.setdefault("turn_tools", set())
    if not isinstance(turn, set):
        turn = shared.app_state["turn_tools"] = set(turn)
    web_allowed = web_ok(shared.app_state.get("settings"))
    added, refused = [], []
    for t in asked:
        t = str(t or "").strip().lower()
        if t not in SAFE or (t in WEB and not web_allowed):
            refused.append(t)
            continue
        if t not in turn:
            turn.add(t)
            added.append(t)
            shared.app_state.setdefault("turn_auto", []).append({"id": t, "why": why, "by": "model"})
    if not added and refused:
        return {"ok": False, "status": "permanent_failure",
                "message": f"Can't turn on {', '.join(refused)} here. Tools you can turn on: {', '.join(SAFE)}"
                           + ("" if web_allowed else " (web tools are off - Settings > Web Search and Agent)") + "."}
    import events as event_log
    for t in added:
        event_log.emit_activity("tools_enabled", f"Turned on {LABELS.get(t, t)} by itself", detail=why)
    return {"ok": True, "on": sorted(turn & set(SAFE)),
            "message": (f"On now: {', '.join(LABELS.get(t, t) for t in added)}. Their tools and instructions come with "
                        "your next step." if added else "Those tools are already on.")}


def for_reply() -> list:
    """What was turned on by itself this turn, for the reply's "Turned on by itself" row."""
    import core.state as shared
    seen, out = set(), []
    for a in shared.app_state.get("turn_auto") or []:
        if a["id"] not in seen:
            seen.add(a["id"])
            out.append({"id": a["id"], "label": LABELS.get(a["id"], a["id"]), "why": a["why"], "by": a.get("by", "app")})
    return out
