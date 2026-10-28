"""
agent/research.py
=================
Research - the composer's Research tool and /research, and Investment research
(the composer's Investment research tool and /invest). A question becomes a
report built from many web sources:

  1 Plan    the model splits the question into sub-questions, each with a
            few search phrasings. They are shown on /plan's card (Approve /
            Edit / Reject): nothing is searched before you approve.
  2 Gather  per sub-question, in rounds: 2-3 searches at once
            (search_engine.web_search_tool), the best new pages read in
            parallel (fetch_page_tool, only the passages about the
            sub-question), then the model writes findings citing [n] and
            names what is still missing - the next round searches for that.
  3 Write   one structured report citing [n], streamed into the chat. Its
            sources are saved with it, and it can be exported (index.html).

Depth (the composer's Depth menu, `research_depth`): Quick ~8 sources, Standard
~25, Deep ~80, Ultra 300-400; "auto" follows the effort level (12-48). A local
model's context can't hold 400 pages, so the work never does - it keeps a
ledger instead:
  * notes, not pages: each round's pages are read by the model once and turned
    into short bullets citing [n]; only those bullets are kept.
  * consolidation: when a sub-question's bullets pass NOTE_BUDGET, the model
    merges duplicates into fewer bullets that keep every [n] and every number.
  * saturation: a sub-question stops early when two rounds in a row add
    little that is new (the same facts from new sites) - the budget moves on.
  * diversity: at most `per_domain` pages from one site.
  * sections: a large report is written one section per sub-question (each
    from its own bullets), then the summary, disagreements and open questions
    from the sections - no single call has to hold everything.
  * on disk: the ledger (sources, notes, progress) is saved after every round
    in data/research/, so a stopped or crashed run can be resumed (send
    "resume" with Research on). A temporary chat's research is never saved.

With Bias check on (composer_tools.BIAS_RULES) every sub-question is also
searched for evidence against it, the notes rate each source's trust, and the
report weighs both sides and ends with a verdict. Investment research always
does, and more: the company's numbers first, from official sources
(finance.company_brief: SEC filings, prices, the economy) as a factual
backbone, and every finding labelled FACT / MARKET / COMPUTED / REPORTED /
ESTIMATE / OPINION / SPECULATION / PROMOTIONAL (finance.EVIDENCE) so what
happened stays apart from what someone expects or believes.

The searches and reads are ordinary web_search / fetch_page events, so the
chat's search row shows them live; a research_progress event drives the
progress bar. Stop ends it between any two requests; Pause holds it.
"""
from __future__ import annotations

import json
import os
import re
import time
import uuid
from collections import Counter

import atomic_io
import composer_tools
import corroboration
import effort as effort_mod
import finance
import permissions
import privacy
import prompt_prep
import search_engine
import sources
from run_control import RunStopped, run_control

import agent.models as llm
import core.state as shared
import events as event_log

# effort level -> (pages to read, rounds per sub-question, most sub-questions, pages per round) - depth "auto"
BUDGETS = {"low": (12, 1, 3, 4), "normal": (18, 2, 4, 3), "high": (25, 2, 5, 3), "extra": (32, 3, 5, 3),
           "max": (40, 3, 6, 3), "ultra": (48, 4, 6, 3)}
TIME_LIMIT_S = 40 * 60
# Chosen depths (the composer's Depth menu).
DEPTHS = {
    "quick": {"target": 8, "questions": 3, "rounds": 1, "per_round": 4, "per_domain": 2, "minutes": 12},
    "standard": {"target": 25, "questions": 5, "rounds": 2, "per_round": 4, "per_domain": 3, "minutes": 35},
    "deep": {"target": 80, "questions": 8, "rounds": 4, "per_round": 5, "per_domain": 3, "minutes": 100},
    "ultra": {"target": 350, "questions": 14, "rounds": 8, "per_round": 6, "per_domain": 4, "minutes": 260},
}
DEPTH_LABELS = {"auto": "Auto", "quick": "Quick", "standard": "Standard", "deep": "Extensive", "ultra": "Ultra-deep"}
NOTE_BUDGET = 5000             # characters of bullets per sub-question before they are consolidated
WRITE_SINGLE = 14000           # findings up to this size are written in one call; more: section by section
LEDGER_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "research")
RESUME_DAYS = 7

PLAN_SYSTEM = (
    "You plan web research. Split the user's question into {n} focused sub-questions that together answer it "
    "completely (background, the key facts, recent developments, disagreements or trade-offs - whatever applies). "
    "For each, give 2-3 web search queries worded differently (official names, key terms, a narrower angle). "
    "Answer with JSON only: {{\"title\": \"a short report title\", \"questions\": [{{\"q\": \"sub-question\", "
    "\"search\": [\"query\", \"query\"]}}]}}")
FINANCE_PLAN = (
    "You plan investment research. Split the request into {n} focused sub-questions covering what applies: the "
    "business and how it makes money; recent financial results and trends; valuation versus peers and its own "
    "history; competition and market position; management, ownership and capital allocation; risks (business, "
    "financial, regulatory, macro); catalysts and upcoming events; the bull case and the bear case; the "
    "macroeconomic backdrop (rates, inflation, sector). For each, give 2-3 web search queries. List the stock "
    "tickers (or company names) involved. Answer with JSON only: {{\"title\": \"a short report title\", "
    "\"tickers\": [\"TICKER\"], \"questions\": [{{\"q\": \"sub-question\", \"search\": [\"query\", \"query\"]}}]}}")
NOTES_SYSTEM = (
    "You extract research findings. From the numbered sources below, write the facts that answer the "
    "sub-question as short bullet points. End every bullet with the source number(s) it comes from, like [3] or "
    "[2, 5]. Use only these sources and only their numbers; never invent facts, numbers or quotes. Include dates "
    "for anything time-sensitive. Note where sources disagree. Skip anything already in KNOWN SO FAR unless a "
    "source adds a new number, date or disagreement. Then, on the last line, write "
    "'FOLLOW-UP: <query> | <query>' with up to two web searches that would fill important gaps, or "
    "'FOLLOW-UP: none'.")
EVIDENCE_NOTES = (
    " EVIDENCE: start every bullet with its evidence class in brackets - [FACT], [MARKET], [COMPUTED], [REPORTED], "
    "[ESTIMATE], [OPINION], [SPECULATION] or [PROMOTIONAL] - using the class given for its source under SOURCE "
    "KINDS, and lower it when the bullet itself is a forecast (ESTIMATE) or a view (OPINION) even on a factual site: "
    "a company's guidance is ESTIMATE, an analyst's rating is OPINION or ESTIMATE, a reported number is FACT or "
    "REPORTED. Never present an ESTIMATE, OPINION or SPECULATION as a fact.")
CONSOLIDATE_SYSTEM = (
    "You merge research notes. Rewrite the bullets below as fewer bullets without losing information: combine "
    "bullets that state the same thing and list all their source numbers together, like [2, 7, 11]; keep every "
    "distinct fact, number, date, disagreement and evidence label; drop only repetition. Output bullets only.")
# The findings carry corroboration.tag: how many independent origins (not links) back each one.
CONFIRMATION_RULE = (
    " Each finding ends with how well it is confirmed: {single source}, {2 independent} or {confirmed: N independent} "
    "(independent origins - copies of one wire story count once). Present a {single source} finding as reported by "
    "that source (\"according to ...\"), never as settled fact, and give confirmed findings the weight they have. Never "
    "copy these braces into the report.")
REPORT_SYSTEM = (
    "You write research reports in Markdown from findings that cite numbered sources. Structure:\n"
    "# <title>\n"
    "A summary of 3-5 sentences that directly answers the question.\n"
    "## Key findings\n- the most important facts\n"
    "## <one section per sub-question, with a descriptive heading>\n"
    "## Where sources disagree\n"
    "## Open questions\n"
    "Leave out \"Where sources disagree\" when they agree; under Open questions list what the sources could not settle. "
    "Cite every factual sentence with its source number(s) from the findings, like [4] or [2, 7] - only numbers "
    "that appear in the findings. Keep the dates of time-sensitive facts. Write plainly; use tables where they help "
    "compare. Do not add a sources or references list: the app shows the sources." + CONFIRMATION_RULE)
SECTION_SYSTEM = (
    "You write one section of a research report in Markdown from findings that cite numbered sources. Start with "
    "'## ' and a descriptive heading, then the section: every factual sentence cites its source number(s) from the "
    "findings, like [4] or [2, 7] - only numbers that appear in them. Keep dates, numbers and disagreements; use a "
    "table where it helps compare. No summary of the whole report, no sources list." + CONFIRMATION_RULE)
OVERVIEW_SYSTEM = (
    "You finish a research report whose sections are written. From them, write in Markdown:\n"
    "# <title>\nA summary of 3-5 sentences that directly answers the question.\n## Key findings\n- the most "
    "important facts, citing [n]\n"
    "then a line with exactly ===SECTIONS===\nthen:\n## Where sources disagree\n## Open questions\n"
    "(leave out Where sources disagree when they agree) "
    "Cite only source numbers that appear in the sections. Do not repeat the sections.")
FINANCE_REPORT = (
    "\nINVESTMENT RESEARCH - the report must also: keep evidence apart - say plainly what is FACT (reported "
    "figures, filings, official data), what is an ESTIMATE (forecasts, guidance, targets - may not happen) and what "
    "is OPINION or SPECULATION (views, rumours); lead with the numbers (a table of the key figures from the factual "
    "backbone); add '## Bull case' and '## Bear case' with equal care and their sources; '## Key risks'; "
    "'## What to watch' (upcoming results, events, thresholds that would change the picture); and '## Evidence "
    "quality' - a short table of how much of the case rests on FACT versus ESTIMATE versus OPINION/SPECULATION. Do "
    "not tell the reader to buy, sell or hold: describe the evidence and the trade-offs. End with the line: "
    "'*Research, not financial advice - check the figures against the filings before acting.*'")
FINANCE_TAIL = (
    "\nINVESTMENT RESEARCH - after the key findings add a table of the key figures from the factual backbone, and "
    "after ===SECTIONS=== also write '## Bull case', '## Bear case', '## Key risks', '## What to watch' and '## "
    "Evidence quality' (how much rests on FACT versus ESTIMATE versus OPINION/SPECULATION). Don't tell the reader to "
    "buy, sell or hold. End with: '*Research, not financial advice - check the figures against the filings before "
    "acting.*'")


BIAS_NOTES = (
    " BIAS CHECK: for each source, add its kind and trust in brackets after its number, e.g. [3 · news site · "
    "trust: high], lower the trust of social posts, sites known for misinformation and extreme or unconfirmed "
    "claims, and list what speaks against the sub-question as well as for it. For the follow-up, prefer a search "
    "for the side that has fewer sources so far, including independent and non-mainstream ones.")
BIAS_REPORT = (
    "\nBIAS CHECK - the report must also: give both sides of every disputed point with their sources, equally; keep the "
    "raw findings in full rather than short; add '## Sources and trust' - a table: [n] | source | kind | trust "
    "(high / medium / low) | why; say where a claim rests on one unconfirmed or low-trust source; and end with "
    "'## Verdict' - what the evidence supports and how confident that is. " + composer_tools.BIAS_RULES)


def budget(level: str, depth: str = "auto") -> dict:
    if depth in DEPTHS:
        return dict(DEPTHS[depth])
    target, rounds, most, per_round = BUDGETS.get(level, BUDGETS["normal"])
    return {"target": target, "rounds": rounds, "questions": most, "per_round": per_round, "per_domain": 3,
            "minutes": TIME_LIMIT_S // 60}


def _clean_questions(raw, question: str, most: int) -> list:
    out = []
    for item in raw if isinstance(raw, list) else []:
        if isinstance(item, str):
            item = {"q": item}
        if not isinstance(item, dict):
            continue
        q = re.sub(r"\s+", " ", str(item.get("q") or item.get("question") or "")).strip()[:300]
        if not q or any(q.lower() == x["q"].lower() for x in out):
            continue
        search = [re.sub(r"\s+", " ", str(s)).strip()[:200] for s in (item.get("search") or []) if str(s).strip()]
        out.append({"q": q, "search": search[:3]})
    return out[:most] or [{"q": question, "search": []}]


def plan(question: str, most: int, finance_mode: bool = False) -> dict:
    """{"title", "questions": [{"q", "search"}], "tickers"} - from the model, or the question itself."""
    system = (FINANCE_PLAN if finance_mode else PLAN_SYSTEM).format(n=f"3-{most}")
    got = llm.model_json(system, f"Question: {question}", max_tokens=900 + 90 * max(0, most - 6)) or {}
    tickers = [str(t).strip()[:40] for t in (got.get("tickers") or []) if str(t).strip()][:4] if finance_mode else []
    return {"title": str(got.get("title") or question)[:120], "questions": _clean_questions(got.get("questions"), question, most),
            "tickers": tickers}


def questions_from_text(text: str, planned: list, most: int = 8) -> list:
    """The approved card's lines -> sub-questions, keeping the planned searches of unchanged lines."""
    by_q = {p["q"].lower(): p for p in planned}
    return [{"q": s, "search": (by_q.get(s.lower()) or {}).get("search") or []} for s in prompt_prep.steps_from_text(text, most)]


def follow_ups(notes: str) -> tuple[str, list]:
    """Notes without their FOLLOW-UP line, and its queries."""
    m = re.search(r"^\s*FOLLOW-?UP\s*:\s*(.*)$", notes, re.I | re.M)
    if not m:
        return notes.strip(), []
    queries = [q.strip(" .\"'") for q in m.group(1).split("|")]
    queries = [q for q in queries if q and q.lower() not in ("none", "n/a", "-")]
    return (notes[:m.start()] + notes[m.end():]).strip(), queries[:2]


_WORD = re.compile(r"[a-z0-9]+(?:\.[0-9]+)?")
_STOP = frozenset("the a an of to in on for and or is are was were be by with as at from that this it its their".split())


def signature(bullet: str) -> frozenset:
    """A bullet's content words, without citations or labels - for telling new findings from repeats."""
    text = re.sub(r"\[[^\]]*\]", " ", bullet.lower())
    return frozenset(w for w in _WORD.findall(text) if w not in _STOP)


def is_new(sig: frozenset, seen: list) -> bool:
    """New unless a known bullet shares most of its words (Jaccard >= 0.6)."""
    if len(sig) < 3:
        return False
    for s in seen:
        inter = len(sig & s)
        if inter and inter / len(sig | s) >= 0.6:
            return False
    return True


def bullets(text: str) -> list:
    return [b.strip() for b in re.split(r"\n(?=\s*[-*•]\s)", "\n" + (text or "")) if b.strip()]


class Research:
    def __init__(self, question: str, level: str, settings: dict, browser_fallback: bool = False, bias: bool = False,
                 depth: str = "auto", finance_mode: bool = False):
        self.question, self.level, self.settings = question, level, settings
        self._live_settings = settings is shared.app_state.get("settings")
        self.finance = finance_mode
        self.bias = bias or finance_mode          # Bias check: both sides, trust ratings, a verdict
        self.depth = depth if depth in DEPTHS else "auto"
        self.b = budget(level, self.depth)
        self.browser_fallback = browser_fallback
        self.task_id = uuid.uuid4().hex[:12]
        self.read = set()                         # canonical URLs read (or tried)
        self.domains = Counter()                  # pages read per site (diversity)
        self.pages_ok = 0
        self.notes: list = []                     # [{"q", "text"}]
        self.done: set = set()                    # sub-questions finished
        self.seen: dict = {}                      # sub-question -> [signature] of its bullets
        self.backbone = ""                        # Investment research: the numbers, from finance.py
        self.title, self.questions, self.tickers = "", [], []
        self.progress_ev = None
        self.consolidations = 0

    # ---- events --------------------------------------------------------------------
    def _progress(self, stage: str, **fields):
        data = {"stage": stage, "sources": self.pages_ok, "target": self.b["target"], "depth": self.depth, **fields}
        if self.progress_ev is None:
            self.progress_ev = event_log.emit_event("activity", "Research", phase="research_progress", status="running",
                                                    research=data)
        else:
            event_log.update_event(self.progress_ev, research={**self.progress_ev.get("research", {}), **data})

    def _tool(self, tool: str, label: str, fn, args: dict | None = None) -> dict:
        """Use the ordinary permission/approval policy, with Research's own budgets.

        Accepting a research plan never overrides a tool denial or lockdown.
        The same gate runs after resuming a saved research ledger.
        """
        from tools import approvals
        run_control.checkpoint()
        policy_settings = lambda: shared.app_state["settings"] if self._live_settings else self.settings
        decision = permissions.decide(tool, policy_settings(),
                                      tainted=bool(shared.app_state.get("untrusted_seen")), args=args or {})
        refused = None
        if decision == "deny":
            refused = f"NOT EXECUTED - '{tool}' is set to DENY in Settings > Permissions."
        elif decision == "ask":
            shared.app_state["waiting_for_os_approval"] = True
            try:
                allowed = approvals.ask_batch_approval_os(
                    [{"tool_name": tool, "args": args or {}, "justification": label}],
                    context_line=f"Research: {self.title or self.question}")
            finally:
                shared.app_state["waiting_for_os_approval"] = False
            if not allowed:
                refused = f"NOT EXECUTED - '{tool}' was not approved."
        run_control.checkpoint()
        # Settings may have changed while the approval card was open.
        if not refused and permissions.decide(tool, policy_settings(), args=args or {}) == "deny":
            refused = f"NOT EXECUTED - '{tool}' is set to DENY in Settings > Permissions."
        ev = event_log.emit_event("tool", label, tool=tool, status="running")
        shared.app_state["running_tool_event"] = ev
        try:
            res = ({"ok": False, "status": "approval_required", "message": refused} if refused else fn())
        finally:
            shared.app_state["running_tool_event"] = None
        web = (res.get("event") or {}).get("web")
        event_log.finish_event(ev, ok=bool(res.get("ok")), detail=str(res.get("message") or "")[:400],
                               **({"web": web} if web else {}))
        return res

    # ---- the ledger on disk (resume) -----------------------------------------------
    def _path(self) -> str:
        return os.path.join(LEDGER_DIR, f"{self.task_id}.json")

    def save(self, status: str = "running"):
        """After every round. Never for a temporary chat (privacy.py)."""
        if privacy.active():
            return
        data = {"task_id": self.task_id, "question": self.question, "title": self.title, "questions": self.questions,
                "tickers": self.tickers, "depth": self.depth, "level": self.level, "finance": self.finance, "bias": self.bias,
                "notes": self.notes, "done": sorted(self.done), "read": sorted(self.read), "pages_ok": self.pages_ok,
                "domains": dict(self.domains), "backbone": self.backbone, "status": status, "updated": time.time(),
                "sources": [{k: s.get(k) for k in ("url", "title", "snippet", "date", "read", "excerpt", "note")}
                            for s in sources.current().items]}
        try:
            os.makedirs(LEDGER_DIR, exist_ok=True)
            tmp = self._path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False)
            atomic_io.replace(tmp, self._path())
        except OSError:
            pass

    @classmethod
    def load_unfinished(cls, settings: dict, browser_fallback: bool = False):
        """The newest research that stopped before its report (within RESUME_DAYS), ready to go on."""
        try:
            names = [os.path.join(LEDGER_DIR, n) for n in os.listdir(LEDGER_DIR) if n.endswith(".json")]
        except OSError:
            return None
        for path in sorted(names, key=os.path.getmtime, reverse=True):
            try:
                with open(path, encoding="utf-8") as f:
                    d = json.load(f)
            except (OSError, ValueError):
                continue
            if d.get("status") == "done" or time.time() - float(d.get("updated") or 0) > RESUME_DAYS * 86400:
                continue
            r = cls(d["question"], d.get("level") or "normal", settings, browser_fallback, bool(d.get("bias")),
                    d.get("depth") or "auto", bool(d.get("finance")))
            r.task_id, r.title, r.questions, r.tickers = d["task_id"], d.get("title") or d["question"], d.get("questions") or [], d.get("tickers") or []
            r.notes, r.done, r.read = d.get("notes") or [], set(d.get("done") or []), set(d.get("read") or [])
            r.pages_ok, r.domains, r.backbone = int(d.get("pages_ok") or 0), Counter(d.get("domains") or {}), d.get("backbone") or ""
            reg = sources.current()                 # the same numbers as before, so every [n] in the notes still points right
            reg.limit = sources.CITE_MAX
            for s in d.get("sources") or []:
                reg.add(s.get("url") or "", title=s.get("title") or "", snippet=s.get("snippet") or "", date=s.get("date") or "",
                        read=bool(s.get("read")), excerpt=s.get("excerpt") or "", note=s.get("note") or "")
            for n in r.notes:
                r.seen[n["q"]] = [signature(b) for b in bullets(n["text"])]
            return r
        return None

    # ---- the steps -----------------------------------------------------------------
    def search(self, queries: list) -> list:
        """Ranked results not read yet, at most `per_domain` pages from one site."""
        queries = [q for q in queries if q][:3]
        if not queries:
            return []
        args = {"query": queries[0], "queries": queries[1:], "num_results": 12 if self.depth in ("deep", "ultra") else 10,
                "unnumbered": True}
        res = self._tool("web_search", f"Search the web for {queries[0]}", lambda: search_engine.web_search_tool(
            args,
            self.settings, browser_fallback=self.browser_fallback, should_cancel=run_control.is_stopped,
            progress=event_log.progress), args=args)
        run_control.checkpoint()
        results = [r for r in (res.get("data") or {}).get("results") or []
                   if sources.canonical(r["url"]) not in self.read and not search_engine.low_quality(r)]
        if self.finance:
            # Relevance first, nudged by evidence class: an official or reported page moves up a few
            # places, promotion and chatter move down - never a stale filing over a relevant analysis.
            order = {id(r): i for i, r in enumerate(results)}
            results.sort(key=lambda r: order[id(r)] - 4 * finance.WEIGHT[finance.classify(r["url"], r.get("title", ""))])
        out, batch = [], Counter()                # the cap counts this batch too, not only pages read before
        for r in results:
            d = sources.domain(r["url"])
            if self.domains[d] + batch[d] < self.b["per_domain"]:
                out.append(r)
                batch[d] += 1
        return out

    def read_pages(self, urls: list, focus: str) -> tuple[str, list]:
        for u in urls:
            self.read.add(sources.canonical(u))
            self.domains[sources.domain(u)] += 1
        args = {"urls": urls, "query": focus, "max_chars": 1800}
        res = self._tool("fetch_page", f"Read {len(urls)} page{'s' if len(urls) != 1 else ''}",
                         lambda: search_engine.fetch_page_tool(args,
                                                               browser_fallback=self.browser_fallback,
                                                               should_cancel=run_control.is_stopped,
                                                               progress=event_log.progress), args=args)
        run_control.checkpoint()
        pages = ((res.get("event") or {}).get("web") or {}).get("pages") or []
        self.pages_ok += sum(1 for p in pages if p.get("ok"))
        return (str(res.get("message") or "") if res.get("ok") else ""), pages

    def _kinds(self, pages: list) -> str:
        """SOURCE KINDS for the notes (Investment research): each page's evidence class."""
        rows = [f"[{p['n']}] {p.get('domain') or sources.domain(p.get('url', ''))} - {finance.label(p.get('url', ''), p.get('title', ''))}"
                for p in pages if p.get("ok") and p.get("n")]
        return ("\n\nSOURCE KINDS:\n" + "\n".join(rows)) if rows else ""

    def consolidate(self, q: str, text: str) -> str:
        """Too many bullets for one sub-question: merged into fewer, keeping every [n]."""
        self._progress("consolidating", question=q)
        out = llm.model_text(CONSOLIDATE_SYSTEM, f"Sub-question: {q}\n\n{text}", max_tokens=1600,
                             label="Merging notes", temperature=0.1)["text"].strip()
        self.consolidations += 1
        # A merge that lost most of the citations is worse than the long notes it replaces.
        if len(sources.cited_numbers(out)) < 0.7 * len(sources.cited_numbers(text)):
            return text
        return out

    def gather(self, questions: list):
        remaining = [q for q in questions if q["q"] not in self.done]
        per_question = max(2, -(-max(1, self.b["target"] - self.pages_ok) // max(1, len(remaining))))       # ceil
        saturating = self.depth in ("deep", "ultra")
        for i, item in enumerate(questions):
            if item["q"] in self.done:
                continue
            queries = (item["search"] or []) + [item["q"]]
            if self.bias:                         # the other side too, from the first round
                queries = queries[:2] + [f"evidence against {item['q']}" if not self.finance else f"{item['q']} risks bear case",
                                         f"criticism {item['q']}" if not self.finance else f"{item['q']} bull case"]
            entry = next((n for n in self.notes if n["q"] == item["q"]), None)
            if entry is None:
                entry = {"q": item["q"], "text": ""}
                self.notes.append(entry)
            seen = self.seen.setdefault(item["q"], [])
            got_here, flat = 0, 0
            for rnd in range(self.b["rounds"]):
                if self.pages_ok >= self.b["target"] + 8 or got_here >= per_question:
                    break
                self._progress("searching", question=item["q"], index=i + 1, total=len(questions), round=rnd + 1)
                fresh = self.search(list(dict.fromkeys(queries)))
                want = min(self.b["per_round"] + (1 if rnd == 0 else 0), per_question - got_here)
                picks = [r["url"] for r in fresh][:min(6, want + 2)]      # two spares: some sites refuse to be read
                if not picks:
                    break
                self._progress("reading", question=item["q"], index=i + 1, total=len(questions), round=rnd + 1)
                before = self.pages_ok
                text, pages = self.read_pages(picks, item["q"])
                got_here += self.pages_ok - before
                if not text:
                    self.save()
                    continue
                self._progress("noting", question=item["q"], index=i + 1, total=len(questions), round=rnd + 1)
                known = "\n".join(bullets(entry["text"])[-12:])
                system = NOTES_SYSTEM + (BIAS_NOTES if self.bias else "") + (EVIDENCE_NOTES if self.finance else "")
                out = llm.model_text(system, f"Sub-question: {item['q']}\n(Part of: {self.question})\n\n"
                                             + (f"KNOWN SO FAR:\n{known}\n\n" if known else "") + text
                                             + (self._kinds(pages) if self.finance else ""),
                                     max_tokens=700, label="Noting findings", temperature=0.2)
                notes, more = follow_ups(out["text"])
                new = [b for b in bullets(notes) if is_new(signature(b), seen)]
                for b in new:
                    seen.append(signature(b))
                if new:
                    entry["text"] = (entry["text"] + "\n" + "\n".join(new)).strip()
                if len(entry["text"]) > NOTE_BUDGET * 1.5:
                    entry["text"] = self.consolidate(item["q"], entry["text"])
                    self.seen[item["q"]] = seen = [signature(b) for b in bullets(entry["text"])]
                # Saturation: two rounds in a row with little that is new - this sub-question is covered.
                flat = flat + 1 if len(new) <= max(1, len(bullets(notes)) // 4) else 0
                self.save()
                if saturating and flat >= 2:
                    event_log.emit_activity("research_saturated", f"Covered: {item['q'][:80]}", detail="new pages repeated what was known")
                    break
                queries = more
                if not queries:
                    break
            self.done.add(item["q"])
            self.save()
        self.notes = [n for n in self.notes if n["text"].strip()]

    def _backbone_block(self) -> str:
        return (f"FACTUAL BACKBONE (official and market data, labelled):\n{self.backbone}\n\n{finance.LEGEND}\n\n"
                if self.backbone else "")

    def write(self, title: str) -> str:
        # Each finding tagged with how many independent origins back it (corroboration.py): the writer
        # says "according to ..." for the ones only one outlet reports.
        items = sources.current().items
        findings = "\n\n".join(f"### {n['q']}\n{corroboration.annotate(n['text'], items)}" for n in self.notes)
        head = (f"Question: {self.question}\nSuggested title: {title}\nToday: {time.strftime('%Y-%m-%d')}\n\n"
                + self._backbone_block())
        extra = (BIAS_REPORT if self.bias and not self.finance else "") + (FINANCE_REPORT if self.finance else "")
        if len(findings) + len(self.backbone) <= WRITE_SINGLE:
            self._progress("writing")
            return llm.model_text(REPORT_SYSTEM + extra, head + f"Findings by sub-question:\n\n{findings}",
                                  max_tokens=4500 if self.bias else 3500, stream=True, label="Writing the report",
                                  temperature=0.3)["text"]
        # Section by section: each from its own findings, then the summary around them.
        sections = []
        for i, n in enumerate(self.notes):
            self._progress("writing", section=i + 1, sections=len(self.notes))
            body = llm.model_text(SECTION_SYSTEM + (EVIDENCE_NOTES.replace("start every bullet", "mark each claim") if self.finance else ""),
                                  head + f"Section topic (a sub-question): {n['q']}\n\nFindings:\n{corroboration.annotate(n['text'], items)}",
                                  max_tokens=1400, label=f"Writing section {i + 1} of {len(self.notes)}", temperature=0.3)["text"].strip()
            if not body.startswith("#"):
                body = f"## {n['q']}\n{body}"
            sections.append(body)
        self._progress("writing", section=len(sections), sections=len(sections), overview=True)
        digest = "\n\n".join(s[:1800] for s in sections)
        tail = FINANCE_TAIL if self.finance else (BIAS_REPORT if self.bias else "")
        overview = llm.model_text(OVERVIEW_SYSTEM + tail, head + f"The sections (shortened):\n\n{digest}",
                                  max_tokens=2600, stream=True, label="Writing the summary", temperature=0.3)["text"]
        top, _, bottom = overview.partition("===SECTIONS===")
        return "\n\n".join(p.strip() for p in (top, *sections, bottom) if p and p.strip())


def with_confirmation(report: str) -> str:
    """The report with "How well confirmed" (corroboration.report_section) added - before the closing
    "not financial advice" line of an investment report, else at the end."""
    section = corroboration.report_section(report, sources.current().items)
    if not section:
        return report
    body, sep, tail = report.rstrip().rpartition("\n")
    if sep and tail.strip().startswith("*Research, not financial advice"):
        return f"{body.rstrip()}\n\n{section}\n\n{tail.strip()}"
    return f"{report.rstrip()}\n\n{section}"


def depth_of(req) -> str:
    d = str(getattr(req, "research_depth", None) or (shared.app_state.get("settings") or {}).get("research_depth") or "auto")
    return d if d in DEPTHS else "auto"


def wants_resume(text: str) -> bool:
    # "retry" and "try again" too: a Research turn once took "retry" as the question to research (1.4.4).
    return bool(re.fullmatch(r"\s*(please )?(resume|continue|retry|try again|again|redo( it)?|keep going|go on)"
                             r"( (the|that) )?( ?(last|previous) )?( ?research)?( please)?[.!]?\s*", text or "", re.I))


def _fail(r, message: str) -> dict:
    if r.progress_ev is not None:
        event_log.update_event(r.progress_ev, status="done", research={**r.progress_ev["research"], "stage": "failed"})
    shared.app_state["conversation"].append({"role": "assistant", "content": message})
    return {"status": "success", "message": message, "task_id": r.task_id}


def run(req) -> dict:
    """One Research turn (agent/tasks._run_turn sends it here instead of the orchestrator)."""
    question = (req.message or "").strip()
    if not question:
        return {"status": "error", "message": "Say what to research."}
    if search_engine.turn_mode(shared.app_state["settings"]) == "disabled":
        return {"status": "error", "message": "Research needs web search, which is turned off (Settings > Web Search)."}
    if not shared.state_lock.acquire(blocking=False):
        return {"status": "error", "message": "Busy."}
    s = shared.app_state["settings"]
    model = llm._current_model()
    level = effort_mod.choose_level(s, model, shared.app_state.get("active_project"), shared.app_state.get("effort_session"))[0]
    if level == "auto":
        level = "extra"  # Research is a sustained multi-source workflow.
    from tools.dispatch import _browser_fallback        # imports this package's modules; resolved at call time
    tools = shared.app_state.get("turn_tools") or ()
    finance_mode = "invest" in tools or getattr(req, "command", None) == "invest"
    resumed = Research.load_unfinished(s, _browser_fallback(s)) if wants_resume(question) and not privacy.active() else None
    if wants_resume(question) and not resumed:
        shared.state_lock.release()
        why = ("A temporary chat's research isn't kept, so there's nothing to pick up."
               if privacy.active() else "There's no stopped or failed research from the last week to pick up.")
        return {"status": "error", "message": f"{why} Say what to research and I'll start a new one."}
    r = resumed or Research(question, level, s, browser_fallback=_browser_fallback(s), bias="biascheck" in tools,
                            depth=depth_of(req), finance_mode=finance_mode)
    # Every page read gets a number, plus the finance backbone's figures and filings: room for all of them.
    sources.current().limit = max(sources.MAX_SOURCES, min(sources.CITE_MAX, r.b["target"] * 2 + 150))
    run_control.begin(r.b["minutes"] * 60)
    what = "Investment research" if r.finance else "Research"
    try:
        shared.app_state["current_user_request"] = r.question
        shared.app_state["turn_policy"] = {"mode": "none", "allow_mutation": False, "reason": "research: web only"}
        shared.app_state["conversation"].append({"role": "user", "content": question})
        depth_note = f" · {DEPTH_LABELS[r.depth]}" if r.depth != "auto" else ""
        event_log.emit_activity("run_started", f"{'Resuming ' if resumed else ''}{what}{depth_note} · "
                                f"{effort_mod.LABELS.get(r.level, r.level)} effort", effort=r.level, task_id=r.task_id,
                                model=model, command="research", depth=r.depth)
        if resumed:
            questions = r.questions
            r._progress("searching", resumed=True)
        else:
            r._progress("planning")
            planned = plan(question, r.b["questions"], finance_mode=r.finance)
            r.title, r.tickers = planned["title"], planned.get("tickers") or []
            event_log.emit_activity("prep_waiting", "Waiting for you to check the research plan", card="plan",
                                    text="\n".join(q["q"] for q in planned["questions"]))
            run_control.set_deadline(24 * 3600)                     # reading the card is not run time
            answer = prompt_prep.gate.request("plan", {"task_id": r.task_id, "text": "\n".join(q["q"] for q in planned["questions"]),
                                                       "request": question, "command": "research", "risks": []},
                                              cancelled=run_control.is_stopped)
            run_control.checkpoint()
            run_control.set_deadline(r.b["minutes"] * 60)
            answer = answer or {"action": "cancel", "text": ""}
            if answer["action"] == "cancel":
                event_log.emit_activity("prep_answered", "Research cancelled before anything was searched", card="plan", action="cancel")
                event_log.update_event(r.progress_ev, status="done", research={**r.progress_ev["research"], "stage": "cancelled"})
                message = "Research cancelled - nothing was searched."
                shared.app_state["conversation"].append({"role": "assistant", "content": message})
                return {"status": "success", "message": message, "prep_cancelled": True, "task_id": r.task_id}
            questions = questions_from_text(answer["text"], planned["questions"], r.b["questions"] + 4) or planned["questions"]
            r.questions = questions
            event_log.emit_activity("plan_ready", f"Research plan · {len(questions)} question{'s' if len(questions) != 1 else ''}",
                                    steps=[q["q"] for q in questions][:8], accepted=True)
            if r.finance:                          # the numbers first: the factual backbone
                r._progress("backbone")
                data = r._tool("market_data", "Read company and economic data for investment research",
                               lambda: {"ok": True, "text": backbone(r.tickers)},
                               {"action": "company_and_macro", "symbols": r.tickers[:4], "range": "5y"})
                r.backbone = data.get("text", "") if data.get("ok") else ""
            r.save()
        r.gather(questions)
        if not r.notes and not r.backbone:
            r.save("failed")
            return _fail(r, "I couldn't find reliable sources for this - the searches returned nothing I could read. "
                            "Try rephrasing the question, or add a Brave or Tavily key in Settings > Web Search.")
        report = with_confirmation(r.write(r.title or question))
        r.save("done")
        event_log.update_event(r.progress_ev, status="done", research={**r.progress_ev["research"], "stage": "done"})
        event_log.emit_activity("verification_complete", f"Report written from {r.pages_ok} sources"
                                + (f" ({r.consolidations} note merges)" if r.consolidations else ""))
        shared.app_state["conversation"].append({"role": "assistant", "content": report})
        return {"status": "success", "message": report, "task_id": r.task_id,
                "research": {"title": r.title, "questions": [q["q"] for q in questions], "sources": r.pages_ok,
                             "effort": r.level, "depth": r.depth, "finance": r.finance}}
    except RunStopped as e:
        r.save("stopped")
        if r.progress_ev is not None:
            event_log.update_event(r.progress_ev, status="done", research={**r.progress_ev["research"], "stage": "stopped"})
        event_log.emit_activity("run_cancelled", "Time limit reached" if e.reason == "time" else "Stopped by you", level="warn")
        keep = "" if privacy.active() else " Its notes are kept: send **resume** with Research on to continue it."
        return {"status": "stopped", "message": f"Research stopped after {r.pages_ok} sources.{keep}", "task_id": r.task_id}
    except Exception as e:
        r.save("stopped")
        if r.progress_ev is not None:
            event_log.update_event(r.progress_ev, status="done", research={**r.progress_ev["research"], "stage": "failed"})
        event_log.emit_activity("run_failed", "The research failed", level="error", detail=str(e)[:200])
        return {"status": "error", "message": f"Research failed: {str(e)[:300]}", "task_id": r.task_id}
    finally:
        run_control.end()
        shared.state_lock.release()


def backbone(names: list) -> str:
    """Investment research: each company's official numbers and the economy, as labelled lines citing [n]."""
    mode = search_engine.turn_mode()
    parts = []
    with search_engine.scope(mode):
        for name in names[:4]:
            try:
                text, _ = finance.company_brief(name, "5y", include_news=True)
                parts.append(text)
            except (finance.DataError, Exception) as e:          # a name that isn't a listed company: the web covers it
                event_log.emit_activity("finance_data", f"No market data for {str(name)[:60]}", detail=str(e)[:160])
        try:
            macro = finance.macro_brief()
            if macro:
                parts.append(macro)
        except Exception:
            pass
    return "\n\n".join(parts)
