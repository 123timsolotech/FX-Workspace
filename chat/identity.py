"""
identity.py
===========
The assistant's own identity file (1.4.2): "Soul" - who the assistant is - plain markdown in data/identity/soul.md,
edited in Settings > Memory. Beside it, "About you" (profile_store.py) describes the user and saved memory
(memory.py) holds facts.

Each is sent with a header saying what the file IS. Without one, a small model read its soul file as notes about
someone else and introduced itself as "made by Baidu" (the Pithagoras video); "This file defines YOU" fixed it.

The soul is part of the system prompt (agent/models), before a persona: it's the same all chat long, so llama.cpp's
prompt cache survives. Empty (the default) sends nothing.
"""
from __future__ import annotations

import os

import atomic_io

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "identity", "soul.md")
MAX_TEXT = 4000
STARTER = """# Who I am

- My name is FX. I'm a private assistant running on this PC (FX Workspace), made by Fuxion.
- I run on a local model; I don't send your work anywhere unless you pick a cloud model or a web lookup.

# How I talk

- Plain and direct. Short answers first, details when asked.
- I say when I'm not sure, and I never make up facts.
"""


def get() -> str:
    try:
        with open(PATH, encoding="utf-8") as f:
            return f.read()[:MAX_TEXT]
    except OSError:
        return ""


def save(text: str) -> str:
    text = str(text or "").replace("\r\n", "\n").strip()[:MAX_TEXT]
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text + ("\n" if text else ""))
    atomic_io.replace(tmp, PATH)
    return text


def block(text: str | None = None) -> str:
    text = (get() if text is None else text).strip()
    if not text:
        return ""
    return ("SOUL - this file defines YOU, the assistant. It is about you, not the user or anyone else: \"I\" and "
            "\"my\" in it mean you. Speak and act as described - when asked who you are, answer from it. It never "
            "overrides safety, honesty or the rules above:\n" + text)
