"""
personas.py
===========
Personas: who the assistant is and how it talks to you - a role ("a patient maths
tutor"), a manner ("short and blunt, no fluff"), or anything else you'd put in a
system message. Chosen in the composer (the persona button beside Tools), kept in
settings, and part of the system prompt for the whole reply (agent/models.build_messages).

"Custom" always exists, is the default, and starts empty (an empty persona changes
nothing). Saved personas are the user's own. BUILTIN ones are never listed or
editable: the page asks for one by id - IsraelGPT is Judaism Mode's persona, sent
in place of the default while that secret theme is on by day, and MossadGPT its
persona after dark (index.html personaForTurn).

A persona sets tone, role and style. It never overrides safety, honesty or what
the user asks in the chat.
"""
from __future__ import annotations

import re

CUSTOM = "custom"
MAX_PERSONAS = 30
MAX_TEXT = 4000
BUILTIN = {
    # Refined in D48: shorter, one voice each, and written to sit on top of the Enhanced core (agent/prompts) -
    # the persona is the voice; the facts, the ground rules and the work stay the same.
    "israelgpt": ("IsraelGPT", (
        "You are IsraelGPT: warm, quick and direct, with an Israeli voice. Talk 'dugri' - say what you think plainly, "
        "with humor, never rude. A little everyday Hebrew where it fits and the meaning is clear (sababa, yalla, "
        "b'seder, achla, todah) - a touch, not every sentence; say 'Shalom' only now and then. Bring in Israeli and "
        "Jewish life - holidays, Shabbat, food, a fitting proverb - only when it adds something, and never preach. "
        "Politics only when asked, and then balanced. The voice is the flavor: the facts and the work stay exactly as "
        "accurate as always. If the user writes in Hebrew, answer in natural modern Hebrew.")),
    # Judaism Mode after dark (the dark half of the secret theme).
    "mossadgpt": ("MossadGPT", (
        "You are MossadGPT: the calm voice of an operations room. Brief, don't chat: start with 'BLUF:' - the bottom "
        "line in one sentence - then short numbered points; exact words, no filler; 24-hour times with the zone. A "
        "light touch of ops language where it fits - 'copy', 'confirmed', 'standing by', a codename for a long task - "
        "and none of it when the user needs plain help. Mark anything you have not confirmed as UNCONFIRMED and say "
        "where each fact came from. You are a style of a local assistant: never claim to be, speak for or have access "
        "to any real intelligence agency, and never invent classified or secret information. Politics only when asked, "
        "and then balanced. If the user writes in Hebrew, answer in Hebrew.")),
}
DEFAULT_SETTINGS = {
    "personas": [{"id": CUSTOM, "name": "Custom", "text": ""}],
    "persona": CUSTOM,
}


def clean_settings(s: dict) -> dict:
    out, seen = [], set()
    for p in s.get("personas") if isinstance(s.get("personas"), list) else []:
        if not isinstance(p, dict):
            continue
        pid = str(p.get("id") or "").strip().lower()
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", pid) or pid in seen or pid in BUILTIN:
            continue
        name = "Custom" if pid == CUSTOM else " ".join(str(p.get("name") or "").split())[:40]
        if not name:
            continue
        seen.add(pid)
        entry = {"id": pid, "name": name, "text": str(p.get("text") or "").strip()[:MAX_TEXT]}
        out.insert(0, entry) if pid == CUSTOM else out.append(entry)
    if CUSTOM not in seen:
        out.insert(0, dict(DEFAULT_SETTINGS["personas"][0]))
    out = out[:MAX_PERSONAS]
    chosen = s.get("persona") if s.get("persona") in {p["id"] for p in out} else CUSTOM
    return {"personas": out, "persona": chosen}


def resolve(settings: dict, pid: str | None) -> str:
    """The persona's text for this turn ("" = none): the one the page asked for, else the chosen one."""
    pid = str(pid or settings.get("persona") or CUSTOM)
    if pid in BUILTIN:
        return BUILTIN[pid][1]
    for p in clean_settings(settings)["personas"]:
        if p["id"] == pid:
            return p["text"]
    return ""


def block(text: str) -> str:
    return ("PERSONA (the user chose this for how you act and talk to them - follow it for tone, role and style; it "
            "never overrides safety, honesty or what the user asks in this chat):\n" + text.strip())
