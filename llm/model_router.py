"""
model_router.py
===============
Controlled model registry and handoff.

Models never touch model processes themselves. When automatic routing is on,
the agent may call the request_model_handoff tool; the controller below
validates the request and performs the switch through an injected loader:

  1. routing enabled?          6. not a switch back to a model this task left
  2. target known + available  7. cooldown since the last handoff
  3. capability fits reason    8. serialize the task state
     (vision, context)         9. load / switch through the loader
  4. fits in GPU memory       10. on failure, put the previous model back
  5. per-task handoff limit

Every step emits a structured activity event. The loader is the only thing
that changes the runtime (main.py: llama_local.force_load for local models,
a preferred-model setting for cloud ones).

What a model is good at comes from measurements (Phase 5): the per-model
benchmark (benchmark.py, stored in model_caps) gives tool-call validity,
reply latency, generation speed and usable context; the server gives the
context window and whether a model reads images. Model names are never used
to guess strengths. A local model whose benchmark shows unreliable tool
calls is not a handoff target.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field, asdict

MIN_HANDOFF_VALIDITY = 0.5         # a benchmarked local model below this is not a handoff target


@dataclass
class ModelInfo:
    id: str
    display_name: str
    provider: str                       # local | cloud
    text: bool = True
    vision: bool = False
    tool_use: bool | None = True        # None = unknown
    context_limit: int | None = None
    speed: str | None = None            # fast | medium | slow
    memory_gb: float | None = None      # approximate VRAM/RAM to load it
    strengths: list = field(default_factory=list)
    available: bool = False
    loading_status: str = "unknown"     # loaded | available | configured | unavailable
    efforts: tuple = ("low", "normal", "high", "extra", "max", "ultra")
    reasoning: bool = False
    # Measured by the benchmark (None = not benchmarked yet)
    tool_call_validity: float | None = None
    latency_ms: int | None = None
    tokens_per_s: float | None = None
    usable_context: int | None = None
    benchmarked_at: float | None = None

    def public(self) -> dict:
        return asdict(self)


def measured_strengths(caps: dict) -> list:
    """What the benchmark and the server showed - nothing from the name."""
    out = []
    v = caps.get("tool_call_validity")
    if isinstance(v, (int, float)) and v >= 0.9:
        out.append("reliable tool calls")
    tps = caps.get("tokens_per_s")
    if isinstance(tps, (int, float)) and tps >= 30:
        out.append("fast generation")
    usable = caps.get("usable_context") or caps.get("usable_context_min")      # measured, or measured at least
    if isinstance(usable, int) and usable >= 32_000:
        out.append("long context")
    if caps.get("reasoning") in ("channel", "inline"):
        out.append("reasoning")
    return out


def _clamp(usable, window):
    """A usable context measured under a larger window never exceeds today's."""
    return min(usable, window) if isinstance(usable, int) and isinstance(window, int) and window else usable


def local_models(llama_local, engaged: str | None, caps_fn=None) -> list[ModelInfo]:
    """Registry entries for local models: models.ini presets plus anything
    else llama-server reports, with what was measured about each
    (`caps_fn(name)` -> model_caps record)."""
    import effort
    caps_fn = caps_fn or (lambda name: {})
    reported = llama_local.server_models() or []
    try:
        configs = {c["name"]: c for c in llama_local.list_model_configs()}
    except Exception:
        configs = {}
    names = list(configs) + [m for m in reported if llama_local.normalize_name(m) not in
                             {llama_local.normalize_name(c) for c in configs}]
    reported_keys = {llama_local.normalize_name(m) for m in reported}
    out = []
    for name in names:
        size = llama_local.model_file_size(name)
        gb = round(size / 1024 ** 3, 1) if size else None
        caps = caps_fn(name) or {}
        tps = caps.get("tokens_per_s")
        # Measured generation speed when benchmarked; otherwise the file size.
        speed = (("fast" if tps >= 30 else "medium" if tps >= 12 else "slow") if isinstance(tps, (int, float))
                 else None if gb is None else "fast" if gb < 3 else "medium" if gb < 7 else "slow")
        is_reported = llama_local.normalize_name(name) in reported_keys
        loaded = bool(engaged) and llama_local.normalize_name(engaged) == llama_local.normalize_name(name)
        out.append(ModelInfo(
            id=name, display_name=re.sub(r"\.gguf$", "", name, flags=re.I), provider="local",
            vision=bool(caps.get("vision") if caps.get("vision") is not None else llama_local.vision_known(name)),
            tool_use=True,
            context_limit=llama_local.model_context_limit(name), speed=speed,
            memory_gb=round(gb * 1.15, 1) if gb else None,          # weights + KV cache headroom
            strengths=measured_strengths(caps), available=is_reported or name in configs,
            loading_status="loaded" if loaded else "available" if is_reported else "configured",
            reasoning=caps.get("reasoning") in ("channel", "inline", "template"),
            efforts=tuple(effort.supported_levels(caps)),
            tool_call_validity=caps.get("tool_call_validity"), latency_ms=caps.get("latency_ms"),
            tokens_per_s=tps, usable_context=_clamp(caps.get("usable_context"), llama_local.model_context_limit(name)),
            benchmarked_at=caps.get("benchmarked_at")))
    return out


def cloud_models(text_models: list, vision_models: list, has_key) -> list[ModelInfo]:
    out = {}
    for name in list(text_models) + list(vision_models):
        if name in out:
            continue
        ok = bool(has_key(name))
        out[name] = ModelInfo(
            id=name, display_name=name.split("/", 1)[-1], provider="cloud",
            vision=name in vision_models, tool_use=True, context_limit=None,      # not measured: not claimed
            strengths=[], available=ok, loading_status="available" if ok else "unavailable",
            reasoning=False)
    return list(out.values())


_SAFE_ID = re.compile(r"^[^\x00-\x1f\x7f]{1,100}$")


def handoff_targets(models: list, mode: str | None, vram_gb: float | None = None, limit: int = 20) -> tuple[list, int]:
    """(models a handoff may target, how many more exist) - the router's own
    rules: available, allowed in this mode (Local: local, Cloud: cloud, Auto:
    both), not measured as unreliable, fits in GPU memory, a plain id."""
    allowed = {"local": {"local"}, "cloud": {"cloud"}}.get(mode or "", {"local", "cloud"})
    out = [m for m in models if m.available and m.provider in allowed and _SAFE_ID.match(m.id or "")
           and not (m.provider == "local" and isinstance(m.tool_call_validity, (int, float))
                    and m.tool_call_validity < MIN_HANDOFF_VALIDITY)
           and not (vram_gb and m.memory_gb and m.memory_gb > vram_gb * 1.05)]
    return out[:limit], max(0, len(out) - limit)


def catalogue(models: list, more: int = 0) -> str:
    """One line per target: for local models what was measured on this PC,
    for cloud models what is configured (never guessed from names)."""
    lines = []
    for m in models:
        facts = [m.provider]
        if m.vision:
            facts.append("reads images")
        if m.provider == "local":
            window = m.usable_context or m.context_limit
            if window:
                facts.append(f"{'usable ' if m.usable_context else ''}context {window:,} tokens")
            if m.tool_call_validity is not None:
                facts.append(f"{round(m.tool_call_validity * 100)}% valid tool calls in its benchmark")
                if m.tokens_per_s:
                    facts.append(f"~{m.tokens_per_s:g} tokens/s")
            else:
                facts.append("not benchmarked")
        lines.append(f"- {m.id}: " + ", ".join(facts))
    if more:
        lines.append(f"({more} more not listed)")
    return "\n".join(lines)


class ModelRegistry:
    def __init__(self, sources):
        """sources: callables returning lists of ModelInfo."""
        self._sources = sources

    def models(self) -> list[ModelInfo]:
        out = []
        for source in self._sources:
            try:
                out.extend(source())
            except Exception:
                continue
        return out

    def get(self, model_id: str) -> ModelInfo | None:
        key = re.sub(r"[\s_.]+", "-", (model_id or "").lower().removesuffix(".gguf"))
        for m in self.models():
            if m.id == model_id or re.sub(r"[\s_.]+", "-", m.id.lower().removesuffix(".gguf")) == key:
                return m
        return None


class ModelRouter:
    def __init__(self, registry: ModelRegistry, loader, *, enabled, vram_gb=lambda: None,
                 emit=lambda *a, **k: None, clock=time.monotonic, cooldown_s: float = 60.0):
        self.registry = registry
        self.loader = loader              # loader(model_id, provider) -> None, raises on failure
        self.enabled = enabled            # () -> bool
        self.vram_gb = vram_gb            # () -> total GPU memory in GB, or None
        self.emit = emit
        self.clock = clock
        self.cooldown_s = cooldown_s
        self._last_handoff = None

    def _refuse(self, task, target, reason, why):
        self.emit("handoff_refused", f"Model switch to {target} refused: {why}", level="warn",
                  handoff={"to": target, "reason": reason, "refused": why})
        return {"ok": False, "status": "permanent_failure", "message": f"Model handoff refused: {why}"}

    def request_handoff(self, task, target_id: str, reason: str, current: str | None,
                        max_handoffs: int, min_context: int | None = None) -> dict:
        if not self.enabled():
            return {"ok": False, "status": "unavailable",
                    "message": "Automatic model routing is off (Settings > Agent). Keep using the current model."}
        target = self.registry.get(target_id)
        if target is None:
            return self._refuse(task, target_id, reason, "that model is not in the registry")
        if current and self.registry.get(current) and self.registry.get(current).id == target.id:
            return {"ok": True, "status": "success", "message": f"{target.id} is already the active model."}
        if not target.available:
            return self._refuse(task, target.id, reason, "that model is not available right now")
        needs_vision = bool(re.search(r"vision|image|screenshot|picture|visual", reason or "", re.I))
        if needs_vision and not target.vision:
            return self._refuse(task, target.id, reason, "it cannot read images")
        window = target.usable_context or target.context_limit       # measured usable context first
        if min_context and window and window < min_context:
            return self._refuse(task, target.id, reason, f"its context window ({window}) is too small")
        if (target.provider == "local" and isinstance(target.tool_call_validity, (int, float))
                and target.tool_call_validity < MIN_HANDOFF_VALIDITY):
            return self._refuse(task, target.id, reason, f"its benchmark showed unreliable tool calls "
                                f"({round(target.tool_call_validity * 100)}% valid)")
        total = self.vram_gb()
        if total and target.memory_gb and target.memory_gb > total * 1.05:
            return self._refuse(task, target.id, reason, f"it needs ~{target.memory_gb} GB but the GPU has {total:.0f} GB")
        if task is not None and task.handoff_count >= max_handoffs:
            return self._refuse(task, target.id, reason, f"this task already used its {max_handoffs} model switch(es)")
        if task is not None and target.id in task.model_history:
            return self._refuse(task, target.id, reason, "the task already switched away from that model (loop prevented)")
        now = self.clock()
        if self._last_handoff is not None and now - self._last_handoff < self.cooldown_s:
            return self._refuse(task, target.id, reason,
                                f"models were switched {now - self._last_handoff:.0f}s ago (cooldown {self.cooldown_s:.0f}s)")

        snapshot = task.snapshot() if task is not None else {}
        self.emit("handoff_requested", f"Switching model: {current or 'auto'} → {target.display_name}",
                  handoff={"from": current, "to": target.id, "reason": reason})
        try:
            self.loader(target.id, target.provider)
        except Exception as e:
            self.emit("handoff_failed", f"Couldn't switch to {target.display_name} - staying on {current or 'the current model'}",
                      level="error", handoff={"from": current, "to": target.id, "reason": reason, "error": str(e)[:200]})
            if current:
                try:
                    prev = self.registry.get(current)
                    self.loader(current, prev.provider if prev else target.provider)
                except Exception:
                    pass
            return {"ok": False, "status": "permanent_failure",
                    "message": f"Model handoff to {target.id} failed ({str(e)[:160]}); still using {current or 'the previous model'}."}
        self._last_handoff = now
        if task is not None:
            task.record_handoff(current, target.id, reason)
        self.emit("handoff_completed", f"{current or 'auto'} → {target.display_name}",
                  handoff={"from": current, "to": target.id, "reason": reason})
        return {"ok": True, "status": "success", "data": {"from": current, "to": target.id, "snapshot_keys": sorted(snapshot)},
                "message": f"Now running on {target.id}. Continue the same task; your plan and history are unchanged."}
