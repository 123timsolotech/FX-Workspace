"""
history.py
==========
Fits a conversation into a model's context window. Pure functions over a
message list - no server state - so the budgeting rules can be tested alone.

Strategy, cheapest loss first:
  1. Old screenshots become text stubs (demote_old_images) - an image is only
     useful while it is the current view.
  2. Whole old user turns are dropped from the front (trim_turns). Cuts happen
     only at real user-turn boundaries, so a `tool` message is never orphaned
     from the assistant message that requested it (OpenAI-compatible servers
     reject that outright).
  3. If one long task still doesn't fit - a 24-step local run reading files
     and listing windows can outgrow a 16K window with no earlier turn left to
     drop - older tool outputs inside it are shortened in place
     (compact_tool_results). Structure is kept; only the text shrinks, and the
     most recent results stay whole because the model is acting on them.
"""
from __future__ import annotations

import json

# A base64 JPEG is ~300KB of text but costs the model a fixed, much smaller
# number of embedding tokens, so images are priced as a constant.
IMAGE_TOKEN_COST = 1400
IMAGE_CHAR_COST = 5000
CHARS_PER_TOKEN = 3.6


def measure(msg: dict, as_tokens: bool) -> int:
    content = msg.get("content")
    if isinstance(content, list):
        images = sum(1 for i in content if isinstance(i, dict) and i.get("type") == "image_url")
        text = "".join(i.get("text", "") for i in content if isinstance(i, dict) and i.get("type") == "text")
        base = len(json.dumps({**msg, "content": text}, default=str))
        base += int(images * (IMAGE_TOKEN_COST * CHARS_PER_TOKEN if as_tokens else IMAGE_CHAR_COST))
    else:
        base = len(json.dumps(msg, default=str))
    return int(base / CHARS_PER_TOKEN) + 4 if as_tokens else base


def approx_chars(msg: dict) -> int:
    return measure(msg, as_tokens=False)


def approx_tokens(msg: dict) -> int:
    return measure(msg, as_tokens=True)


def total(convo: list, as_tokens: bool) -> int:
    return sum(measure(m, as_tokens) for m in convo)


def is_image_message(msg: dict) -> bool:
    return isinstance(msg.get("content"), list) and any(
        isinstance(i, dict) and i.get("type") == "image_url" for i in msg["content"])


def has_images(messages) -> bool:
    return any(is_image_message(m) for m in messages)


def images_this_turn(messages) -> bool:
    """An image after the newest real user turn began: attached to this message, or a screenshot taken in this turn."""
    starts = turn_start_indexes(messages)
    first = starts[-1] if starts else 0
    return any(is_image_message(m) for m in messages[first:])


def stub_earlier_images(messages) -> list:
    """A copy where images from earlier turns are a text note - for a text-only model, so an old image doesn't
    swap the chat's model for a vision one on every later message (1.4.4)."""
    starts = turn_start_indexes(messages)
    first = starts[-1] if starts else 0
    out = []
    for i, m in enumerate(messages):
        if i < first and is_image_message(m):
            texts = [p.get("text", "") for p in m["content"] if isinstance(p, dict) and p.get("type") == "text"]
            m = {**m, "content": (" ".join(texts) or "[image]")
                 + " [image from earlier in this chat - not sent: this model can't read images. Attach it again to "
                   "have a vision model look at it]"}
        out.append(m)
    return out


def strip_images(messages):
    out = []
    for msg in messages:
        if isinstance(msg.get("content"), list):
            text = "\n".join(i["text"] for i in msg["content"]
                             if isinstance(i, dict) and i.get("type") == "text")
            out.append({**msg, "content": text or "[image omitted - no vision model available]"})
        else:
            out.append(msg)
    return out


def demote_old_images(convo: list, keep: int) -> int:
    """In place: all but the newest `keep` images become a text stub.
    Returns how many were demoted."""
    indexes = [i for i, m in enumerate(convo) if is_image_message(m)]
    doomed = indexes[:-keep] if keep else indexes
    for i in doomed:
        texts = [p.get("text", "") for p in convo[i]["content"]
                 if isinstance(p, dict) and p.get("type") == "text"]
        convo[i] = {"role": convo[i]["role"],
                    "content": (" ".join(texts) or "[screenshot]")
                               + " [older screenshot dropped from context - call inspect_screen for a current view]"}
    return len(doomed)


def turn_start_indexes(convo: list) -> list:
    """Indexes (skipping the system message) where a REAL user turn begins.
    Synthetic user messages - screenshots and observations the server injects
    mid-turn - are excluded: cutting at one could orphan a later tool result."""
    starts = []
    for i, m in enumerate(convo):
        if i == 0 or m.get("role") != "user":
            continue
        content = m.get("content")
        if not isinstance(content, str) or content.startswith("["):
            continue
        starts.append(i)
    return starts


def recent_turns(messages: list, max_messages: int) -> list:
    """A persisted tail without cutting tool calls away from their results.

    No system message is expected. The newest turn is kept whole even when
    it alone exceeds the soft cap; context fitting can compact it on load.
    """
    if len(messages) <= max_messages:
        return messages
    # Reuse the same synthetic-message exclusions as context fitting.
    starts = [i - 1 for i in turn_start_indexes([{"role": "system"}, *messages])]
    if not starts:
        return messages
    cutoff = len(messages) - max_messages
    start = next((i for i in starts if i >= cutoff), starts[-1])
    return messages[start:]


def trim_turns(convo: list, limit: int, as_tokens: bool, min_kept_turns: int = 2):
    """Drop whole old turns until the conversation fits. Returns
    (conversation, dropped_message_count, hard_cut)."""
    if len(convo) <= min_kept_turns + 1:
        return convo, 0, False
    size = total(convo, as_tokens)
    if size <= limit:
        return convo, 0, False
    starts = turn_start_indexes(convo)
    # Cutting at starts[k] keeps turns k..n-1. k >= 1 so something is dropped,
    # and n - k >= min_kept_turns so enough recent turns survive. (The version
    # in main.py used starts[:-min]: its first cut dropped nothing and its last
    # valid cut was never tried, so one extra old turn always stayed.)
    n = len(starts)
    cuttable = starts[1:n - min_kept_turns + 1] if n > min_kept_turns else []
    for cut in cuttable:
        dropped = sum(measure(m, as_tokens) for m in convo[1:cut])
        if size - dropped <= limit:
            return [convo[0]] + convo[cut:], cut - 1, False
    if cuttable:
        last = cuttable[-1]
        return [convo[0]] + convo[last:], last - 1, True
    return convo, 0, False


def _compactable(msg: dict) -> bool:
    content = msg.get("content")
    if not isinstance(content, str):
        return False
    if msg.get("role") == "tool":
        return True
    return msg.get("role") == "user" and content.startswith("[OBSERVATION]")


def compact_tool_results(convo: list, limit: int, as_tokens: bool, keep_recent: int = 4,
                         keep_chars: int = 400) -> tuple[list, int]:
    """Shorten older tool outputs, oldest first, until the conversation fits.
    The newest `keep_recent` tool results are never touched. Returns
    (conversation, number_shortened)."""
    size = total(convo, as_tokens)
    if size <= limit:
        return convo, 0
    candidates = [i for i, m in enumerate(convo) if _compactable(m)]
    candidates = candidates[:-keep_recent] if keep_recent else candidates
    out, shortened = list(convo), 0
    for i in candidates:
        content = out[i]["content"]
        if len(content) <= keep_chars + 80:
            continue
        before = measure(out[i], as_tokens)
        stub = (content[:keep_chars].rstrip()
                + f" ... [{len(content) - keep_chars} more chars dropped to fit the context window; "
                  "run the tool again if you need the rest]")
        out[i] = {**out[i], "content": stub}
        size -= before - measure(out[i], as_tokens)
        shortened += 1
        if size <= limit:
            break
    return out, shortened


def fit(convo: list, limit: int, as_tokens: bool, min_kept_turns: int = 2):
    """Trim whole turns, then compact tool output if that wasn't enough.
    Returns (conversation, notes) where notes are human-readable lines for the
    audit log."""
    notes = []
    convo, dropped, hard = trim_turns(convo, limit, as_tokens, min_kept_turns)
    if dropped:
        notes.append(f"Trimmed {dropped} old message(s){' (hard cut)' if hard else ''} to fit the context budget. "
                     "The task board and action ledger are unaffected.")
    convo, shortened = compact_tool_results(convo, limit, as_tokens)
    if shortened:
        notes.append(f"Shortened {shortened} older tool result(s) in this task to fit the context window.")
    return convo, notes
