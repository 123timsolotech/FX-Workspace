"""
tools/search_eval.py - the Deep search evaluation (docs/plans/1.4.4.md, B10). A developer's check, not shipped.

    .venv\\Scripts\\python.exe tools\\search_eval.py [--configs web,sources,deep] [--limit N] [--out results.json]

Runs every question in tools/search_eval.json through each configuration, live, through the network layer's
current route (direct unless set otherwise), and measures:
    hit@5 / hit@10   a result in the top 5 / 10 matches the question's answer pattern
    found@10         how many of the top 10 match (more answering sources to cross-check)
    forum@10         how many matching ones are forum / Q&A / issue threads
    seconds          wall time for the search
Configurations:
    web       the web search alone, one query (what a normal reply does today)
    sources   the same query + the routed forum / Q&A sources (deep_sources), fused (RRF)
    deep      fan-out phrasings (up to 6) + the sources, fused
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


def run(cfg: str, q: str) -> tuple[list, float, bool]:
    """(results, seconds, did the web engine refuse)"""
    t = time.time()
    settings = {"search_cache_minutes": 0}
    if cfg == "web":
        got = search_engine.search(q, settings=settings, num_results=10)
    else:
        queries = search_engine.fanout(q, limit=6) if cfg == "deep" else [q]
        extra = deep_sources.search(q, queries[1:2], n=6)
        got = search_engine.search(queries[0], queries=queries[1:], settings=settings, num_results=10,
                                   extra_lists=extra["lists"], max_queries=6)
    refused = any(re.search(r"refused|paused|rate", w, re.I) for w in got["why"])
    return got["results"], time.time() - t, refused


def score(results: list, expect: str) -> dict:
    pat = re.compile(expect, re.I)
    match = [bool(pat.search(f"{r.get('title')} {r.get('snippet')} {r.get('url')}")) for r in results[:10]]
    forum = sum(1 for r, m in zip(results[:10], match) if m and (r.get("kind") in ("forum", "q&a", "issue")
                                                                   or THREADISH.search(r.get("url") or "")))
    return {"hit5": any(match[:5]), "hit10": any(match), "found10": sum(match), "forum10": forum}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--configs", default="web,sources,deep")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--out", default="")
    ap.add_argument("--gap", type=float, default=10.0, help="seconds between two runs - DuckDuckGo refuses bursts")
    a = ap.parse_args()
    qs = json.load(open(os.path.join(HERE, "tools", "search_eval.json"), encoding="utf-8"))["questions"]
    qs = qs[:a.limit] if a.limit else qs
    configs = a.configs.split(",")
    rows = []
    for item in qs:
        row = {"q": item["q"], "forum": item.get("forum", False)}
        for cfg in configs:
            time.sleep(a.gap)
            try:
                results, secs, refused = run(cfg, item["q"])
                row[cfg] = {**score(results, item["expect"]), "seconds": round(secs, 1), "n": len(results), "web_refused": refused}
            except Exception as e:
                row[cfg] = {"error": f"{type(e).__name__}: {str(e)[:120]}"}
            print(f"{cfg:8} {json.dumps(row[cfg])}  {item['q'][:60]}", flush=True)
        rows.append(row)
    print("\nconfig    hit@5   hit@10  found@10  forum@10  avg s   (forum questions: hit@10)")
    for cfg in configs:
        ok = [r[cfg] for r in rows if "error" not in r[cfg]]
        fq = [r[cfg] for r in rows if "error" not in r[cfg] and r["forum"]]
        if not ok:
            continue
        avg = lambda k, xs=ok: sum(float(x[k]) for x in xs) / len(xs)  # noqa: E731
        print(f"{cfg:8} {avg('hit5'):6.0%}  {avg('hit10'):6.0%}  {avg('found10'):8.1f}  {avg('forum10'):8.1f}  {avg('seconds'):5.1f}"
              f"   {avg('hit10', fq) if fq else 0:6.0%} of {len(fq)}   ({len(rows) - len(ok)} errors, "
              f"web engine refused {sum(1 for x in ok if x.get('web_refused'))})")
    if a.out:
        json.dump(rows, open(a.out, "w", encoding="utf-8"), indent=1)


if __name__ == "__main__":
    main()
