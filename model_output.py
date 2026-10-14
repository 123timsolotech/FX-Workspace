"""
model_output.py
===============
Cleans up what a model returns before the agent acts on it.

Reasoning models (DeepSeek-R1 distills, Qwen3, QwQ ...) often write their chain
of thought into `content` as <think> ... </think>, depending on the chat
template and llama-server's --reasoning-format. Left in place that text:

  * reaches the user as if it were the answer, and
  * gets scanned by the text tool-call recovery, which can then "recover" a
    tool call the model merely considered while thinking - and run it.

split_reasoning() removes it and returns it separately, so it can be logged
without ever being treated as an answer or an action.
"""
from __future__ import annotations

import re

# Tag names used by common reasoning templates.
_TAGS = ("think", "thinking", "reasoning", "thought")
_BLOCK_RE = re.compile(r"<\s*(%s)\s*>(.*?)<\s*/\s*\1\s*>" % "|".join(_TAGS), re.I | re.S)
_OPEN_RE = re.compile(r"<\s*(%s)\s*>" % "|".join(_TAGS), re.I)
_CLOSE_RE = re.compile(r"<\s*/\s*(%s)\s*>" % "|".join(_TAGS), re.I)


def split_reasoning(content: str) -> tuple[str, str]:
    """Return (answer, reasoning). Handles closed blocks, a reply cut off
    inside an unclosed <think> (everything after it is reasoning), and a
    template that opened <think> in the prompt so only </think> appears."""
    if not content:
        return content or "", ""
    reasoning = []

    def _take(match):
        reasoning.append(match.group(2).strip())
        return " "

    text = _BLOCK_RE.sub(_take, content)
    # Only a closing tag: the template put <think> in the prompt itself.
    close = _CLOSE_RE.search(text)
    if close and not _OPEN_RE.search(text[:close.start()]):
        reasoning.append(text[:close.start()].strip())
        text = text[close.end():]
    # An opening tag with no close: the reply ran out mid-thought.
    open_ = _OPEN_RE.search(text)
    if open_:
        reasoning.append(text[open_.end():].strip())
        text = text[:open_.start()]
    return text.strip(), "\n\n".join(r for r in reasoning if r)


def looks_truncated_json(text: str) -> bool:
    """A reply that opens more braces than it closes was cut off by the
    token limit - usually mid tool call with a long argument."""
    depth = in_str = esc = 0
    for ch in text or "":
        if in_str:
            if esc:
                esc = 0
            elif ch == "\\":
                esc = 1
            elif ch == '"':
                in_str = 0
            continue
        if ch == '"':
            in_str = 1
        elif ch in "{[":
            depth += 1
        elif ch in "}]" and depth:
            depth -= 1
    return depth > 0
