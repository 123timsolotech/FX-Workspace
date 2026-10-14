"""
voice_duplex.py
===============
Full-duplex "micro-turn" voice, in the spirit of DuplexCascade (arXiv 2603.09180): a cascade of the models the
app already has - no fine-tuning, no speech-to-speech model.

While you talk, the page sends the utterance so far about every 0.6 s. Each tick is one micro-turn:

  hear     the accurate listener (Parakeet, CPU) transcribes the utterance so far (~0.2 s)
  decide   the local chat model, held to a grammar, answers with ONE action (~0.1 s on Gemma-4-E4B):
             <wait>        you're mid-thought - stay quiet, even through a pause ("so I was, um...")
             <mm>          a pause where a short "mm-hm" fits - you keep the turn
             <say> words   it's its turn: the reply streams back a few words at a time to the voice
             <do>          the request needs the full assistant (tools, files, the web) - the page hands it
                           to the normal turn-based pipeline
  speak    the page voices the words as they arrive (Kokoro, CPU), first a short clause so it starts quickly

The page keeps listening while it speaks: talking over it lowers its voice at once; a backchannel ("yeah",
"mm-hm") lets it carry on, anything else stops it, and what was actually heard of the reply is committed here
(commit()) so the conversation remembers the cut-off point.

Measured on this PC (RTX 3060 Ti 8 GB, Ryzen 5 3600): decision TTFT ~95 ms (Gemma-4-E4B), ~115 ms (Qwen 3.5 4B),
~290 ms (Ornith 9B), with a kept-alive connection - a new HTTP connection to the router costs 300-450 ms on
Windows, which is why one client is kept open here. Kokoro makes a 2-word clause in ~0.5 s.

The chat limits (chat_guard.py) screen each finished user turn before anything is said. Nothing is stored:
sessions live in memory and end with voice mode.
"""
from __future__ import annotations

import json
import re
import threading
import time

import httpx

import chat_guard
import llama_local
import netlayer

# The fastest measured model that makes good turn-taking calls; any configured model can be chosen instead.
PREFERRED = ("Gemma-4-E4B-Tools", "Qwen-3.5-4B-Q4_K_M")
HISTORY_TURNS = 12                  # messages of the voice conversation kept in the prompt
SESSION_TTL = 30 * 60
BACKCHANNELS = re.compile(r"^\W*(?:mm+[- ]?hm+|uh[- ]?huh|mhm|yeah|yep|yes|okay|ok|right|sure|got it|i see|true|exactly|cool|nice)\W*$", re.I)

SYSTEM = """You are a voice assistant in a live spoken conversation on the user's own PC. You hear the user's words \
as they speak, a few at a time, and decide each moment what to do. Reply with exactly one action:
<wait> - their sentence is unfinished: mid-sentence, thinking ("so I was...", "um", a trailing "and"). Say nothing.
<mm> - they paused after a complete thought inside a longer story, where a listener would say "mm-hm". Rare.
<say> your reply - they asked a question, made a request, or finished what they were saying. Take the turn at once: \
a finished question means it's your turn even if the pause is short. Speak like a person on a call: \
short, warm, natural, one to three sentences, no lists, no markdown, no emojis. Answer directly first.
<do> - they asked for something that needs tools: searching the web, current facts or news, opening, reading or \
changing files or apps on the PC, Roblox Studio, images, anything you can't just answer from knowledge.
<no CATEGORY> - answering would mean producing content outside this app's limits (below). Judge what is actually \
asked, not single words: education, history, news, fiction, safety information, security on their own systems and \
dark topics are fine.
Reply in the language the user speaks (English, Hebrew or Russian), in the same language as their last words.
Be honest: if you don't know, say so briefly. Never invent facts."""
# The chat limits (chat_guard.py), as part of the same decision: a separate check would be a second prompt, and
# with the router's single slot that throws away the prompt cache on every turn (+0.4-0.9 s measured).
LIMITS = {"minors": "any sexual or sexualised content involving a minor (under 18), in any form",
          "mass_harm": "real, actionable help making or deploying weapons of mass casualties",
          "malware": "working malicious code or attacks on systems or people they don't own or have permission to test",
          "self_harm": "they seek ways to harm or kill themselves, or say they intend to"}


def _limits_text(enabled: list) -> str:
    return "\n\nLimits (CATEGORY: what it covers):\n" + "\n".join(f"{k}: {LIMITS[k]}" for k in LIMITS if k in enabled)


def _grammar(final: bool, enabled: list) -> str:
    """Each micro-turn is held to one action; once the user has clearly stopped, waiting is no longer allowed."""
    cats = " | ".join(f'"{k}"' for k in LIMITS if k in enabled)
    actions = ([] if final else ['"<wait>"', '"<mm>"']) + ['"<do>"'] + ([f'"<no " ({cats}) ">"'] if cats else []) + ['"<say> " text']
    return "root ::= " + " | ".join(actions) + "\ntext ::= [^<]{1,700}"

_client: httpx.Client | None = None
_client_lock = threading.Lock()
_sessions: dict[str, dict] = {}
_sessions_lock = threading.Lock()


def _http() -> httpx.Client:
    """One kept-alive connection to the llama.cpp router (a new one costs 300-450 ms here)."""
    global _client
    with _client_lock:
        if _client is None:
            root = llama_local.LLAMA_SERVER_BASE.rstrip("/")
            root = root[:-3] if root.endswith("/v1") else root
            _client = netlayer.local_client(timeout=httpx.Timeout(120, connect=5))     # this PC only
            _client.base_url = root
        return _client


def model_for(settings: dict) -> str:
    """Settings > Voice > Live conversation model; by default the fastest good one that is configured."""
    chosen = str((settings or {}).get("voice_duplex_model") or "").strip()
    names = llama_local.server_models() or []
    if chosen and (not names or chosen in names):
        return chosen
    for name in PREFERRED:
        if name in names:
            return name
    return chosen or (names[0] if names else PREFERRED[0])


def _session(sid: str) -> dict:
    now = time.time()
    with _sessions_lock:
        for k in [k for k, v in _sessions.items() if now - v["at"] > SESSION_TTL]:
            _sessions.pop(k, None)
        s = _sessions.setdefault(str(sid)[:40], {"turns": [], "at": now, "strikes": 0, "screened": {}, "care": False})
        s["at"] = now
        return s


def end(sid: str) -> None:
    with _sessions_lock:
        _sessions.pop(str(sid)[:40], None)


def ensure_router() -> bool:
    """The llama.cpp router answers - started here as run.bat would when it isn't running (the app can be opened
    without run.bat, and then live conversation had nothing to talk to: "connection refused")."""
    import llama_builds
    if llama_builds._answers():
        return True
    kind = llama_builds.mode() if llama_builds.mode() != "auto" else "latest"
    exe = llama_builds.exe_of(kind) or llama_builds.exe_of("latest") or llama_builds.exe_of("prism")
    if not exe:
        return False
    with llama_builds._lock:
        if not llama_builds._answers():
            llama_builds._start(exe, llama_builds._args_like({}))
    return llama_builds._wait_ready(llama_builds.READY_TIMEOUT)


def warm(settings: dict) -> dict:
    """Load the conversation model now (voice mode opened), so the first answer isn't a model load."""
    t0 = time.perf_counter()
    if not ensure_router():
        return {"ok": False, "model": "", "message": "the local model server (llama-server) isn't installed or didn't start"}
    model = model_for(settings)
    try:
        _http().post("/v1/chat/completions", json={"model": model, "max_tokens": 1, "cache_prompt": True,
                     "messages": [{"role": "system", "content": SYSTEM}, {"role": "user", "content": "hi"}],
                     "chat_template_kwargs": {"enable_thinking": False}}).raise_for_status()
    except Exception as e:
        return {"ok": False, "model": model, "message": str(e)[:200]}
    return {"ok": True, "model": model, "ms": round((time.perf_counter() - t0) * 1000)}


def _messages(s: dict, heard: str, pause_ms: int, final: bool, enabled: list) -> list:
    msgs = [{"role": "system", "content": SYSTEM + _limits_text(enabled) + ("\n\n" + chat_guard.CARE_NOTE if s["care"] else "")}]
    for role, text in s["turns"][-HISTORY_TURNS:]:
        msgs.append({"role": role, "content": text})
    msgs.append({"role": "user", "content": heard + (" [they stopped talking]" if final else "")})
    return msgs


def _refuse(s: dict, heard: str, category: str):
    """A turn outside the limits: the refusal is spoken, and it counts as a strike in this voice conversation."""
    s["strikes"] += 1
    limit = max(1, chat_guard.load().get("strike_limit", 3))
    text = chat_guard.REFUSAL.format(label=chat_guard.CATEGORIES[category]["label"]) + \
        (chat_guard.STRIKE_NOTE if s["strikes"] < limit and category != "minors" else "")
    if category == "minors":
        s["strikes"] = limit                              # as in a chat: closed at once
    s["turns"] += [("user", heard), ("assistant", text)]
    yield {"action": "say", "refused": category}
    yield {"text": text}
    yield {"done": True}


def step(sid: str, heard: str, pause_ms: int, final: bool, settings: dict):
    """One micro-turn. Yields dicts: {"action", "model", "llm_ms"} first; then for "say" {"text": ...} pieces and
    finally {"done": True}. The user's words and the full reply are added to the session when a reply is spoken."""
    s, model = _session(sid), model_for(settings)
    heard = " ".join(str(heard or "").split())[:2000]
    if not heard:
        yield {"action": "wait", "model": model, "llm_ms": 0}
        return
    cfg = chat_guard.load()
    if s["strikes"] >= max(1, cfg.get("strike_limit", 3)):
        yield {"action": "locked", "text": chat_guard.LOCKED}
        return
    previous = next((t for role, t in reversed(s["turns"]) if role == "user"), "")
    if chat_guard._rule_minors(heard) or (previous and chat_guard._rule_minors(f"{previous}\n{heard}")):
        if final or re.search(r"[.?!]\s*$", heard):       # the rule needs no model; wait for the finished request
            yield from _refuse(s, heard, "minors")
        else:
            yield {"action": "wait", "model": model, "llm_ms": 0}
        return
    enabled = [c for c in cfg.get("categories", list(LIMITS)) if c in LIMITS] + ["minors"]
    t0 = time.perf_counter()
    body = {"model": model, "stream": True, "max_tokens": 160, "temperature": 0.5, "top_p": 0.9, "cache_prompt": True,
            "grammar": _grammar(final, enabled), "chat_template_kwargs": {"enable_thinking": False},
            "messages": _messages(s, heard, int(pause_ms), final, enabled)}
    action, buf, reply, first_ms = None, "", "", None
    with _http().stream("POST", "/v1/chat/completions", json=body) as r:
        r.raise_for_status()
        for line in r.iter_lines():
            if not line.startswith("data: ") or line.strip() == "data: [DONE]":
                continue
            try:
                delta = (json.loads(line[6:]).get("choices") or [{}])[0].get("delta", {}).get("content") or ""
            except ValueError:
                continue
            if not delta:
                continue
            if first_ms is None:
                first_ms = round((time.perf_counter() - t0) * 1000)
            if action is None:
                buf += delta
                m = re.match(r"\s*<(wait|mm|do|say|no (\w+))>\s?", buf)
                if not m:
                    if len(buf) > 24:
                        action = "wait"
                        break
                    continue
                action, rest = m.group(1), buf[m.end():]
                if action.startswith("no "):
                    category = m.group(2)
                    if category == "self_harm":
                        if not s["care"]:
                            s["care"] = True              # answered - with care, never with a method
                            action = "care"
                        else:
                            action = "say_care"
                        break
                    r.close()
                    yield from _refuse(s, heard, category)
                    return
                if action != "say":
                    break                                # the decision is all we need
                yield {"action": "say", "model": model, "llm_ms": first_ms}
                if rest:
                    reply += rest
                    yield {"text": rest}
                continue
            reply += delta
            yield {"text": delta}
    if action == "care":                                  # answer this turn again, now with the care note
        yield from step(sid, heard, pause_ms, True, settings)
        return
    if action == "say_care":                              # flagged again with the note already on: the fixed reply
        s["turns"] += [("user", heard), ("assistant", chat_guard.CARE_REPLY)]
        yield {"action": "say", "model": model, "llm_ms": first_ms}
        yield {"text": chat_guard.CARE_REPLY}
        yield {"done": True}
        return
    if action != "say":
        yield {"action": action or "wait", "model": model, "llm_ms": first_ms}
        return
    s["turns"] += [("user", heard), ("assistant", reply.strip())]
    yield {"done": True}


def commit(sid: str, spoken: str) -> None:
    """Talked over: keep only what was actually heard of the last reply (marked as cut off)."""
    s = _session(sid)
    if s["turns"] and s["turns"][-1][0] == "assistant":
        spoken = " ".join(str(spoken or "").split())
        s["turns"][-1] = ("assistant", (spoken + " —" if spoken else "—") + " [interrupted]")


def is_backchannel(text: str) -> bool:
    """"mm-hm", "yeah", "right" said over the reply: the user is listening, not taking the turn."""
    return bool(BACKCHANNELS.match(str(text or "").strip()))
