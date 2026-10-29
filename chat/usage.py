"""
usage.py
========
What the assistant used: tokens, web searches and page reads - per chat (the
footer figures saved with each reply, agent/sessions.metrics_of) and per day
(data/usage.json, the last DAYS days) - with an estimated cost for cloud
models from litellm's price list. Local models cost nothing; a cloud model the
list doesn't know has an unknown cost (None), never a guessed one.
"""
from __future__ import annotations

import json
import os
import threading
import time

import atomic_io
import privacy

DAYS = 90


def is_cloud(model: str) -> bool:
    """Cloud model ids carry their provider ("gemini/...", "groq/..."); local GGUF names don't."""
    return "/" in str(model or "")


def price(model: str) -> tuple[float, float] | None:
    """(input, output) dollars per token, or None when litellm doesn't list the model."""
    try:
        import litellm
        info = litellm.model_cost.get(model) or litellm.model_cost.get(str(model).split("/", 1)[-1])
    except Exception:
        return None
    if not info or info.get("input_cost_per_token") is None:
        return None
    return float(info.get("input_cost_per_token") or 0), float(info.get("output_cost_per_token") or 0)


def cost(model: str, tokens_in: int, tokens_out: int) -> float | None:
    if not is_cloud(model):
        return 0.0
    p = price(model)
    return None if p is None else round(tokens_in * p[0] + tokens_out * p[1], 6)


def _blank() -> dict:
    return {"in": 0, "out": 0, "searches": 0, "reads": 0, "replies": 0, "cost": 0.0, "unpriced": 0}


def _add(total: dict, m: dict):
    total["in"] += int(m.get("in") or 0)
    total["out"] += int(m.get("out") or 0)
    total["searches"] += int(m.get("searches") or 0)
    total["reads"] += int(m.get("reads") or 0)
    total["replies"] += 1
    c = cost(m.get("model") or "", int(m.get("in") or 0), int(m.get("out") or 0))
    if c is None:
        total["unpriced"] += 1                  # tokens counted, cost unknown
    else:
        total["cost"] = round(total["cost"] + c, 6)


def chat_totals(transcript: list) -> dict:
    total = _blank()
    for e in transcript or []:
        if e.get("r") == "a" and isinstance(e.get("m"), dict):
            _add(total, e["m"])
    return total


class UsageStore:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def add(self, m: dict, when: float | None = None):
        """One reply's figures into its day. A temporary chat's replies are not counted (privacy.py)."""
        if not isinstance(m, dict) or not (m.get("in") or m.get("out") or m.get("searches") or m.get("reads")):
            return
        if privacy.active():
            return
        day = time.strftime("%Y-%m-%d", time.localtime(when or time.time()))
        with self._lock:
            data = self._load()
            total = {**_blank(), **(data.get(day) or {})}
            _add(total, m)
            data[day] = total
            for old in sorted(data)[:-DAYS]:
                data.pop(old, None)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f)
            atomic_io.replace(tmp, self.path)

    def days(self, n: int = 7) -> list:
        """The last n days, oldest first, each {"day", ...totals} (zeros for quiet days)."""
        data = self._load()
        now = time.time()
        out = []
        for i in range(n - 1, -1, -1):
            day = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
            out.append({"day": day, **{**_blank(), **(data.get(day) or {})}})
        return out
