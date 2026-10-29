"""
agent/parsing.py
================
Tool-call parsing: native calls, calls printed as text, malformed/truncated arguments.
(Split out of main.py in Phase 2 - see ARCHITECTURE.md D14.)
"""
from __future__ import annotations

import ast
import json
import re
import intent_router
import llama_local
import model_output

import events as event_log
import tools.schemas as schemas


def _restrict_to_favorites(names: list, favorites: list) -> list:
    """Empty favorites = no restriction. Non-empty favorites = ONLY those
    (matched case-sensitively for cloud model ids, and normalized the same
    way resolve_candidates() matches local model ids, since a locally
    reported id and the name the user picked it by can differ in case/
    punctuation)."""
    if not favorites:
        return names
    exact = set(favorites)
    normalized = {llama_local.normalize_name(f) for f in favorites}
    return [n for n in names if n in exact or llama_local.normalize_name(n) in normalized]


def _normalize_clarification_options(question: str, raw_options) -> list[str]:
    """Return a short, valid option list from unreliable model output."""
    value = raw_options
    if isinstance(value, str):
        value = value.strip()
        parsed = None
        for loader in (json.loads, ast.literal_eval):
            try:
                parsed = loader(value)
                break
            except Exception:
                continue
        value = parsed
    if not isinstance(value, (list, tuple)):
        value = []

    options = []
    for item in value:
        if not isinstance(item, (str, int, float)):
            continue
        text = re.sub(r"\s+", " ", str(item)).strip()
        if not (2 <= len(text) <= 80) or len(re.findall(r"[A-Za-z0-9]", text)) < 2:
            continue
        if text.casefold() not in {existing.casefold() for existing in options}:
            options.append(text)
        if len(options) == 6:
            break

    # Safe repair for the exact family shown in the bug report. For unknown
    # malformed output the UI shows only its free-form answer field.
    if len(options) < 2:
        words = set(re.findall(r"[a-z0-9]+", (question or "").lower()))
        for family, products in intent_router.PRODUCT_FAMILIES.items():
            if family in words:
                options = list(products.values())[:3]
                break
    return options


def _clarification_key(question: str) -> str:
    words = re.findall(r"[a-z0-9]+", (question or "").lower())
    ignored = {"which", "what", "do", "you", "want", "mean", "the", "a", "an", "with", "to"}
    return " ".join(word for word in words if word not in ignored)


def _same_clarification(left: str, right: str) -> bool:
    a, b = set(_clarification_key(left).split()), set(_clarification_key(right).split())
    return bool(a and b and (a == b or ("proton" in a and "proton" in b) or len(a & b) >= 2))


# ==============================================================================
# Tool-call parsing and repair (the local-model integration layer)
# ==============================================================================
class _ShimFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class _ShimToolCall:
    """Quacks like an OpenAI tool_call so the rest of the loop is unaware that
    this one was reconstructed from plain text."""
    _n = 0

    def __init__(self, name, arguments):
        _ShimToolCall._n += 1
        self.id = f"textcall_{_ShimToolCall._n}"
        self.type = "function"
        self.function = _ShimFunction(name, arguments)


def _balanced_json_spans(text: str):
    """Yield every balanced {...} / [...] substring, ignoring braces that are
    inside string literals. Regex cannot do this correctly for nested args."""
    depth, start, in_str, esc = 0, None, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            if depth == 0:
                start = i
            depth += 1
        elif ch in "}]":
            if depth:
                depth -= 1
                if depth == 0 and start is not None:
                    yield text[start:i + 1]
                    start = None


def _coerce_args(raw) -> dict:
    """Turn whatever the model produced into a dict, or {} - never raise.
    Small local models routinely emit code fences, single quotes, a trailing
    comma, or a dict that is already a dict."""
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    text = str(raw).strip()
    if not text:
        return {}
    for candidate in (text, re.sub(r"^```(?:json)?|```$", "", text, flags=re.I | re.M).strip()):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    for span in _balanced_json_spans(text):
        try:
            parsed = json.loads(span)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            continue
    # Last resort: a Python-style dict with single quotes.
    try:
        import ast
        parsed = ast.literal_eval(text)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    return {}


class _MalformedArgs(dict):
    """Arguments the model sent that could not be read - usually a native tool
    call cut off by the length limit mid-string. It stays an (empty) dict so
    every consumer keeps working, but it is never approved or executed: the
    agent loop answers it with a tool error instead (see _reject_malformed)."""

    def __init__(self, raw: str, truncated: bool):
        super().__init__()
        self.raw_len = len(raw)
        self.truncated = truncated


def _native_args(raw):
    """_coerce_args for a native tool call, but an argument string that was
    sent and cannot be parsed is reported as malformed instead of silently
    becoming {} - {} is a valid-looking "call with no arguments" that used
    to reach the approval card as "(no arguments)"."""
    args = _coerce_args(raw)
    if args or isinstance(raw, dict):
        return args
    text = str(raw or "").strip()
    if not text or text in ("{}", "null"):
        return args
    return _MalformedArgs(text, truncated=model_output.looks_truncated_json(text) or text.count('"') % 2 == 1)


def _normalize_call_obj(obj):
    """Accept every shape a local model uses to express one tool call."""
    out = []
    if isinstance(obj, list):
        for item in obj:
            out.extend(_normalize_call_obj(item))
        return out
    if not isinstance(obj, dict):
        return out
    if "function" in obj and isinstance(obj["function"], dict):
        return _normalize_call_obj(obj["function"])
    if "tool_call" in obj:
        return _normalize_call_obj(obj["tool_call"])
    if "tool_calls" in obj:
        return _normalize_call_obj(obj["tool_calls"])

    name = obj.get("name") or obj.get("tool") or obj.get("tool_name") or obj.get("action_name")
    if not isinstance(name, str) or not schemas.known_tool(name):
        return out
    args = obj.get("arguments")
    if args is None:
        args = obj.get("parameters")
    if args is None:
        args = obj.get("args")
    if args is None:
        args = {k: v for k, v in obj.items()
                if k not in ("name", "tool", "tool_name", "action_name", "arguments", "parameters", "args")}
    out.append((name, _coerce_args(args)))
    return out


_TOOL_TAG_RE = re.compile(
    r"<\s*(?:tool_call|function_call|tool▁call|tool_use)\s*>|</\s*(?:tool_call|function_call|tool▁call|tool_use)\s*>"
    r"|\[TOOL_CALLS\]|<\|python_tag\|>", re.I)


# Qwen3-family XML form (Qwen3-Coder, Qwen3.5/3.8 and models built on them,
# e.g. Ternary Bonsai 2) when the server leaves it in the text:
#   <tool_call><function=web_read><parameter=url>https://...</parameter></function></tool_call>
# A reply cut off before the closing tags still yields what it contains.
_XML_FN_RE = re.compile(r"<function=([\w.\-]+)\s*>(.*?)(?:</function\s*>|(?=<function=)|\Z)", re.S)
_XML_PARAM_RE = re.compile(r"<parameter=([\w.\-]+)\s*>(.*?)(?:</parameter\s*>|(?=<parameter=)|(?=</function)|\Z)", re.S)


def _xml_value(raw: str):
    value = raw[1:] if raw.startswith("\n") else raw
    value = value[:-1] if value.endswith("\n") else value
    text = value.strip()
    if text[:1] in "[{":                       # arrays / objects arrive as JSON text
        try:
            return json.loads(text)
        except ValueError:
            pass
    return value                               # strings; tool_registry coerces "12" / "true"


def _xml_tool_calls(content: str):
    """(calls, leftover) for the XML form, or ([], content) when absent."""
    if "<function=" not in content:
        return [], content
    calls, spans = [], []
    for m in _XML_FN_RE.finditer(content):
        # The XML form is unambiguous, so an invented tool name is still a
        # call: the loop answers it with the closest real tools instead of the
        # markup landing in the chat as the reply.
        name = m.group(1)
        args = {p.group(1): _xml_value(p.group(2)) for p in _XML_PARAM_RE.finditer(m.group(2))}
        calls.append((name, args))
        spans.append(m.span())
    if not calls:
        return [], content
    leftover = content
    for start, end in reversed(spans):
        leftover = leftover[:start] + " " + leftover[end:]
    leftover = _TOOL_TAG_RE.sub(" ", leftover)
    return calls, re.sub(r"\s+", " ", leftover).strip()


def _extract_tool_calls_from_text(content: str):
    """Recover tool calls a local model printed into `content` instead of
    populating tool_calls with. Returns (calls, leftover_prose)."""
    if not content:
        return [], content or ""

    xml_calls, xml_leftover = _xml_tool_calls(content)
    if xml_calls:
        return xml_calls, xml_leftover

    # Common 3B-model form: ask_user(question text, ['A', 'B']). It is not
    # JSON and the question is often unquoted, but it is still unambiguous.
    simple = re.fullmatch(r"\s*ask_user\s*\((.*)\)\s*", content, flags=re.I | re.S)
    if simple:
        body = simple.group(1).strip()
        bracket = body.find("[")
        if bracket >= 0:
            question = body[:bracket].strip(" ,\t\r\n\"'")
            try:
                options = ast.literal_eval(body[bracket:])
            except Exception:
                options = []
            options = _normalize_clarification_options(question, options)
            if question:
                return [("ask_user", {"question": question, "options": options})], ""

    if "{" not in content:
        return [], content or ""
    cleaned = _TOOL_TAG_RE.sub(" ", content)
    calls, consumed = [], []
    for span in _balanced_json_spans(cleaned):
        try:
            obj = json.loads(span)
        except Exception:
            continue
        found = _normalize_call_obj(obj)
        if found:
            calls.extend(found)
            consumed.append(span)
    if not calls:
        return [], content
    leftover = cleaned
    for span in consumed:
        leftover = leftover.replace(span, " ", 1)
    leftover = re.sub(r"```(?:json|tool_code)?", " ", leftover, flags=re.I)
    return calls, re.sub(r"\s+", " ", leftover).strip()


def parse_response(msg):
    """Normalise a model message into (tool_calls, prose). Native tool_calls
    win; text-embedded calls are the local-model fallback."""
    native = getattr(msg, "tool_calls", None) or []
    # Reasoning models' <think> text is never an answer and never an action.
    prose, reasoning = model_output.split_reasoning(getattr(msg, "content", "") or "")
    if reasoning:
        event_log.log_action(f"Set aside {len(reasoning)} chars of model reasoning (not shown, not acted on).")
    if native:
        calls = []
        for tc in native:
            name = tc.function.name
            if not schemas.known_tool(name):
                # Kept, so the loop tells the model (it used to vanish silently).
                event_log.log_action(f"Model requested unknown tool '{name}'.")
            calls.append((tc, name, _native_args(tc.function.arguments)))
        if calls:
            return calls, prose

    recovered, leftover = _extract_tool_calls_from_text(prose)
    if recovered:
        event_log.log_action(f"Recovered {len(recovered)} tool call(s) from plain text (local-model format).")
        return [(_ShimToolCall(n, json.dumps(a)), n, a) for n, a in recovered], leftover
    answer = model_output.strip_self_talk(prose)          # the final reply: no "The user said..., I should..." (1.4.4)
    if answer != prose:
        event_log.log_action(f"Left out {len(prose) - len(answer)} chars where the model talked to itself about the message.")
    return [], answer
