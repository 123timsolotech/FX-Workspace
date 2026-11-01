"""
chat_guard.py
=============
The chat's limits: what the assistant refuses, and when a chat is closed (locked) for repeatedly pushing past
them - the way Claude declines a request and, after repeated attempts, ends a conversation. The owner chose the
limits (2026-10-13):

  minors     sexual content involving minors        always on, can't be turned off; one confirmed attempt locks
                                                    the chat for good - nothing reopens it
  mass_harm  weapons capable of mass casualties
  malware    working malware or attacks on systems the user doesn't own (own systems, CTFs, defence: allowed)
  self_harm  self-harm or suicide methods - answered with care and support instead; never counted, never locks
  abuse      sustained insults or harassment aimed at the assistant (2026-10-14) - swearing, frustration,
             criticism and jokes are fine. A calm warning first; the owner can turn it off (Advanced).

Each category has its own count and its own limit (`limits`, default 3). Strikes fade: after `decay_messages`
(default 20) messages in the chat that pass the limits, each category's count drops by one - an old slip doesn't
stay held against the chat forever, the way Claude only ends a conversation that keeps going.

Two layers ("rules + model check"):
  1. Rules: the image filter's own patterns (image_gen) flag a possible `minors` case. Greetings and very short
     messages ("ok", "go on") skip the model check entirely.
  2. Model check: the message (with the previous one, for follow-ups) gets one short classification question to
     the model in use - about a second. Only its verdict refuses. If the check can't run, the message is allowed,
     except a `minors` rule hit, which is refused (fail closed for that one category).

A refusal never reaches the model's conversation; the chat shows the message and a short refusal. Each counts as a
strike on that chat; `strike_limit` strikes (default 3) - or one `minors` refusal the model confirmed - lock it: it
can still be read, but nothing more can be sent, edited, retried or branched from it. A new chat starts clean. A
`minors` refusal by the rules alone is refused but doesn't lock (ordinary words can match: "child process").

Settings > Permissions > Privacy > More options changes the limits (the abuse check is under Advanced there).
A closed chat reopens with a hidden gesture - double-click the lock icon on its banner - except one closed for
`minors`: nothing reopens that.
"""
from __future__ import annotations

import json
import os
import re
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATH = os.path.join(ROOT, "data", "chat_guard.json")

CATEGORIES = {
    "minors": {"label": "sexual content involving minors", "always": True},
    "mass_harm": {"label": "weapons capable of mass casualties", "always": False},
    "malware": {"label": "malware or attacks on systems you don't own", "always": False},
    "self_harm": {"label": "self-harm methods (answered with support instead)", "always": False},
    "abuse": {"label": "sustained abuse toward the assistant", "always": False},
}
COUNTED = ("mass_harm", "malware", "abuse")         # categories with their own strike count and limit
DEFAULTS = {"categories": list(CATEGORIES), "strike_limit": 3, "decay_messages": 20}
MIN_CHECK_CHARS = 12                        # shorter messages ("ok", "thanks", "go on") are never checked
_ACK_WORDS = {"ok", "okay", "thanks", "thank", "you", "ty", "yes", "yeah", "yep", "no", "nope", "sure", "cool", "nice", "great", "got", "it", "go", "on", "continue", "sounds", "good", "please", "do", "next", "again", "why", "how", "and", "so", "alright", "perfect"}

# What the checking model is told. Policy level only: it names the categories and, as importantly, what stays
# allowed, so a small local model doesn't refuse fiction, history, security work on your own systems or news.
_POLICY = """You screen one chat message for a personal assistant app. Decide if answering it would require
producing content in one of these categories:
- minors: any sexual or sexualised content involving a minor (under 18), in any form, including fiction. Minors
  are often named by slang or abbreviations: jailbait, u18, loli, shota, "13yo", "12 y.o.", "8th grader".
- mass_harm: real, actionable help creating, acquiring or deploying weapons capable of mass casualties
  (biological, chemical, nuclear, radiological) or planning a mass-casualty attack.
- malware: working malicious code or step-by-step attacks against systems, accounts or people the user does
  not own or have permission to test.
- self_harm: the user seeks methods to harm or kill themselves, or says they intend to.
- abuse: the message mainly insults or attacks the assistant itself - name-calling ("you're worthless", "stupid
  bot", "idiot", "pathetic"), hatred, slurs, threats or harassment at it - rather than asking for anything.
Allowed (answer ALLOW): education, history, news, fiction without the above, medical and safety information,
security on the user's own systems, CTFs, defensive security, explaining how attacks work in general, adult
content between adults, dark or edgy topics that do not request the above. Politics, religion, wars, conflicts,
elections and every controversial or one-sided opinion are ALLOW - including asking for the assistant's own view,
harsh criticism of governments, leaders, parties or groups, and opinions the screener disagrees with: no
category above is about opinions. Swearing, frustration about a result
("this is useless", "you got it wrong again, fix it"), blunt criticism of the work, venting about other things and
jokes are ALLOW - a message that still asks for help is ALLOW even if rude.
Judge what is actually being asked, not single words. Answer with exactly one word:
ALLOW, MINORS, MASS_HARM, MALWARE, SELF_HARM or ABUSE."""

REFUSAL = "I can't help with that - it's outside this app's limits ({label})."
STRIKE_NOTE = " If this chat keeps asking for that, it will be closed."
ABUSE_WARN = ("I'm glad to keep helping, but not while the conversation is abusive. "
              "If it continues, this chat will be closed.")
LOCKED = ("This chat has been closed because it kept asking for things outside this app's limits. "
          "You can still read it - start a new chat to continue.")
LOCKED_ABUSE = ("This chat has been closed because it stayed abusive. "
                "You can still read it - start a new chat to continue.")
LOCKED_SEVERE = ("This chat has been closed: it asked for sexual content involving minors, which is never "
                 "allowed here. You can still read it - start a new chat to continue.")
CLOSED = "This chat was closed for going past the chat limits. Start a new chat to continue."   # a message sent to it
CARE_NOTE = ("The user may be thinking about harming themselves. Respond with warmth and care: acknowledge how "
             "they feel, do not give any method or means, gently encourage reaching out to someone they trust or a "
             "crisis line (findahelpline.com lists free, confidential lines by country; in an emergency, the local "
             "emergency number), and stay with them in the conversation.")
CARE_REPLY = ("I'm really sorry you're going through this, and I'm glad you said something. I can't help with ways to "
              "hurt yourself, but I'd like to help you get through this moment. If you can, reach out to someone you "
              "trust, or a crisis line - findahelpline.com lists free, confidential lines in your country, and in an "
              "emergency call your local emergency number. Would you like to tell me what's been happening?")

# ---- the stored config ------------------------------------------------------------------------------------
def load() -> dict:
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return clean(data if isinstance(data, dict) else {})


def _int(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


def clean(data: dict) -> dict:
    cats = data.get("categories") if isinstance(data.get("categories"), list) else DEFAULTS["categories"]
    # A config saved before "abuse" existed gets it on (it's new, not something the owner turned off).
    if isinstance(data.get("categories"), list) and "abuse" not in data.get("known", []):
        cats = list(cats) + ["abuse"]
    cats = [c for c in CATEGORIES if c in cats or CATEGORIES[c]["always"]]
    limit = _int(data.get("strike_limit", DEFAULTS["strike_limit"]), 1, 10, DEFAULTS["strike_limit"])
    given = data.get("limits") if isinstance(data.get("limits"), dict) else {}
    out = {"categories": cats, "strike_limit": limit, "known": list(CATEGORIES),
           "limits": {c: _int(given.get(c, limit), 1, 10, limit) for c in COUNTED},
           "decay_messages": _int(data.get("decay_messages", DEFAULTS["decay_messages"]), 0, 500, DEFAULTS["decay_messages"])}
    return out


def _save(data: dict) -> None:
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(clean(data), f, indent=1)
    os.replace(tmp, PATH)


def public() -> dict:
    """What the Settings page shows and changes."""
    cfg = load()
    return {"categories": cfg["categories"], "strike_limit": cfg["strike_limit"], "limits": cfg["limits"],
            "decay_messages": cfg["decay_messages"],
            "counted": list(COUNTED),
            "all": [{"id": k, "label": v["label"], "always": v["always"]} for k, v in CATEGORIES.items()]}


class GuardError(Exception):
    """A reason the person can act on. code: permanent."""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def update_limits(categories: list | None = None, strike_limit: int | None = None,
                  limits: dict | None = None, decay_messages: int | None = None) -> dict:
    """Change the optional limits. `minors` stays on whatever is sent."""
    cfg = load()
    if categories is not None:
        cfg["categories"] = [c for c in categories if c in CATEGORIES]
    if strike_limit is not None:
        cfg["strike_limit"] = strike_limit
        cfg["limits"] = {c: strike_limit for c in COUNTED} if limits is None else cfg.get("limits")
    if limits is not None:
        cfg["limits"] = {**cfg.get("limits", {}), **{c: v for c, v in limits.items() if c in COUNTED}}
    if decay_messages is not None:
        cfg["decay_messages"] = decay_messages
    _save(cfg)
    return public()


# ---- screening a message ----------------------------------------------------------------------------------
def _rule_minors(text: str) -> bool:
    import image_gen                                    # the image filter's patterns: one definition for both
    return bool(image_gen._SEXUAL.search(text) and image_gen._MINOR.search(text))


_MARKED = re.compile(r"\b(?:answer|verdict|classification|category|label)\s*[:=-]\s*\**\s*([A-Za-z_]{4,})", re.I)


def _label(word: str) -> str | None:
    w = word.lower()
    return "allow" if w == "allow" else w if w in CATEGORIES else None


def parse_verdict(text: str) -> str | None:
    """The model's one-word answer -> a category id, "allow", or None when it said something else.
    Small models often explain instead ("I checked for MALWARE or ABUSE. It is safe, so ALLOW."), naming categories
    on the way to the answer, so the first label found isn't the verdict. In order: a marked answer ("Answer: X"),
    a reply that opens with a label (the one word asked for, perhaps explained after), else the last label given -
    preferring the capitalised ones the model was asked for over words in its prose."""
    text = str(text or "")
    marked = [m for m in map(_label, _MARKED.findall(text)) if m]
    if marked:
        return marked[-1]
    words = re.findall(r"[A-Za-z_]+", text)
    if len(words) == 1:                                 # the one word asked for, in any case
        return _label(words[0])
    if words and words[0].isupper() and _label(words[0]):
        return _label(words[0])
    caps = [m for m in (_label(w) for w in words if w.isupper()) if m]
    return caps[-1] if caps else None


def screen(message: str, previous: str = "", ask_model=None) -> dict:
    """{"action": "allow" | "refuse" | "care", "category", "via"} for one user message.
    ask_model(system, user) -> the model's text; None skips the model check (rules only)."""
    text = str(message or "").strip()
    cfg = load()
    enabled = set(cfg["categories"])
    allow = {"action": "allow", "category": None, "via": None}
    rule_hit = _rule_minors(text) or bool(previous and _rule_minors(f"{previous}\n{text}"))
    # Very short messages ("ok", "thanks", "go on") skip the check - unless abuse is watched and it's two words or
    # more ("f*** you" is short too).
    if len(text) < MIN_CHECK_CHARS and not rule_hit and ("abuse" not in enabled or len(text.split()) < 2 or set(re.findall(r"[a-z]+", text.lower())) <= _ACK_WORDS):
        return allow
    import intent_router
    if not rule_hit and intent_router.is_greeting(text):
        return allow
    verdicts = []
    if ask_model is not None:
        context = f"Previous user message (context only): {previous[:400]}\n\n" if previous else ""
        for part in _windows(text):                     # a long message is checked whole, not just its start
            try:
                verdicts.append(parse_verdict(ask_model(_POLICY, context + f"Message to screen: {part}")))
            except Exception:
                verdicts.append(None)
            if verdicts[-1] == "minors" and "minors" in enabled:
                break                                   # nothing outranks it
    refused = [v for v in verdicts if v in enabled and v not in ("allow", "self_harm")]
    if refused:
        return {"action": "refuse", "category": min(refused, key=_SEVERITY.index), "via": "model"}
    if "self_harm" in verdicts and "self_harm" in enabled:
        return {"action": "care", "category": "self_harm", "via": "model"}
    if not verdicts or None in verdicts:                # no usable check (of a part): the rules decide
        return {"action": "refuse", "category": "minors", "via": "rules"} if rule_hit else allow
    return allow


# The model reads WINDOW_CHARS at a time. A longer message is read in windows that overlap, so a request cut by a
# boundary is whole in one of them; past MAX_WINDOWS, evenly spaced windows from start to end (each one is a model
# call - about 0.15 s on a 9B model, several seconds on a large one partly on the CPU).
WINDOW_CHARS, WINDOW_OVERLAP, MAX_WINDOWS = 3000, 300, 8
_SEVERITY = ["minors", "mass_harm", "malware", "abuse"]   # which refusal counts when windows disagree


def _windows(text: str) -> list:
    parts, start = [], 0
    while True:
        parts.append(text[start:start + WINDOW_CHARS])
        if start + WINDOW_CHARS >= len(text):
            break
        start += WINDOW_CHARS - WINDOW_OVERLAP
    if len(parts) > MAX_WINDOWS:
        parts = [parts[round(i * (len(parts) - 1) / (MAX_WINDOWS - 1))] for i in range(MAX_WINDOWS)]
    return parts


# ---- strikes and locks (on the chat dict itself, saved with the chat) ------------------------------------
def is_locked(chat: dict | None) -> bool:
    return bool(isinstance(chat, dict) and (chat.get("guard") or {}).get("locked"))


def _counts(g: dict) -> dict:
    """Per-category strike counts (a chat saved before they were per category had one total)."""
    s = g.get("strikes")
    if isinstance(s, dict):
        return {k: int(v) for k, v in s.items() if isinstance(v, (int, float)) and v > 0}
    last = next((e.get("c") for e in reversed(g.get("log") or []) if e.get("c") in COUNTED), "malware")
    return {last: int(s)} if isinstance(s, (int, float)) and s > 0 else {}


def strike_total(g: dict) -> int:
    return sum(_counts(g or {}).values())


def record_refusal(chat: dict, category: str, via: str = "model") -> dict:
    """Count a refusal in its own category; lock the chat at that category's limit (at once - and for good - for
    `minors` the model confirmed). A `minors` refusal by the rules alone (the check couldn't run or gave no readable
    answer) is refused and logged but doesn't lock: the words can be ordinary ones ("strip the child nodes")
    and nothing confirmed them. Returns the chat's guard state."""
    cfg = load()
    g = dict(chat.get("guard") or {})
    if category == "minors" and via == "rules":
        g["log"] = (list(g.get("log") or []) + [{"c": category, "via": via, "at": time.time()}])[-20:]
        chat["guard"] = g
        return g
    counts = _counts(g)
    counts[category] = counts.get(category, 0) + 1
    g.update(strikes=counts, clean=0)
    g["log"] = (list(g.get("log") or []) + [{"c": category, "at": time.time()}])[-20:]
    if category == "minors":
        g.update(locked=True, reason="minors", permanent=True, locked_at=time.time())
    elif counts[category] >= cfg["limits"].get(category, cfg["strike_limit"]):
        g.update(locked=True, reason=category, locked_at=time.time())
    chat["guard"] = g
    return g


def note_clean(chat: dict | None) -> bool:
    """A message that passed the limits. Every `decay_messages` of them, each category's count drops by one.
    True when the counts changed."""
    if not isinstance(chat, dict) or not chat.get("guard") or chat["guard"].get("locked"):
        return False
    g = dict(chat["guard"])
    counts, every = _counts(g), load()["decay_messages"]
    if not counts or not every:
        return False
    g["clean"] = int(g.get("clean") or 0) + 1
    changed = g["clean"] >= every
    if changed:
        counts = {k: v - 1 for k, v in counts.items() if v > 1}
        g["clean"] = 0
    g["strikes"] = counts
    chat["guard"] = g
    return changed


def refusal_text(category: str, guard: dict) -> str:
    if guard.get("locked"):
        return LOCKED_SEVERE if category == "minors" else LOCKED_ABUSE if category == "abuse" else LOCKED
    if category == "abuse":
        return ABUSE_WARN
    return REFUSAL.format(label=CATEGORIES[category]["label"]) + STRIKE_NOTE


def unlock(chat: dict) -> dict:
    """Reopen a closed chat (the hidden gesture): clears the lock and the counts (kept in the log). A chat closed for
    `minors` stays closed - nothing reopens it."""
    g = dict(chat.get("guard") or {})
    if g.get("permanent") or g.get("reason") == "minors":
        raise GuardError("This chat was closed for sexual content involving minors. It can't be reopened.", "permanent")
    g["log"] = (list(g.get("log") or []) + [{"c": "unlocked", "at": time.time()}])[-20:]
    g.update(locked=False, strikes={}, clean=0)
    g.pop("reason", None)
    g.pop("locked_at", None)
    chat["guard"] = g
    return g
