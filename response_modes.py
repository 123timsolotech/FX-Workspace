"""
response_modes.py
=================
How the assistant answers (D47) - one setting, response_mode, chosen in Setup beside the message box or in
Settings > Agent:

  enhanced (default)  the agent: the Enhanced core (agent/prompts.ENHANCED_CORE - who it is, how it answers, the
                      ground rules and how it speaks up when one stops it), the tools, the live machine state, the
                      brief, memory, persona and the checks after the answer (careful.py).
  raw                 the model alone: the conversation and nothing else - no system message, no tools, no live
                      state, brief, memory, persona or checks, and the model's own sampling settings. For seeing what
                      a model does by itself, or plain chat. Nothing on the PC can change in Raw.

A message may carry its own response_mode (ChatRequest.response_mode); otherwise the setting decides.
"""
from __future__ import annotations

ENHANCED, RAW = "enhanced", "raw"
MODES = (ENHANCED, RAW)
LABELS = {ENHANCED: "Enhanced", RAW: "Raw"}
DEFAULT_SETTINGS = {"response_mode": ENHANCED}


def clean_settings(s: dict) -> dict:
    mode = s.get("response_mode")
    return {"response_mode": mode if mode in MODES else ENHANCED}


def for_request(settings: dict | None, req=None) -> str:
    """This message's mode: its own when it names a valid one, else the setting."""
    asked = getattr(req, "response_mode", None) if req is not None else None
    if asked in MODES:
        return asked
    return clean_settings(settings or {})["response_mode"]


def is_raw(state: dict) -> bool:
    """Whether the turn now running is a Raw one (agent/tasks sets raw_mode for its length)."""
    return bool(state.get("raw_mode"))
