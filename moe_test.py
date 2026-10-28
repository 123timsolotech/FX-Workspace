"""
moe_test.py
===========
"Test the MoE cache" (1.4.2, Settings > Models): llama.cpp's --moe-cache-mib keeps recently used experts of a
mixture-of-experts model (Qwen3.5-35B-A3B and the like, whose experts sit in RAM with n-cpu-moe) in a VRAM cache.
On an 8-12 GB card it can also make writing SLOWER when the cache misses often - so it is measured, never assumed.

  1. the same prompt three times without the cache (prompt and writing speed separately, llama-server's own timings);
  2. the same with two or three cache sizes that fit in the free VRAM;
  3. a size is kept only if writing is at least 5% faster AND prompt speed doesn't drop; otherwise nothing changes.

About 15 minutes per model. Results are kept in data/moe_test.json and shown in Settings. The model is reloaded for
each size; chats wait meanwhile (the local runtime is reserved for each measurement).
"""
from __future__ import annotations

import json
import os
import threading
import time

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "moe_test.json")
RUNS = 3
GAIN = 1.05                  # writing at least 5% faster...
PROMPT_TOLERANCE = 0.98      # ...and prompt speed no more than measurement noise (2%) slower
HEADROOM_MB = 768
PROMPT = ("Summarise the following design notes for a Roblox obby in five bullet points, then suggest two new "
          "obstacles.\n\n" + "The obby has lava floors, moving platforms, checkpoints every ten stages, a timer, "
          "a leaderboard, and a final tower with a spinning beam. Players respawn at the last checkpoint. " * 12)
state = {"running": None, "progress": "", "error": None}
_lock = threading.Lock()


def _load() -> dict:
    try:
        with open(DATA, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(d: dict) -> None:
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    tmp = DATA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=1)
    os.replace(tmp, DATA)


def candidates() -> list:
    """Models with experts (a GGUF expert count, or n-cpu-moe set), with their current cache size."""
    import diagnostics
    import llama_local
    out = []
    for cfg in llama_local.list_model_configs():
        opts = llama_local.model_options(cfg["name"])
        meta = diagnostics.meta_for(cfg["model_path"]) if cfg.get("model_path") else None
        experts = (meta or {}).get("expert_count")
        if experts or opts.get("n-cpu-moe"):
            out.append({"name": cfg["name"], "experts": experts, "n_cpu_moe": opts.get("n-cpu-moe"),
                        "cache_mib": opts.get("moe-cache-mib")})
    return out


def results() -> dict:
    return {"state": dict(state), "models": candidates(), "results": _load()}


def decide(base: dict, tried: list) -> tuple:
    """(size to keep or None, why). base/tried rows: {"mib", "prompt_tps", "gen_tps"}."""
    good = [t for t in tried if t.get("gen_tps") and base.get("gen_tps")
            and t["gen_tps"] >= base["gen_tps"] * GAIN and t["prompt_tps"] >= base["prompt_tps"] * PROMPT_TOLERANCE]
    if not good:
        best = max((t for t in tried if t.get("gen_tps")), key=lambda t: t["gen_tps"], default=None)
        if best and base.get("gen_tps"):
            change = (best["gen_tps"] / base["gen_tps"] - 1) * 100
            return None, (f"No size helped enough: the best ({best['mib']} MiB) wrote {change:+.0f}% "
                          f"vs no cache - the setting stays off.")
        return None, "Nothing to compare - the setting stays as it was."
    best = max(good, key=lambda t: t["gen_tps"])
    return best["mib"], (f"Kept {best['mib']} MiB: writing {(best['gen_tps'] / base['gen_tps'] - 1) * 100:+.0f}%, "
                         f"prompt {(best['prompt_tps'] / base['prompt_tps'] - 1) * 100:+.0f}%.")


def sizes(free_mb: float) -> list:
    room = max(0.0, free_mb - HEADROOM_MB)
    out = sorted({int(room * f) // 256 * 256 for f in (0.25, 0.5, 0.75)})
    return [s for s in out if s >= 256][:3]


def _measure(name: str) -> dict:
    """RUNS identical completions; the averages of llama-server's own prompt and writing speeds."""
    import llama_local
    import local_runtime
    import netlayer
    p, g = [], []
    with local_runtime.reserve(), netlayer.local_client(timeout=600) as http:      # llama-server on this PC only
        for _ in range(RUNS):
            r = http.post(f"{llama_local.LLAMA_SERVER_BASE.rstrip('/')}/chat/completions",
                          json={"model": name, "messages": [{"role": "user", "content": PROMPT}], "max_tokens": 160,
                                "temperature": 0, "cache_prompt": False, "stream": False})
            r.raise_for_status()
            t = r.json().get("timings") or {}
            if t.get("prompt_per_second"):
                p.append(float(t["prompt_per_second"]))
            if t.get("predicted_per_second"):
                g.append(float(t["predicted_per_second"]))
    avg = lambda xs: round(sum(xs) / len(xs), 2) if xs else None      # noqa: E731
    return {"prompt_tps": avg(p), "gen_tps": avg(g)}


def _with_cache(name: str, mib: int | None) -> dict:
    import llama_local
    llama_local.set_model_options({name: {"moe-cache-mib": mib}}, backup=False)
    llama_local.unload_all()
    llama_local.force_load(name, timeout=600)
    return {"mib": mib, **_measure(name)}


def run(name: str) -> dict:
    """The whole test for one model (on the caller's thread)."""
    import diagnostics
    import llama_local
    if name not in {c["name"] for c in candidates()}:
        return {"ok": False, "message": f"{name} isn't a mixture-of-experts model."}
    before = llama_local.model_options(name).get("moe-cache-mib")
    llama_local.set_model_options({name: {}})                         # a models.ini backup before anything changes
    row = {"at": time.time(), "before": before}
    try:
        state["progress"] = "Without the cache"
        base = _with_cache(name, None)
        free = float((diagnostics.gpu_info() or {}).get("total_mb", 0)) - float((diagnostics.gpu_info() or {}).get("used_mb", 0))
        tried = []
        for i, mib in enumerate(sizes(free), 1):
            state["progress"] = f"Cache {mib} MiB ({i} of {len(sizes(free))})"
            try:
                tried.append(_with_cache(name, mib))
            except Exception as e:                                       # this size didn't load: noted, next one
                tried.append({"mib": mib, "error": str(e)[:200], "prompt_tps": None, "gen_tps": None})
        keep, why = decide(base, tried)
        llama_local.set_model_options({name: {"moe-cache-mib": keep if keep else before}}, backup=False)
        llama_local.unload_all()
        row.update(base=base, tried=tried, kept=keep, why=why, free_mb=round(free))
    except Exception as e:
        llama_local.set_model_options({name: {"moe-cache-mib": before}}, backup=False)
        row.update(error=str(e)[:300], why="The test stopped - the setting is back as it was.")
    d = _load()
    d[name] = row
    _save(d)
    import events as event_log
    event_log.log_action(f"MoE cache test for {name}: {row.get('why')}")
    return {"ok": "error" not in row, **row}


def start(name: str) -> dict:
    with _lock:
        if state["running"]:
            return {"ok": False, "message": f"Already testing {state['running']}."}
        state.update(running=name, progress="Starting", error=None)

    def work():
        try:
            r = run(name)
            state["error"] = None if r.get("ok") else r.get("message") or r.get("error")
        except Exception as e:
            state["error"] = str(e)[:300]
        finally:
            state.update(running=None, progress="")
    threading.Thread(target=work, daemon=True, name="benchmark-moe").start()
    return {"ok": True, "message": f"Testing {name} - about 15 minutes. Chats wait while it measures."}
