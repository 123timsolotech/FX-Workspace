"""
mini_window.py
==============
The mini window (1.4.2): the always-on-top panels (the ?hud=1 page) in a small window of the desktop app itself -
it used to be a separate Edge window (app_window.py, still used outside the desktop app). Its state is settings:

  overlay_window        open or closed
  overlay_opacity       0.3 .. 1
  overlay_clickthrough  clicks pass through to what's under it
  mini_place            "studio" (over Roblox Studio, following it), a corner "tr" "tl" "br" "bl", or "free"

The desktop app reads shell_state() (GET /api/shell) about once a second and applies it, so Settings, the tray, the
top bar and the assistant's mini_window tool (apply()) all change the same thing.
"""
from __future__ import annotations

import app_window
import core.config as config
import core.state as shared

ACTIONS = ("open", "close", "toggle", "set")
TITLE = "FX Workspace mini"                            # desktop/main.js keeps this title on the window
PLACE_WORDS = {"studio": "over Roblox Studio", "tr": "in the top-right corner", "tl": "in the top-left corner",
               "br": "in the bottom-right corner", "bl": "in the bottom-left corner", "free": "where you put it"}


def shell_state() -> dict:
    s = shared.app_state["settings"]
    is_open = bool(s.get("overlay_window"))
    place = s.get("mini_place") if s.get("mini_place") in config.MINI_PLACES else "studio"
    if is_open and app_window.in_desktop_app():
        app_window.keep_on_top(TITLE)                  # read about once a second: a lost "on top" comes back
    return {"close_to_tray": s.get("close_to_tray", True) is not False,
            "login": {"start_with_windows": bool(s.get("start_with_windows")), "start_hidden": s.get("start_hidden", True) is not False},
            "mini": {"open": is_open, "opacity": float(s.get("overlay_opacity") or 0.88),
                     "click_through": bool(s.get("overlay_clickthrough")), "place": place},
            "studio_rect": app_window.studio_rect() if is_open and place == "studio" else None}


def apply(action: str = "toggle", opacity=None, click_through=None, place=None) -> dict:
    """The mini_window tool and the top bar's button: open, close, toggle or restyle it. Changes only this app's own
    window - nothing outside the app - so it runs without asking."""
    action = action if action in ACTIONS else "toggle"
    s = dict(shared.app_state["settings"])
    if action == "open":
        s["overlay_window"] = True
    elif action == "close":
        s["overlay_window"] = False
    elif action == "toggle":
        s["overlay_window"] = not s.get("overlay_window")
    elif not any(v is not None for v in (opacity, click_through, place)):
        return {"ok": False, "message": "Say what to change: opacity, click_through or place."}
    if opacity is not None:
        try:
            value = float(opacity)
        except (TypeError, ValueError):
            return {"ok": False, "message": "Opacity is a number from 0.3 to 1 (or a percent)."}
        value = value / 100 if value > 1 else value
        s["overlay_opacity"] = round(max(0.3, min(1.0, value)), 2)
    if click_through is not None:
        s["overlay_clickthrough"] = bool(click_through)
    if place is not None:
        if place not in config.MINI_PLACES:
            return {"ok": False, "message": f"Place is one of: {', '.join(config.MINI_PLACES)}."}
        s["mini_place"] = place
    cleaned = config.clean_privacy_settings(s)
    s.update({k: cleaned[k] for k in ("overlay_window", "overlay_opacity", "overlay_clickthrough", "mini_place")})
    shared.app_state["settings"] = s
    config.save_settings(s)
    app_window.overlay_sync(s)                         # outside the desktop app: the Edge window
    st = shell_state()["mini"]
    if not st["open"]:
        return {"ok": True, "message": "Closed the mini window.", "mini": st}
    return {"ok": True, "mini": st,
            "message": (f"The mini window is open {PLACE_WORDS[st['place']]}, at {round(st['opacity'] * 100)}% opacity"
                        + (", clicks pass through it." if st["click_through"] else "."))}
