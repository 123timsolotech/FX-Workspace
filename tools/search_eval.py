"""
tools/search_eval.py - the Deep search evaluation (docs/plans/1.4.4.md, B10). A developer's check, not shipped.

    .venv\\Scripts\\python.exe -u tools\\search_eval.py [--tor] [--gap 20] [--limit N] [--out results.json]

For every question in tools/search_eval.json it collects, live and once:
    the web search for each of Deep's phrasings (search_engine.fanout, at most DEEP_WEB_QUERIES), spaced by --gap
    seconds - DuckDuckGo refuses bursts, and an earlier version of this check got this PC's address dropped by it;
    the routed forum / Q&A sources (deep_sources).
Then it compares, on exactly the same results:
    web       the first query's web results alone (what a normal reply gets today)
    sources   + the forum / Q&A sources, fused (RRF) and ranked
    deep      all of Deep's web phrasings + the sources, fused and ranked
and measures hit@5 / hit@10 (a result matches the question's answer pattern), found@10 (how many match), forum@10
(how many matching ones are threads), plus how long each part took. --tor searches through the built-in Tor
(DuckDuckGo's onion service), so the evaluation's many searches never touch this PC's own address.
"""
import argparse
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.chdir(HERE)

import deep_sources        # noqa: E402
import search_engine       # noqa: E402

THREADISH = re.compile(r"news\.ycombinator\.com|stackoverflow\.com|stackexchange\.com|superuser\.com|askubuntu\.com|"
                       r"serverfault\.com|reddit\.com|lemmy|/t/|forum|discuss|community\.|github\.com/.+/(issues|pull|discussions)|"
                       r"answers\.microsoft\.com|steamcommunity\.com", re.I)


def ranked(lists: list, queries: list) -> list:
    return search_engine.rank(search_engine.fuse(lists), queries, any(search_engine.time_sensitive(q) for q in queries))


def score(results: list, expect: str) -> dict:
    pat = re.compile(expect, re.I)
    match = [bool(pat.search(f"{r.get('title')} {r.get('snippet')} {r.get('url')}")) for r in results[:10]]
    forum = sum(1 for r, m in zip(results[:10], match) if m and (r.get("kind") in ("forum", "q&a", "issue")
                                                                   or THREADISH.search(r.get("url") or "")))
    return {"hit5": any(match[:5]), "hit10": any(match), "found10": sum(match), "forum10": forum}


def collect(question: str, gap: float) -> dict:
    queries = search_engine.fanout(question, limit=search_engine.DEEP_WEB_QUERIES)
    web, why, web_s = [], [], []
    for i, q in enumerate(queries):
        if i:
            time.sleep(gap)
        t = time.time()
        got = search_engine.search(q, settings={"search_cache_minutes": 0}, num_results=10)
        web_s.append(time.time() - t)
        web.append(got["results"])
        why += got["why"]
    t = time.time()
    src = deep_sources.search(question, queries[1:2], n=6)
    return {"queries": queries, "web": web, "web_s": web_s, "src": src["lists"], "src_used": src["used"],
            "src_why": src["why"], "src_s": time.time() - t, "web_why": why}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="")
    ap.add_argument("--gap", type=float, default=20.0, help="seconds between two web searches")
    ap.add_argument("--tor", action="store_true", help="search through the built-in Tor (DuckDuckGo's onion service)")
    a = ap.parse_args()
    if a.tor:
        import netlayer
        import tor_service
        tor_settings = {"net_route": "direct", "net_components": {"search": "tor"}}
        netlayer.configure(lambda: tor_settings)
        netlayer.start_builtin_tor()
        if not tor_service.wait_ready(90):
            sys.exit(f"Tor didn't connect: {tor_service.status()}")
    qs = json.load(open(os.path.join(HERE, "tools", "search_eval.json"), encoding="utf-8"))["questions"]
    qs = qs[:a.limit] if a.limit else qs
    rows = []
    for k, item in enumerate(qs):
        if k:
            time.sleep(a.gap)
        c = collect(item["q"], a.gap)
        refused = any(re.search(r"refused|paused|reached|rate", w, re.I) for w in c["web_why"])
        row = {"q": item["q"], "forum": item.get("forum", False), "web_refused": refused, "sources": c["src_used"],
               "web_s": round(sum(c["web_s"]) / len(c["web_s"]), 1), "src_s": round(c["src_s"], 1)}
        for cfg, lists, queries in (("web", c["web"][:1], c["queries"][:1]),
                                    ("sources", c["web"][:1] + c["src"], c["queries"][:1]),
                                    ("deep", c["web"] + c["src"], c["queries"])):
            row[cfg] = score(ranked(lists, queries), item["expect"])
        row["raw"] = {"queries": c["queries"], "web": c["web"], "src": c["src"]}     # to tune the ranking offline
        rows.append(row)
        print(json.dumps({k2: row[k2] for k2 in ("web", "sources", "deep", "web_refused", "web_s", "src_s")}), item["q"][:60], flush=True)
    n, fq = len(rows), [r for r in rows if r["forum"]]
    print(f"\n{n} questions ({len(fq)} answered mainly in forums); web engine refused or unreachable on "
          f"{sum(r['web_refused'] for r in rows)}; web search {sum(r['web_s'] for r in rows) / n:.1f} s a query, "
          f"forum / Q&A sources {sum(r['src_s'] for r in rows) / n:.1f} s (in parallel with it in the app)")
    print("config    hit@5   hit@10  found@10  forum@10   forum questions: hit@10")
    for cfg in ("web", "sources", "deep"):
        avg = lambda key, xs=rows: sum(float(r[cfg][key]) for r in xs) / max(1, len(xs))  # noqa: E731
        print(f"{cfg:8} {avg('hit5'):6.0%}  {avg('hit10'):6.0%}  {avg('found10'):8.1f}  {avg('forum10'):8.1f}   {avg('hit10', fq):6.0%}")
    if a.out:
        json.dump(rows, open(a.out, "w", encoding="utf-8"), indent=1)


if __name__ == "__main__":
    main()
