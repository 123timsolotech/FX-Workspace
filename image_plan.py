"""
image_plan.py
=============
"Think first" for pictures (Pony V6 and its presets): before drawing, the chat model works out what the picture
needs - who and what is in it, how many, pose, hands, expression, outfit, camera and framing, background, light,
composition - and writes it as Pony tags, plus the few things that must be true in the result. After drawing,
optionally, a vision model looks at the picture against those must-be-true points; a clear mismatch gets one
corrected attempt.

Sequences: one fixed identity (character, outfit, colours, setting) plus a change per picture - camera angles of
the same shot ("angles") or steps of one motion ("frames").

Everything here is text in, JSON out; image_gen draws. Local models only see what the user wrote and the
picture they made.
"""
from __future__ import annotations

import json
import re

import agent.models as llm
import llama_local

PONY_KEYS = ("pony6", "pony6_anime", "pony6_warm")

_PLAN_SYSTEM = """You plan pictures for Pony Diffusion V6 XL, an anime/cartoon image model that reads danbooru-style
tags. Think about what the picture needs so it makes sense: who and what is in it and how many (1girl, 2boys,
solo...), identity (hair colour and style, eye colour, body type), outfit, pose and what the hands are doing,
expression, camera angle and framing (close-up, upper body, cowboy shot, full body, from side, from above...),
background and setting, lighting, composition, and any objects the request names. Resolve contradictions and keep
everything the user asked for; add only what is needed for a coherent picture. Never add the score_ / source_ /
rating_ tags - the app adds those.
Counting uses danbooru tags: 1girl / 1boy for one person plus "solo"; 2girls, 3girls, 2boys, 1boy 1girl ...
plus "multiple girls" / "multiple boys" for more than one - NEVER "solo" then. When there are several people, say
what each one does (e.g. "girl on left reading book, girl on right holding ice cream cone").

Answer with JSON only:
{"thinking": "2-4 short sentences on how the picture should be put together",
 "tags": "comma-separated tags, most important first (count, character, outfit, pose, framing, setting, light)",
 "negative": "extra things to avoid for THIS picture (e.g. extra arms, two heads, text), or empty",
 "checks": ["3-5 short facts the finished picture must show, e.g. 'exactly one girl', 'holding a red umbrella'"]}"""

_SEQ_SYSTEM = """You plan a SEQUENCE of pictures for Pony Diffusion V6 XL (danbooru-style tags) that must show the
SAME character/scene every time. First fix the identity: count, character (hair colour/style, eyes, body), outfit
with colours, accessories, setting and light - these tags must be identical in every picture. Then write what
changes per picture. Mode "angles": the same moment from different camera angles (from front, three-quarter view,
from side, from behind, from above...). Mode "frames": consecutive steps of one action, small changes between
neighbours (like key frames of an animation). Never add score_ / source_ / rating_ tags.

Answer with JSON only:
{"thinking": "1-3 sentences",
 "identity": "comma-separated tags shared by every picture",
 "negative": "extra things to avoid, or empty",
 "pictures": [{"label": "short name", "tags": "only the tags that change for this picture"}, ...]}"""

_CHECK_SYSTEM = """You check a generated picture against a request. Look carefully. FIRST count the people in the
picture and compare with the request. Then, for each required fact, say whether the picture clearly shows it. Also
note obvious defects (extra or missing limbs/fingers, merged bodies, garbled faces). "add_negative" is only for
unwanted things that appear in the picture - never for anything the request asks for. Answer with JSON only:
{"ok": true or false, "problems": ["short description", ...], "add_tags": "tags that would fix it, or empty",
 "add_negative": "tags to avoid that would fix it, or empty"}
Say ok=false only for clear, important mismatches, not for style or taste."""


class PlanError(Exception):
    pass


def _json(text: str) -> dict:
    """The first {...} object in a model's answer (models sometimes wrap it in prose or ```)."""
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise PlanError("The model didn't answer with a plan.")
    try:
        return json.loads(m.group(0))
    except ValueError:
        # A trailing comma or a stray quote: try the common fixes once.
        fixed = re.sub(r",\s*([}\]])", r"\1", m.group(0))
        try:
            return json.loads(fixed)
        except ValueError as e:
            raise PlanError(f"The plan wasn't readable: {e}") from e


_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def _clean_tags(s: str) -> str:
    tags = [t.strip() for t in re.split(r"[,\n]", str(s or "")) if t.strip()]
    tags = [t for t in tags if not re.match(r"(score_\d|source_|rating_)", t)]
    return ", ".join(dict.fromkeys(tags))


def fix_counts(tags: str) -> str:
    """Danbooru counting, whatever the small model wrote: "two girls" -> "2girls", and no "solo" next to a count of
    two or more (a "solo" tag made a two-girl request come out with one girl - 2026-10-14)."""
    out = []
    for t in [x.strip() for x in tags.split(",") if x.strip()]:
        m = re.fullmatch(r"(one|two|three|four|five|six|\d)\s*(girl|boy|woman|women|man|men)s?", t.lower())
        if m:
            n = int(m.group(1)) if m.group(1).isdigit() else _NUM[m.group(1)]
            noun = {"woman": "girl", "women": "girl", "man": "boy", "men": "boy"}.get(m.group(2), m.group(2))
            t = f"{n}{noun}" + ("s" if n > 1 else "")
        out.append(t)
    many = any(re.fullmatch(r"([2-9])(girls|boys)|multiple (girls|boys)", t) for t in out) or \
        (any(t == "1girl" for t in out) and any(t == "1boy" for t in out))
    if many:
        out = [t for t in out if t != "solo"]
        for t in list(out):
            g = re.fullmatch(r"[2-9](girls|boys)", t)
            if g and f"multiple {g.group(1)}" not in out:
                out.insert(out.index(t) + 1, f"multiple {g.group(1)}")
    return ", ".join(dict.fromkeys(out))


def safe_negative(negative: str, request: str, tags: str) -> str:
    """Drop negative tags that would fight the request: "no X" phrasing, or anything whose words are part of what was
    asked for (a checker once proposed avoiding "ice cream cone, smiling" for a request that wanted both)."""
    wanted = set(re.findall(r"[a-z]{3,}", f"{request} {tags}".lower()))
    keep = []
    for t in [x.strip() for x in str(negative or "").split(",") if x.strip()]:
        words = set(re.findall(r"[a-z]{3,}", t.lower()))
        if t.lower().startswith("no ") or (words and words <= wanted):
            continue
        keep.append(t)
    return ", ".join(keep)


def plan(request: str) -> dict:
    """{"thinking", "tags", "negative", "checks", "model"} for one picture."""
    out = llm.model_text(_PLAN_SYSTEM, f"Request: {request.strip()[:1500]}", max_tokens=600,
                         label="Planning the picture", temperature=0.4)
    data = _json(out["text"])
    tags = fix_counts(_clean_tags(data.get("tags")))
    if not tags:
        raise PlanError("The plan had no tags.")
    return {"thinking": str(data.get("thinking") or "")[:800], "tags": tags,
            "negative": safe_negative(_clean_tags(data.get("negative")), request, tags), "model": out.get("model"),
            "checks": [str(c)[:120] for c in (data.get("checks") or [])][:5]}


def plan_sequence(request: str, mode: str, count: int) -> dict:
    """{"thinking", "identity", "negative", "pictures": [{"label", "tags"}]} - exactly `count` pictures."""
    out = llm.model_text(_SEQ_SYSTEM, f"Mode: {mode}\nNumber of pictures: {count}\nRequest: {request.strip()[:1500]}",
                         max_tokens=900, label="Planning the sequence", temperature=0.4)
    data = _json(out["text"])
    pics = [{"label": str(p.get("label") or f"{i + 1}").replace("_", " ").strip()[:40], "tags": _clean_tags(p.get("tags"))}
            for i, p in enumerate(data.get("pictures") or []) if isinstance(p, dict)]
    identity = fix_counts(_clean_tags(data.get("identity")))
    if not identity or not pics:
        raise PlanError("The sequence plan was incomplete.")
    while len(pics) < count:                            # a short plan: repeat its last step rather than fail
        pics.append(dict(pics[-1], label=f"{pics[-1]['label']} ({len(pics) + 1})"))
    return {"thinking": str(data.get("thinking") or "")[:800], "identity": identity,
            "negative": _clean_tags(data.get("negative")), "pictures": pics[:count], "model": out.get("model")}


def vision_model() -> str | None:
    """The best local model that can see pictures (a models.ini preset with an mmproj), or None. Higher-precision
    quants first: Gemma 4 E2B at Q4 missed a broken extra figure that a check is there to catch (2026-10-14)."""
    seeing = [n for n in llama_local.server_models() or [] if llama_local.is_vision_model(n)]
    rank = lambda n: next((i for i, q in enumerate(("Q8", "Q6", "Q5", "Q4", "Q3", "Q2")) if q in n.upper()), 9)  # noqa: E731
    return sorted(seeing, key=rank)[0] if seeing else None


def check(image, request: str, checks: list) -> dict:
    """{"ok", "problems", "add_tags", "add_negative", "model"} - image is a PIL image."""
    model = vision_model()
    if not model:
        raise PlanError("No local model that can see pictures is set up (a models.ini preset with an mmproj).")
    facts = "\n".join(f"- {c}" for c in checks) or "- it matches the request"
    out = llm.model_text(_CHECK_SYSTEM, f"Request: {request.strip()[:800]}\nRequired:\n{facts}", max_tokens=400,
                         model=f"local:{model}", label="Checking the picture", temperature=0.1, images=[image])
    data = _json(out["text"])
    add_tags = fix_counts(_clean_tags(data.get("add_tags")))
    return {"ok": bool(data.get("ok", True)), "problems": [str(p)[:160] for p in (data.get("problems") or [])][:5],
            "add_tags": add_tags, "add_negative": safe_negative(_clean_tags(data.get("add_negative")), request, add_tags),
            "model": model}
