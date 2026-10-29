"""
model_caps.py
=============
Per-model runtime capabilities of the local llama-server, detected from the
server and from observed replies - never guessed from model names.

  tool_protocol   how tool calls are constrained for this model:
                    "native"  - the chat template supports tool calls; llama-server
                                (--jinja) applies its grammar to the call arguments
                    "generic" - llama-server accepted `tools` but the template has no
                                native format; the server's generic JSON grammar applies
                    "schema"  - the server refused `tools`; this app sends the tool
                                catalogue in the prompt and constrains the whole reply
                                with a JSON schema (response_format)
                    "text"    - no constraint available; replies are parsed leniently
                    None      - not detected yet
  reasoning       "channel" (reasoning_content field), "inline" (<think> in content),
                  "template" (template can think, not observed yet), "none", or None
  context         the context window the server reports for the model (n_ctx)
  json_schema     whether response_format json_schema was accepted (None = untried)

Results are cached in memory and in data/model_caps.json, keyed by model and
server build, so a llama-server upgrade re-detects.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import threading
import time

import atomic_io

CAPS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "model_caps.json")
PROTOCOLS = ("native", "generic", "schema", "text")
_lock = threading.Lock()
_cache: dict | None = None


def _load() -> dict:
    global _cache
    if _cache is None:
        try:
            with open(CAPS_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            _cache = data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            _cache = {}
    return _cache


def _save():
    try:
        os.makedirs(os.path.dirname(CAPS_PATH), exist_ok=True)
        tmp = CAPS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cache or {}, f, indent=2)
        atomic_io.replace(tmp, CAPS_PATH)
    except OSError:
        pass


def get(model: str) -> dict:
    with _lock:
        return dict(_load().get(model) or {})


def all_caps() -> dict:
    with _lock:
        return {k: dict(v) for k, v in _load().items()}


def update(model: str, **fields) -> dict:
    """Merge observed facts into a model's record (None values are ignored)."""
    if not model:
        return {}
    with _lock:
        rec = dict(_load().get(model) or {"model": model})
        changed = False
        for k, v in fields.items():
            if v is not None and rec.get(k) != v:
                rec[k] = v
                changed = True
        if changed:
            rec["updated_at"] = time.time()
            _load()[model] = rec
            _save()
        return dict(rec)


def set_measurements(model: str, fields: dict) -> dict:
    """Replace a model's benchmark measurements: a field that is None is
    removed (this run measured nothing for it), so an older value never
    stands next to a newer run."""
    if not model:
        return {}
    with _lock:
        rec = dict(_load().get(model) or {"model": model})
        for k, v in fields.items():
            if v is None:
                rec.pop(k, None)
            else:
                rec[k] = v
        rec["updated_at"] = time.time()
        _load()[model] = rec
        _save()
        return dict(rec)


def forget(model: str | None = None):
    with _lock:
        if model is None:
            _load().clear()
        else:
            _load().pop(model, None)
        _save()


# ---- detection from the server ------------------------------------------------
_THINK_TEMPLATE_RE = re.compile(r"<think>|enable_thinking|reasoning_content|thinking", re.I)


def from_props(props: dict) -> dict:
    """Facts from a llama-server /props response. Missing fields stay None."""
    if not isinstance(props, dict):
        return {}
    caps = props.get("chat_template_caps") if isinstance(props.get("chat_template_caps"), dict) else {}
    template = props.get("chat_template") if isinstance(props.get("chat_template"), str) else ""
    settings = props.get("default_generation_settings") if isinstance(props.get("default_generation_settings"), dict) else {}
    n_ctx = settings.get("n_ctx") if isinstance(settings.get("n_ctx"), int) else None
    supports = caps.get("supports_tool_calls")
    if supports is None:
        supports = caps.get("supports_tools")
    out = {
        "server_build": str(props.get("build_info") or "") or None,
        "template_hash": hashlib.sha256(template.encode()).hexdigest() if template else None,
        "context": n_ctx,
        "template_tools": bool(supports) if supports is not None else None,
        "parallel_tool_calls": caps.get("supports_parallel_tool_calls") if isinstance(caps.get("supports_parallel_tool_calls"), bool) else None,
        # Recent builds say what the loaded model accepts (a projector = images).
        "vision": props["modalities"].get("vision") if isinstance(props.get("modalities"), dict)
        and isinstance(props["modalities"].get("vision"), bool) else None,
    }
    if template and _THINK_TEMPLATE_RE.search(template):
        out["reasoning_template"] = True
    return out


def detect(model: str, root_url: str, http_get, loaded: bool) -> dict:
    """Ask the server about `model`. Only queried when the model is already
    loaded (a /props request for an unloaded model could make the router
    load it). `http_get(url, timeout)` returns parsed JSON or raises."""
    if not model or not loaded:
        return get(model)
    known = get(model)
    try:
        props = http_get(f"{root_url}/props?model={model}", 5)
    except Exception:
        props = None
    if not isinstance(props, dict) or not props:
        return known                                     # server unreachable or no answer: learn nothing
    facts = from_props(props)
    if any(facts.get(key) and known.get(key) and facts[key] != known[key]
           for key in ("server_build", "template_hash")):
        forget(model)                                    # new llama-server build: start over
        known = {}
    protocol = known.get("tool_protocol")
    if protocol not in ("schema", "text"):              # a refusal observed at runtime wins over props
        if facts.get("template_tools") is True:
            protocol = "native"
        elif facts.get("template_tools") is False:
            protocol = "generic"
    reasoning = known.get("reasoning")
    if reasoning in (None, "none") and facts.get("reasoning_template"):
        reasoning = "template"
    return update(model, server_build=facts.get("server_build"), context=facts.get("context"),
                  template_hash=facts.get("template_hash"),
                  template_tools=facts.get("template_tools"), parallel_tool_calls=facts.get("parallel_tool_calls"),
                  tool_protocol=protocol, reasoning=reasoning, detected_at=time.time())


# ---- facts learned from requests and replies -------------------------------------
_TOOLS_REFUSED_RE = re.compile(r"tools? param.*(jinja|not supported)|--jinja|tool.?call.*not supported", re.I)
_SCHEMA_REFUSED_RE = re.compile(r"response_format|json_schema|grammar", re.I)


def tools_refused(error_text: str) -> bool:
    return bool(_TOOLS_REFUSED_RE.search(error_text or ""))


def schema_refused(error_text: str) -> bool:
    return bool(_SCHEMA_REFUSED_RE.search(error_text or ""))


def observe_reply(model: str, message) -> None:
    """Record what a reply actually showed about the reasoning channel."""
    if not model or message is None:
        return
    reasoning_field = getattr(message, "reasoning_content", None)
    content = getattr(message, "content", None) or ""
    if isinstance(reasoning_field, str) and reasoning_field.strip():
        update(model, reasoning="channel")
    elif isinstance(content, str) and re.search(r"<\s*(think|thinking|reasoning)\s*>", content, re.I):
        if get(model).get("reasoning") != "channel":
            update(model, reasoning="inline")


def protocol(model: str) -> str | None:
    return get(model).get("tool_protocol")


def describe(model: str) -> str:
    """One line for the Activity feed."""
    p = protocol(model)
    return {
        "native": "Tool calls constrained by llama-server's grammar (native template format)",
        "generic": "Tool calls constrained by llama-server's generic JSON grammar",
        "schema": "Tool calls constrained by a JSON schema on the whole reply",
        "text": "Tool calls not constrained by the server - parsed leniently",
    }.get(p, "Tool-call support not detected yet")


# ---- the "schema" protocol: our own constraint ------------------------------------
def reply_schema(tools: list) -> dict:
    """JSON schema for one reply: either {"tool", "arguments"} for one of the
    offered tools, or {"answer"}. Sent as response_format when the server
    refuses native `tools`."""
    options = []
    for t in tools or []:
        fn = t.get("function") or {}
        params = fn.get("parameters") or {"type": "object", "properties": {}}
        options.append({"type": "object", "additionalProperties": False, "required": ["tool", "arguments"],
                        "properties": {"tool": {"const": fn.get("name")}, "arguments": params}})
    options.append({"type": "object", "additionalProperties": False, "required": ["answer"],
                    "properties": {"answer": {"type": "string"}}})
    return {"oneOf": options}


def catalogue_prompt(tools: list) -> str:
    """The tool list as text, for models that get it in the prompt."""
    lines = ["You can call these tools. Reply with ONLY a JSON object: either "
             '{"tool": "<name>", "arguments": {...}} to call one tool, or {"answer": "<text>"} to reply to the user.']
    for t in tools or []:
        fn = t.get("function") or {}
        props = (fn.get("parameters") or {}).get("properties") or {}
        req = set((fn.get("parameters") or {}).get("required") or [])
        args = ", ".join(f"{k}{'' if k in req else '?'}: {v.get('type', 'any')}" for k, v in props.items())
        lines.append(f"- {fn.get('name')}({args}): {fn.get('description', '')[:160]}")
    return "\n".join(lines)
