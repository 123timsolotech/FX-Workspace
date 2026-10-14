"""
benchmark.py
============
A quick benchmark per local model (Phase 5): how reliably it makes valid tool
calls, how fast it answers and how much of its context window it can really
use - measured against the real llama-server through the same constrained-call
path and parser the agent uses. Results are saved in data/benchmarks.json and
copied into the model's detected capabilities (model_caps), where they feed
effort capability detection (effort.supported_levels uses tool_call_validity)
and automatic model routing (model_router) - so choices rest on measurements,
never on model names.

    {"version": 1, "models": {"<model>": {
        "at", "server_build", "tool_call_validity": 0..1 | None, "cases": [...],
        "cut_off": n, "latency_ms": {"median", "p90"}, "tokens_per_s", "tokens_per_s_basis",
        "context": {"detected", "usable", "at_least", "probes": [...]}, "duration_s"}}}

The cases are asked with the tools a real local turn offers (`tools`, the
agent's own schemas - tools.schemas) and judged against those schemas. A reply
cut off by the length limit is reported as "cut off", not as an invalid call;
validity is over the replies that finished. Usable context: needle probes at
fractions of the window the server reports, never larger than the window; the
size recorded is the prompt's real token count when the server reports it,
and a result limited by the probe cap is a lower bound ("at least").

The benchmark is pure logic around an injected `complete(messages, tools,
max_tokens)` -> (tool_calls [(name, args)], text, usage, finish) so it is
tested offline; agent/benchmark_runner.py wires the real one.
"""
from __future__ import annotations

import json
import os
import statistics
import threading
import time

import atomic_io
import tool_registry

BENCH_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "benchmarks.json")
_lock = threading.Lock()
REPLY_TOKENS = 2048              # the room a local turn's reply gets (config.LOCAL_MAX_TOKENS)
PROBE_REPLY_TOKENS = 512         # a needle answer is one word, but a reasoning model thinks first
PROBE_CAP = 32_768               # longest probe; beyond it the result is a lower bound

# Small, unambiguous tool-use tasks, each naming the tool that must be called
# and a check of its arguments. Nothing is executed - the calls are only read.
CASES = [
    {"id": "read", "prompt": "Read the file C:\\Users\\me\\notes.txt and tell me what it says.",
     "tool": "read_file", "check": lambda a: "notes.txt" in str(a.get("filename", ""))},
    {"id": "write", "prompt": "Create C:\\Users\\me\\hello.txt containing exactly the text: hi there",
     "tool": "write_file", "check": lambda a: "hello.txt" in str(a.get("filename", "")) and "hi there" in str(a.get("content", ""))},
    {"id": "shell", "prompt": "Show the current date using a PowerShell command.",
     "tool": "run_powershell", "check": lambda a: "date" in str(a.get("command", "")).lower()},
    {"id": "list", "prompt": "What files are in C:\\Projects ?",
     "tool": "list_dir", "check": lambda a: "projects" in str(a.get("directory", "")).lower()},
    {"id": "url", "prompt": "Open https://create.roblox.com/docs in the browser.",
     "tool": "open_url", "check": lambda a: "create.roblox.com" in str(a.get("url", ""))},
    {"id": "patch", "prompt": ("In the Roblox script ServerScriptService/Lava, change the line `local damage = 10` "
                               "to `local damage = 25`."),
     "tool": "roblox_edit_script",
     "check": lambda a: "Lava" in str(a.get("path", "")) and any("25" in str(e.get("replace", "")) for e in a.get("edits") or [] if isinstance(e, dict))},
]
CASE_TOOLS = sorted({c["tool"] for c in CASES})


def case_tools() -> list:
    """The agent's own schemas for the case tools (a caller normally passes the
    whole list a real turn offers)."""
    import tools.schemas as schemas
    return [t for t in schemas.TOOLS if t["function"]["name"] in CASE_TOOLS]


def _needle_prompt(tokens: int) -> str:
    """About `tokens` tokens of filler with a secret word at the start: the
    model must find it (a usable-context probe)."""
    filler = "The obby has lava, checkpoints and a timer. " * max(1, int(tokens / 11))
    return (f"Remember this secret word: PAPAYA-{tokens}.\n\n{filler}\n\n"
            "What was the secret word at the very start? Answer with the word only.")


def _call(complete, messages, tools, max_tokens):
    out = complete(messages, tools, max_tokens)
    calls, text, usage = out[0], out[1], out[2]
    finish = out[3] if len(out) > 3 else None
    return calls or [], text or "", usage or {}, finish


def run(model: str, complete, detected_context: int | None = None, clock=time.time, progress=None,
        context_probes: tuple = (0.5, 0.9), max_probe_tokens: int = PROBE_CAP, tools: list | None = None,
        system: str | None = None) -> dict:
    """Benchmark one model. Never raises for a model error: a failed case is
    invalid; `progress` may raise to cancel."""
    started = clock()
    progress = progress or (lambda *a: None)
    tools = tools if tools is not None else case_tools()
    schemas = {t["function"]["name"]: t["function"].get("parameters") or {} for t in tools}
    system = system or ("You control a Windows PC through the tools you are given. For each request, call exactly one "
                        "tool with complete, correct arguments.")
    cases, latencies, rates, basis = [], [], [], "end-to-end"
    for i, case in enumerate(CASES, 1):
        progress(f"Tool call {i} of {len(CASES)}")
        t0 = clock()
        try:
            calls, text, usage, finish = _call(complete, [{"role": "system", "content": system},
                                                          {"role": "user", "content": case["prompt"]}], tools, REPLY_TOKENS)
            error = None
        except Exception as e:
            if type(e).__name__ == "Cancelled":
                raise
            calls, text, usage, finish, error = [], "", {}, None, str(e)[:200]
        ms = int(round((clock() - t0) * 1000))
        if error is None:
            latencies.append(ms)
            if usage.get("predicted_per_second"):                  # llama-server's own generation speed
                rates.append(float(usage["predicted_per_second"]))
                basis = "server"
            elif usage.get("completion_tokens") and ms:
                rates.append(usage["completion_tokens"] / (ms / 1000))
        verdict, why = "invalid", error or "no tool call"
        if calls:
            name, args = calls[0]
            _clean, errors = tool_registry.validate_args(schemas.get(name, {}), args if isinstance(args, dict) else {})
            if name != case["tool"]:
                why = f"called {name} instead of {case['tool']}"
            elif not isinstance(args, dict) or errors:
                why = "arguments did not match the schema: " + "; ".join(errors or ["not an object"])
            elif not case["check"](args):
                why = "arguments were wrong"
            else:
                verdict, why = "valid", ""
        if verdict != "valid" and finish == "length":
            verdict, why = "cut_off", "the reply hit the length limit before a complete call"
        cases.append({"id": case["id"], "verdict": verdict, "why": why, "ms": ms})
    judged = [c for c in cases if c["verdict"] != "cut_off"]
    valid = sum(1 for c in judged if c["verdict"] == "valid")

    probes, usable, at_least = [], None, False
    window = int(detected_context or 0)
    if window:
        # (size, limited by the cap) per fraction of the window; equal sizes probed once.
        sizes: dict = {}
        for f in context_probes:
            raw = int(window * f)
            size = min(raw, max_probe_tokens, window - PROBE_REPLY_TOKENS - 256)
            sizes[size] = sizes.get(size, False) or raw > max_probe_tokens
        for size in sorted(s for s in sizes if s >= 512):
            capped = sizes[size]
            for attempt in range(2):
                progress(f"Context probe: {size:,} tokens")
                t0 = clock()
                probe = {"tokens": size, "estimated": True}
                try:
                    _calls, text, usage, finish = _call(complete, [{"role": "user", "content": _needle_prompt(size)}],
                                                        [], PROBE_REPLY_TOKENS)
                    if usage.get("prompt_tokens"):
                        probe.update(tokens=int(usage["prompt_tokens"]), estimated=False)
                    probe["ok"] = f"PAPAYA-{size}".lower() in text.lower() or "papaya" in text.lower()
                    if not probe["ok"] and finish == "length":
                        probe.update(ok=None, cut_off=True)
                except Exception as e:
                    if type(e).__name__ == "Cancelled":
                        raise
                    probe.update(ok=False, error=str(e)[:200])
                probe["ms"] = int(round((clock() - t0) * 1000))
                # The server refused a prompt longer than its window (our token estimate was
                # low): not a lost needle - try once more a fifth smaller.
                too_long = "exceed" in str(probe.get("error", "")).lower() and "context" in str(probe.get("error", "")).lower()
                if too_long and attempt == 0:
                    probe["too_long"] = True
                    probes.append(probe)
                    size = int(size * 0.8)
                    continue
                break
            probes.append(probe)
            if probe.get("ok"):
                usable, at_least = probe["tokens"], capped
            elif probe.get("ok") is False:
                at_least = False
                break
            else:
                break                                   # cut off: can't judge larger windows either
        if usable is not None and window:
            usable = min(usable, window)
    result = {"at": time.time(), "model": model,
              "tool_call_validity": round(valid / len(judged), 3) if judged else None,
              "cases": cases, "cut_off": len(cases) - len(judged),
              "latency_ms": {"median": int(statistics.median(latencies)) if latencies else None,
                             "p90": int(sorted(latencies)[max(0, int(len(latencies) * 0.9) - 1)]) if latencies else None},
              "tokens_per_s": round(statistics.median(rates), 1) if rates else None, "tokens_per_s_basis": basis,
              "context": {"detected": window or None, "usable": usable, "at_least": bool(usable and at_least),
                          "probes": probes},
              "duration_s": round(clock() - started, 1)}
    return result


# ---- storage --------------------------------------------------------------------------------
def load(path: str | None = None) -> dict:
    with _lock:
        return _load(path)


def _load(path: str | None = None) -> dict:
    try:
        with open(path or BENCH_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_result(result: dict, server_build: str | None = None, path: str | None = None) -> dict:
    path = path or BENCH_PATH
    with _lock:
        data = _load(path)
        models = data.setdefault("models", {})
        models[result["model"]] = {**result, "server_build": server_build}
        data["version"] = 1
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        atomic_io.replace(tmp, path)
    return models[result["model"]]


def get(model: str, path: str | None = None) -> dict | None:
    return (load(path).get("models") or {}).get(model)


MEASURED = ("tool_call_validity", "latency_ms", "tokens_per_s", "usable_context", "usable_context_min", "benchmarked_at")


def caps_from(result: dict) -> dict:
    """What a benchmark tells model_caps (and through it, effort and routing).
    Every measured field is present - None where this run measured nothing, so
    a re-run never leaves an older value standing."""
    ctx = result.get("context") or {}
    usable = ctx.get("usable")
    return {"tool_call_validity": result.get("tool_call_validity"),
            "latency_ms": (result.get("latency_ms") or {}).get("median"),
            "tokens_per_s": result.get("tokens_per_s"),
            "usable_context": usable if usable and not ctx.get("at_least") else None,
            "usable_context_min": usable if usable and ctx.get("at_least") else None,
            "benchmarked_at": result.get("at")}
