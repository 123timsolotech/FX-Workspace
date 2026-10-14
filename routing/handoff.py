"""
routing/handoff.py
==================
Model registry and controlled model handoff (model_router.py).
(Split out of main.py in Phase 2 - see ARCHITECTURE.md D14.)
"""
from __future__ import annotations

import time
import llama_local
import model_caps
import model_router
import orchestrator
import system_metrics

import agent.models as llm
import agent.tasks as tasks
import core.config as config
import core.state as shared
import tools.schemas as schemas
import events as event_log


# ---- model registry + controlled handoff (model_router.py) -------------------
model_registry = model_router.ModelRegistry([
    lambda: model_router.local_models(llama_local, shared.app_state.get("local_engaged"), caps_fn=model_caps.get),
    lambda: model_router.cloud_models(config.PRIMARY_MODELS, config.VISION_MODELS, lambda m: bool(llm.get_keys_for_model(m))),
])


def _vram_total_gb():
    gpu = (system_metrics.snapshot_nonblocking() or {}).get("gpu") or {}
    total = gpu.get("vram_total_mb")
    return total / 1024 if total else None


def _load_model(model_id: str, provider: str):
    """The only code that switches the runtime for a handoff."""
    if provider == "cloud":
        shared.app_state["preferred_cloud_model"] = model_id
    else:
        if llama_local.server_models(force=True) is None:
            raise RuntimeError(llama_local.unreachable_message())
        shared.app_state["_handoff_in_progress"] = True
        try:
            system_metrics.begin_model_load()
            llama_local.force_load(model_id, timeout=180)
            system_metrics.finish_model_load(model_id)
            shared.app_state["local_model"] = model_id
            llm.engage_local_model(model_id)
        finally:
            shared.app_state["_handoff_in_progress"] = False
    # Persists: the UI adopts this as the selected model until the user picks another.
    shared.app_state["routed_model"] = {"model": model_id, "provider": provider, "at": time.time()}


def _handoff_catalogue() -> tuple[str, list]:
    """(one line per model a handoff may target now, their ids) - by the
    router's own rules, so the enum offered to the model is what it accepts."""
    targets, more = model_router.handoff_targets(model_registry.models(), shared.app_state.get("mode"), _vram_total_gb())
    return model_router.catalogue(targets, more), [m.id for m in targets]


schemas.handoff_catalogue = _handoff_catalogue


router = model_router.ModelRouter(model_registry, _load_model,
                                  enabled=lambda: bool(shared.app_state["settings"].get("auto_model_routing")),
                                  vram_gb=_vram_total_gb, emit=event_log.emit_activity)


def _handoff_tool(args: dict) -> dict:
    target = model_registry.get(args.get("target_model", ""))
    mode = shared.app_state.get("mode")
    if target is not None and mode == config.MODE_LOCAL and target.provider == "cloud":
        return {"ok": False, "status": "permanent_failure",
                "message": "Local mode keeps everything on this PC, so a cloud model can't be used. Pick a local model."}
    if target is not None and mode == config.MODE_CLOUD and target.provider == "local":
        return {"ok": False, "status": "permanent_failure", "message": "Cloud mode can't switch to a local model."}
    state = tasks.agent.current
    profile = orchestrator.EFFORT_PROFILES[state.effort if state else "normal"]
    return router.request_handoff(state, args.get("target_model", ""), args.get("reason", ""),
                                  llm._current_model(), profile.max_handoffs)
