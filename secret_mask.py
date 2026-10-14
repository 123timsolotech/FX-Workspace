"""
secret_mask.py
==============
The one rule for hiding secret-looking values in text the app shows, saves or
sends on (model notes and reasoning, project file snippets in the brief, saved
past runs): well-known key formats and `key = value` assignments of keys,
tokens, passwords and secrets. The value is replaced with "[hidden]".
"""
from __future__ import annotations

import re

SECRET_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|ghp_[A-Za-z0-9]{20,}|hf_[A-Za-z0-9]{20,}|AIza[0-9A-Za-z_-]{20,}|xox[abpr]-[A-Za-z0-9-]{10,})"
    r"|((?:api[_-]?key|token|password|passwd|secret)\s*[:=]\s*)\S+", re.I)


def mask(text) -> str:
    return SECRET_RE.sub(lambda m: (m.group(1) or "") + "[hidden]", str(text or ""))
