"""
compare_arena.py
================
The Compare place: one prompt, two models, answers side by side - blind by default, so you judge the answers and
not the names. After you vote (left, right, a tie or both bad) the names show, and the vote counts towards a
leaderboard (Elo, like a chess rating, plus wins / losses / ties) - the idea of Odysseus' Compare.

A model is "local:<name>" (llama-server; one is loaded at a time, so local answers run one after the other) or a
cloud model id. Answers come from agent/models.model_text: no tools, no memory, the same plain system prompt for
both - a fair test of the models themselves. "Combine" asks a model to merge the two answers into one.

Stored in data/compare.json: {"version": 1, "rounds": [...newest first, at most 300]}
  round: {id, prompt, system, created, state: running|done|failed, error, blind, sides: {left, right},
          winner: left|right|tie|bad|None, voted_at, combined}
  side:  {model, text, seconds, tokens_out, error}
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import uuid

import atomic_io

DATA = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "compare.json")
KEEP = 300
ELO_START, ELO_K = 1000.0, 32.0
SYSTEM = ("You are a helpful assistant. Answer the user's request directly and accurately. Replies are rendered as "
          "Markdown.")
COMBINE_SYSTEM = ("You merge two answers to the same request into one better answer: keep what is correct and useful "
                  "from each, drop mistakes and repetition, and write it as one clean answer in Markdown. Don't mention "
                  "that there were two answers.")
_lock = threading.Lock()


def _load() -> dict:
    try:
        with open(DATA, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("rounds"), list):
            return d
    except (OSError, ValueError):
        pass
    return {"version": 1, "rounds": []}


def _save(d: dict) -> None:
    os.makedirs(os.path.dirname(DATA), exist_ok=True)
    d["rounds"] = d["rounds"][:KEEP]
    tmp = DATA + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    atomic_io.replace(tmp, DATA)


def _update(rid: str, fn) -> dict | None:
    with _lock:
        d = _load()
        r = next((x for x in d["rounds"] if x["id"] == rid), None)
        if r is None:
            return None
        fn(r)
        _save(d)
        return r


def public(r: dict) -> dict:
    """What the page sees: in a blind round the model names stay hidden until the vote."""
    out = json.loads(json.dumps(r))
    if out.get("blind") and not out.get("winner"):
        for s in out["sides"].values():
            s["model"] = None
    return out


def rounds(limit: int = 50) -> list[dict]:
    with _lock:
        return [public(r) for r in _load()["rounds"][:limit]]


def get(rid: str) -> dict | None:
    with _lock:
        r = next((x for x in _load()["rounds"] if x["id"] == rid), None)
    return public(r) if r else None


def start(prompt: str, model_a: str, model_b: str, blind: bool = True, system: str = "", answer=None,
          lock=None) -> dict:
    """answer(system, prompt, model) -> {"text", "seconds", "tokens_out"}: agent/models.model_text in the app.
    lock: held while the two answers are written (the app's turn lock - no chat runs meanwhile)."""
    prompt = (prompt or "").strip()
    if not prompt:
        raise ValueError("Write a prompt to compare the models on.")
    if not model_a or not model_b:
        raise ValueError("Pick two models.")
    if model_a == model_b:
        raise ValueError("Pick two different models.")
    a, b = (model_a, model_b) if random.random() < 0.5 else (model_b, model_a)
    r = {"id": uuid.uuid4().hex[:12], "prompt": prompt[:8000], "system": (system or "").strip()[:4000],
         "created": time.time(), "state": "running", "error": "", "blind": bool(blind),
         "sides": {"left": {"model": a, "text": "", "state": "waiting"}, "right": {"model": b, "text": "", "state": "waiting"}},
         "winner": None, "voted_at": None, "combined": None}
    with _lock:
        d = _load()
        d["rounds"].insert(0, r)
        _save(d)
    threading.Thread(target=_run, args=(r["id"], answer, lock), name="compare", daemon=True).start()
    return public(r)


def _run(rid: str, answer, lock) -> None:
    try:
        r = get_raw(rid)
        for side in ("left", "right"):
            model = r["sides"][side]["model"]
            _update(rid, lambda x, s=side: x["sides"][s].update(state="running"))
            try:
                out = answer(r["system"] or SYSTEM, r["prompt"], model)
                _update(rid, lambda x, s=side, o=out: x["sides"][s].update(
                    state="done", text=o.get("text", ""), seconds=o.get("seconds"), tokens_out=o.get("tokens_out")))
            except Exception as e:
                _update(rid, lambda x, s=side, e=e: x["sides"][s].update(state="failed", error=str(e)[:300]))
        _update(rid, lambda x: x.update(state="done" if any(s["state"] == "done" for s in x["sides"].values()) else "failed"))
    except Exception as e:
        _update(rid, lambda x: x.update(state="failed", error=str(e)[:300]))
    finally:
        if lock is not None:
            try:
                lock.release()
            except RuntimeError:
                pass


def recover() -> int:
    """At start: rounds the last run left "running" (the app closed mid-answer) end as failed."""
    with _lock:
        d = _load()
        n = 0
        for r in d["rounds"]:
            if r.get("state") == "running":
                n += 1
                r["state"] = "failed" if not any(s.get("state") == "done" for s in r["sides"].values()) else "done"
                for s in r["sides"].values():
                    if s.get("state") in ("running", "waiting"):
                        s.update(state="failed", error="The app closed before this answer finished.")
        if n:
            _save(d)
        return n


def get_raw(rid: str) -> dict:
    with _lock:
        return next(x for x in _load()["rounds"] if x["id"] == rid)


def vote(rid: str, winner: str) -> dict:
    if winner not in ("left", "right", "tie", "bad"):
        raise ValueError("Vote left, right, tie or bad.")
    err = {}

    def apply(r):
        if r.get("winner"):
            err["m"] = "This round already has a vote."
            return
        if r["state"] == "running":
            err["m"] = "Wait for both answers first."
            return
        r["winner"], r["voted_at"] = winner, time.time()
    r = _update(rid, apply)
    if r is None:
        raise KeyError("No such round.")
    if err:
        raise ValueError(err["m"])
    return public(r)


def combine(rid: str, answer, model: str | None = None) -> dict:
    r = get_raw(rid)
    texts = [r["sides"][s].get("text") for s in ("left", "right")]
    if not all(texts):
        raise ValueError("Both answers are needed to combine them.")
    user = f"Request:\n{r['prompt']}\n\nAnswer 1:\n{texts[0]}\n\nAnswer 2:\n{texts[1]}"
    out = answer(COMBINE_SYSTEM, user, model)
    r = _update(rid, lambda x: x.update(combined={"text": out.get("text", ""), "model": out.get("model") or model}))
    return public(r)


def delete(rid: str) -> bool:
    with _lock:
        d = _load()
        n = len(d["rounds"])
        d["rounds"] = [x for x in d["rounds"] if x["id"] != rid]
        _save(d)
        return len(d["rounds"]) != n


def leaderboard() -> list[dict]:
    """Elo from every voted round, oldest first. A tie is half a win each; "both bad" counts as a tie for Elo
    (neither is better) and is counted separately."""
    with _lock:
        rs = [r for r in reversed(_load()["rounds"]) if r.get("winner")]
    rating: dict[str, float] = {}
    rec: dict[str, dict] = {}
    for r in rs:
        a, b = r["sides"]["left"]["model"], r["sides"]["right"]["model"]
        if not a or not b:
            continue
        for m in (a, b):
            rating.setdefault(m, ELO_START)
            rec.setdefault(m, {"wins": 0, "losses": 0, "ties": 0, "bad": 0})
        w = r["winner"]
        sa = 1.0 if w == "left" else 0.0 if w == "right" else 0.5
        ea = 1 / (1 + 10 ** ((rating[b] - rating[a]) / 400))
        rating[a] += ELO_K * (sa - ea)
        rating[b] += ELO_K * ((1 - sa) - (1 - ea))
        if w == "left":
            rec[a]["wins"] += 1; rec[b]["losses"] += 1
        elif w == "right":
            rec[b]["wins"] += 1; rec[a]["losses"] += 1
        elif w == "tie":
            rec[a]["ties"] += 1; rec[b]["ties"] += 1
        else:
            rec[a]["bad"] += 1; rec[b]["bad"] += 1
    rows = [{"model": m, "elo": round(rating[m]), **rec[m], "games": sum(rec[m].values())} for m in rating]
    return sorted(rows, key=lambda x: (-x["elo"], -x["wins"]))
