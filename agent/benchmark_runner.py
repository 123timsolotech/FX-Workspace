"""
agent/benchmark_runner.py
=========================
Runs benchmark.py against a real local model (Settings > Models > Benchmark):
the model is loaded through llama-server's router like any other, and every
case goes through the SAME request path a turn uses - the agent's system
prompt and the tools a local turn offers, the reply budget of a turn,
`_complete_with_protocol` (the tool-call constraint), the streaming client,
the cut-off retry, `_unwrap_schema_reply` and `parsing.parse_response` - so
the numbers describe what the agent will actually get.

The result is stored in data/benchmarks.json and replaces the model's
measurements in model_caps: effort capability detection reads
`tool_call_validity`, the model registry and routing read validity, latency,
speed and usable context - measurements, never model names. A llama-server
upgrade clears them (model_caps re-detects per build); the Models tab then
marks the stored result as from an older build.

One benchmark at a time, and never during a task: it holds the turn lock
while it runs (a chat, routine or suggestion meanwhile is told a benchmark is
running). Cancel interrupts the request in flight. Loading the model does not
count as a model switch in the Activity feed, and the cloud model the UI shows
is left alone. Nothing here executes a tool - the model's calls are only read.
"""
from __future__ import annotations

import threading
import time

import benchmark
import llama_local
import llm_stream
import model_caps
import system_metrics

import agent.models as llm
import agent.parsing as parsing
import agent.prompts as prompts
import core.state as shared
import events as event_log
import tools.schemas as schemas

_lock = threading.Lock()
state = {"running": None, "progress": "", "started": None, "error": None, "cancel": False, "last": None}


class Cancelled(Exception):
    pass


def _cancelled() -> bool:
    return bool(state["cancel"])


def _usage(res) -> dict:
    usage = getattr(res, "usage", None)
    get = (lambda k: usage.get(k)) if isinstance(usage, dict) else (lambda k: getattr(usage, k, None))
    out = {"completion_tokens": get("completion_tokens"), "prompt_tokens": get("prompt_tokens")} if usage else {}
    timings = getattr(res, "timings", None)
    if isinstance(timings, dict) and timings.get("predicted_per_second"):
        out["predicted_per_second"] = timings["predicted_per_second"]      # llama-server's generation speed
    return out


def offered_tools(window: int) -> list:
    """What a local action turn offers with Roblox Studio connected (so every
    case's tool is there): the compact set below 32k tokens, else the full one."""
    if window and window >= schemas.LOCAL_FULL_TOOLS_MIN_CTX:
        return [t for t in schemas.TOOLS if t["function"]["name"] not in schemas._CONDITIONAL_TOOLS]
    return list(schemas.TOOLS_LOCAL_ROBLOX)


def make_complete(model: str):
    """benchmark.run's `complete`: one request through the agent's own path.
    Thinking is off (as at Quick/Balanced); a reply cut off by the length
    limit gets the same one retry a turn gets."""
    def complete(messages, tools, max_tokens):
        if _cancelled():
            raise Cancelled()
        kwargs = dict(model=f"openai/{model}", messages=messages, temperature=0.0, top_p=1.0,
                      max_tokens=max(int(max_tokens), 64), timeout=300,
                      extra_body={"cache_prompt": False, "chat_template_kwargs": {"enable_thinking": False}})
        try:
            res, attempt, protocol = llm._complete_with_protocol(kwargs, model, tools, cancel=_cancelled)
            if tools:
                res = llm._retry_if_cut_off(res, attempt, cancel=_cancelled)
                if protocol in ("schema", "text"):
                    res = llm._unwrap_schema_reply(res, tools)
        except llm_stream.StreamCancelled:
            raise Cancelled()
        choice = res.choices[0]
        if tools:
            parsed, prose = parsing.parse_response(choice.message)
            calls, text = [(name, args) for _tc, name, args in parsed], prose
        else:
            calls, text = [], getattr(choice.message, "content", "") or ""
        return calls, text or "", _usage(res), getattr(choice, "finish_reason", None)
    return complete


def _progress(text: str):
    if _cancelled():
        raise Cancelled()
    state["progress"] = text


def busy_message() -> str | None:
    """Why the app can't start something right now, if a benchmark is the reason."""
    return f"Busy - benchmarking {state['running']}. Wait for it or cancel it in Settings > Models." if state["running"] else None


def start(model: str, runner=None) -> dict:
    """Start a benchmark on a background thread. `runner(model, progress)`
    replaces the model loading and requests in tests."""
    if not model:
        return {"ok": False, "message": "Pick a local model."}
    with _lock:
        if state["running"]:
            return {"ok": False, "message": f"Already benchmarking {state['running']}."}
        if not shared.state_lock.acquire(blocking=False):
            return {"ok": False, "message": "Busy - a task is running. Run the benchmark when it has finished."}
        state.update(running=model, progress="Loading the model", started=time.time(), error=None, cancel=False, last=model)
        try:
            thread = threading.Thread(target=_run, args=(model, runner), daemon=True, name="benchmark")
            thread.start()
        except Exception as e:                       # never leave the lock taken
            state.update(running=None, progress="", error=str(e)[:200])
            shared.state_lock.release()
            return {"ok": False, "message": f"Could not start the benchmark: {e}"}
    return {"ok": True, "message": f"Benchmarking {model}.", "thread": thread}


def cancel() -> bool:
    if not state["running"]:
        return False
    state["cancel"] = True
    state["progress"] = "Cancelling"
    return True


def _load(model: str) -> int:
    """Load the model; returns its context window as the server reports it."""
    if llama_local.server_models(force=True) is None:
        raise RuntimeError(llama_local.unreachable_message())
    system_metrics.begin_model_load()
    llama_local.force_load(model, timeout=180)
    system_metrics.finish_model_load(model)
    cloud_label = shared.app_state.get("last_working_model")
    shared.app_state["_handoff_in_progress"] = True      # loading it for a benchmark is not a model switch
    try:
        llm.engage_local_model(model)
    finally:
        shared.app_state["_handoff_in_progress"] = False
        shared.app_state["last_working_model"] = cloud_label
    llm._detect_local_caps(model, just_used=True)         # /props: the real window, the tool protocol
    return int(model_caps.get(model).get("context") or llama_local.router_status(model).get("ctx")
               or llama_local.model_context_limit(model) or 0)


def _run(model: str, runner=None):
    try:
        event_log.emit_activity("benchmark_started", f"Benchmarking {model}", model=model)
        if runner is None:
            window = _load(model)
            result = benchmark.run(model, make_complete(model), detected_context=window, progress=_progress,
                                   tools=offered_tools(window), system=prompts.SYSTEM_PROMPT_COMPACT)
        else:
            result = runner(model, _progress)
        if _cancelled():                                  # cancelled during the last request: keep nothing
            raise Cancelled()
        stored = benchmark.save_result(result, server_build=model_caps.get(model).get("server_build"))
        model_caps.set_measurements(model, benchmark.caps_from(stored))
        valid = sum(1 for c in stored["cases"] if c["verdict"] == "valid")
        judged = sum(1 for c in stored["cases"] if c["verdict"] != "cut_off")
        lat = (stored.get("latency_ms") or {}).get("median")
        event_log.emit_activity("benchmark_done", f"Benchmark {model}: {valid} of {judged} tool calls valid"
                                + (f", {stored['cut_off']} cut off" if stored.get("cut_off") else "")
                                + (f", median reply {lat} ms" if lat is not None else ""), model=model,
                                detail="; ".join(f"{c['id']}: {c['verdict']}" + (f" ({c['why']})" if c["why"] else "")
                                                 for c in stored["cases"]))
    except Cancelled:
        state["error"] = "Cancelled."
        event_log.emit_activity("benchmark_failed", f"Benchmark of {model} cancelled", model=model, level="warn")
    except Exception as e:
        state["error"] = str(e)[:300]
        event_log.emit_activity("benchmark_failed", f"Benchmark of {model} failed", model=model, level="error",
                                detail=str(e)[:300])
    finally:
        state.update(running=None, progress="", cancel=False)
        shared.state_lock.release()


def public() -> dict:
    """Stored results, each marked `in_use` when it is what the model's
    capabilities (and so effort and routing) are using now - a llama-server
    upgrade clears those, and the stored result is then from an older build."""
    results = benchmark.load().get("models") or {}
    for name, r in results.items():
        caps = model_caps.get(name)
        r["in_use"] = caps.get("benchmarked_at") == r.get("at")
        r["current_build"] = caps.get("server_build")
    return {"running": state["running"], "progress": state["progress"], "started": state["started"],
            "error": state["error"], "last": state["last"], "results": results, "cases": len(benchmark.CASES)}
