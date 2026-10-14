"""
core/observer.py
================
Glue between the awareness engine (awareness.py) and the rest of the app.
(Split out of main.py in Phase 2 - see ARCHITECTURE.md D14.)
"""
from __future__ import annotations

import os
import shutil
import app_registry
import awareness
import context as ctx
import productivity_tools
import roblox_bridge
import system_metrics

import core.config as config
import core.state as shared


# ==============================================================================
# Proactive assistance (awareness.py) - wiring and endpoints
# ==============================================================================
def _disks() -> list:
    out = []
    try:
        import psutil
        parts = psutil.disk_partitions(all=False)
    except Exception:
        parts = []
    for part in parts:
        if "cdrom" in (part.opts or "") or not part.fstype:
            continue
        try:
            usage = shutil.disk_usage(part.mountpoint)
        except Exception:
            continue
        if usage.total < 8 * 1024 ** 3:        # ignore tiny recovery/USB volumes
            continue
        out.append({"mount": part.mountpoint, "free_gb": usage.free / 1024 ** 3,
                    "percent_free": 100.0 * usage.free / usage.total})
    return out


def _clipboard_text() -> str:
    try:
        return app_registry.get_clipboard() if config.IS_WINDOWS else ""
    except Exception:
        return ""


def _notify(title: str, message: str):
    productivity_tools.send_notification(f"FX Workspace · {title}"[:63], message[:250], 7)


engine = awareness.AwarenessEngine(
    settings_fn=lambda: shared.app_state["settings"],
    windows_fn=lambda: ctx.probe_windows(60),
    metrics_fn=lambda: system_metrics.snapshot(active_model=shared.app_state.get("local_engaged")),
    roblox=roblox_bridge,
    disks_fn=_disks,
    downloads_dir=os.path.join(os.path.expanduser("~"), "Downloads"),
    plan_fn=lambda: shared.task_board.as_dict(),
    clipboard_fn=_clipboard_text,
    notifier=_notify if config.IS_WINDOWS else None,
    store_path=os.path.join(config.BASE_DIR, "data", "awareness.json"),
    projects_fn=lambda: shared.project_store.list(),
    active_project_fn=lambda: shared.app_state.get("active_project"),
    routines_fn=lambda: shared.routine_store.list(),
)


def _activity_line() -> str:
    try:
        return engine.activity_line()
    except Exception:
        return ""
