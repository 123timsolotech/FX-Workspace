"""App-specific Windows media commands. No foreground changes or global toggles.

API: https://learn.microsoft.com/uwp/api/windows.media.control.globalsystemmediatransportcontrolssession
Bindings: https://pywinrt.readthedocs.io/en/latest/types.html
"""
from __future__ import annotations

import asyncio
import os
import re


def _matches(source: str, app: str) -> bool:
    # Match a complete app identity component, never an arbitrary substring
    # or the system's default session (which could be a different app).
    key = app.casefold().removesuffix(".exe").strip()
    return bool(key and re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", source.casefold()))


def _state(session) -> str:
    status = session.get_playback_info().playback_status
    return getattr(status, "name", str(status)).casefold()


async def _control(manager, app: str, action: str) -> dict:
    all_sessions = list(manager.get_sessions())
    if app.casefold() == "active media":
        # A command such as "previous song" deliberately targets the one
        # session that is actually playing. Never use Windows' global/current
        # session when multiple players are active, because it may be stale.
        sessions = [s for s in all_sessions if _state(s) == "playing"]
        match_description = "playing Windows media"
    else:
        sessions = [s for s in all_sessions if _matches(s.source_app_user_model_id, app)]
        match_description = app
    if len(sessions) != 1:
        return {"ok": False, "verified": False,
                "message": f"Found {len(sessions)} matching sessions for {match_description}; no command was sent."}
    session = sessions[0]
    before = _state(session)
    quiet = {"paused", "stopped", "closed"}
    controls = session.get_playback_info().controls
    display_app = session.source_app_user_model_id or app
    if action in {"previous", "next"}:
        if before != "playing":
            return {"ok": False, "verified": False,
                    "message": f"{display_app} is {before}, so no {action.title()} command was sent."}
        enabled = (controls.is_previous_enabled if action == "previous"
                   else controls.is_next_enabled)
        if not enabled:
            return {"ok": False, "verified": False,
                    "message": f"{display_app} does not expose an enabled {action.title()} command."}
        method = "try_skip_previous_async" if action == "previous" else "try_skip_next_async"
        accepted = await getattr(session, method)()
        if not accepted:
            return {"ok": False, "verified": False,
                    "message": f"{display_app} rejected the background {action.title()} request."}
        return {"ok": True, "verified": True,
                "message": (f"Windows accepted one {action.title()} command for the active "
                            f"{display_app} media session. Its window was not opened or focused.")}
    if action == "play":
        if before == "playing":
            return {"ok": True, "verified": True,
                    "message": f"{app} is already playing; the current queue was left unchanged."}
        if before not in quiet:
            return {"ok": False, "verified": False,
                    "message": f"{app} playback state is {before}; no command was sent."}
        if not controls.is_play_enabled:
            return {"ok": False, "verified": False,
                    "message": f"{app} does not expose an enabled background Play command."}
        accepted = await session.try_play_async()
        if not accepted:
            return {"ok": False, "verified": False,
                    "message": f"{app} rejected the background Play request."}
        for _ in range(12):
            if _state(session) == "playing":
                return {"ok": True, "verified": True,
                        "message": f"{app} is playing, verified through its Windows media session. Its window was not opened or focused."}
            await asyncio.sleep(0.25)
        return {"ok": False, "verified": False,
                "message": f"Sent one background Play request to {app}, but playback was not verified. No repeat toggle was sent."}
    if before in quiet:
        return {"ok": True, "verified": True, "message": f"{app} is already {before}; no media command was sent."}
    if before != "playing":
        return {"ok": False, "verified": False, "message": f"{app} playback state is {before}; no command was sent."}
    # Some streaming apps expose Pause but no Stop; pausing still silences
    # music, but report the actual action instead of pretending it stopped.
    method = "stop" if action == "stop" and controls.is_stop_enabled else "pause"
    if method == "pause" and not controls.is_pause_enabled:
        return {"ok": False, "verified": False, "message": f"{app} does not expose an enabled {action}/pause media command."}
    accepted = await getattr(session, f"try_{method}_async")()
    if not accepted:
        return {"ok": False, "verified": False, "message": f"{app} rejected the background {method} request."}
    for _ in range(12):
        after = _state(session)
        if after in quiet:
            return {"ok": True, "verified": True,
                    "message": f"{app} is {after}, verified through its Windows media session. Its window was not opened or focused."}
        await asyncio.sleep(0.25)
    return {"ok": False, "verified": False,
            "message": f"Sent one background {method} request to {app}, but could not verify silence. No repeat toggle was sent."}


def control(app: str, action: str) -> dict:
    if action not in {"play", "stop", "pause", "previous", "next"}:
        return {"ok": False, "verified": False, "message": "Unsupported media command."}
    if os.name != "nt":
        return {"ok": False, "verified": False, "message": "Windows media sessions are unavailable on this system."}

    async def perform():
        from winrt.windows.media.control import GlobalSystemMediaTransportControlsSessionManager
        manager = await GlobalSystemMediaTransportControlsSessionManager.request_async()
        return await _control(manager, app, action)

    async def bounded():
        return await asyncio.wait_for(perform(), timeout=8)

    try:
        return asyncio.run(bounded())
    except ImportError:
        return {"ok": False, "verified": False,
                "message": "Windows media support is not installed. Restart using run.bat to install the updated requirements."}
    except TimeoutError:
        return {"ok": False, "verified": False, "message": "Windows media control timed out; playback was not verified."}
    except Exception as exc:
        return {"ok": False, "verified": False, "message": f"Windows media control failed: {str(exc)[:180]}"}
