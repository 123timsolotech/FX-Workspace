"""Host-side execution of simple, explicit desktop intents.

This is intentionally narrow.  Opening a named installed app and pressing a
well-labelled connect control do not require model reasoning; keeping those
operations deterministic makes them reliable even with very small models.
"""
from __future__ import annotations

import os
import re
import time

import app_registry
import context as ctx
import desktop_input as di
import elements
import generic_actions
import media_session
import roblox_places


def _wait_for_window(app_name: str, timeout: float = 20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        found = di.find_window(app_name)
        if found:
            return found
        time.sleep(0.35)
    return None


def _launch_place(place: dict) -> dict:
    os.startfile(place["path"])
    found = _wait_for_window(place["display"], timeout=25.0)
    if not found:
        return {
            "ok": True, "title": place["display"], "needs_agent": True,
            "message": (f"Opened the Roblox place '{place['display']}', but its Studio "
                        "window did not appear within 25 seconds."),
        }
    if not di.focus_window(found[0]):
        return {"ok": False, "title": found[1], "message": f"Opened '{place['display']}', but could not focus its window."}
    return {"ok": True, "title": found[1], "message": (
        f"Opened the Roblox place '{place['display']}'. If the Agentic Bridge plugin is turned "
        "on in Studio (Plugins tab > Bridge) it connects by itself once the place has loaded; the "
        "Roblox Studio dot in the sidebar turns green.")}


def _open_place(name: str) -> dict:
    """'open Test1 in studio': a place file only - never an app of that name."""
    if not di.IS_WINDOWS:
        return {"ok": False, "message": "Opening places is available on Windows only."}
    place, candidates = roblox_places.resolve_place(name)
    if place:
        return _launch_place(place)
    if candidates:
        names = [c["display"] for c in candidates]
        return {"ok": False, "clarification": True, "options": names, "message": ", ".join(names),
                "question": f"Which Roblox place do you mean by '{name}'?"}
    return {"ok": False, "message": (
        f"There is no saved place file named '{name}' (searched {', '.join(roblox_places._default_dirs())}). "
        "If it is a place saved to Roblox rather than as a file, open it from Studio's start page "
        "(Recent or My Games) - I can't open cloud places directly. Once it's open, I can work in it.")}


def _open_app(app_name: str) -> dict:
    if not di.IS_WINDOWS:
        return {"ok": False, "message": "Desktop app routing is available on Windows only."}

    already = ctx.window_is_open(app_name)
    if already:
        hwnd, title = already
        if di.get_active_window_title() == title:
            return {"ok": True, "title": title, "message": f"'{title}' is already open and focused."}
        if not di.focus_window(hwnd):
            return {"ok": False, "title": title, "message": f"'{title}' is open, but Windows did not allow it to be focused."}
        return {"ok": True, "title": title, "message": f"Focused the existing '{title}' window."}

    match, candidates = app_registry.resolve_app(app_name)
    if not match:
        # Not an installed application - but it might be a Roblox PLACE
        # file (this is exactly what 'open Test1' needed and did not have:
        # a place is not in the installed-app index at all).
        place_match, place_candidates = roblox_places.resolve_place(app_name)
        if place_match:
            return _launch_place(place_match)
        all_candidates = candidates + place_candidates
        if all_candidates:
            names = [item["display"] for item in all_candidates]
            return {
                "ok": False,
                "clarification": True,
                "question": f"I found multiple matches for '{app_name}' across installed apps and Roblox places. Which one should I open?",
                "options": names,
                "message": ", ".join(names),
            }
        return {"ok": False, "message": (
            f"'{app_name}' is not in the installed-app index or the Roblox places index "
            f"(searched {', '.join(roblox_places._default_dirs()) or 'no configured folders - set ROBLOX_PLACES_DIRS'}). "
            "I did not open a browser or substitute another app.")}

    os.startfile(match["path"])
    found = _wait_for_window(match["display"]) or _wait_for_window(app_name, timeout=4.0)
    if not found:
        return {
            "ok": True,
            "title": match["display"],
            "message": f"Launched '{match['display']}', but its main window did not appear within 24 seconds.",
            "needs_agent": True,
        }
    if not di.focus_window(found[0]):
        return {"ok": False, "title": found[1], "message": f"Opened '{match['display']}', but could not focus its window."}
    return {"ok": True, "title": found[1], "message": f"Opened '{match['display']}'."}


def _close_app(app_name: str) -> dict:
    if not di.IS_WINDOWS:
        return {"ok": False, "verified": False, "message": "Desktop app routing is available on Windows only."}
    found = ctx.window_is_open(app_name)
    if not found:
        return {"ok": True, "verified": True, "message": f"{app_name} is already closed."}
    hwnd, title = found
    if not di.close_window(hwnd):
        return {"ok": False, "verified": False, "title": title,
                "message": f"Windows did not allow '{title}' to close."}
    for _ in range(10):
        time.sleep(0.2)
        if not di.find_window(title):
            return {"ok": True, "verified": True, "title": title,
                    "message": f"Closed '{title}' and verified that its window is gone."}
    return {"ok": False, "verified": False, "title": title,
            "message": f"Sent a close request to '{title}', but its window was still present after two seconds."}


def _entry_text(entry: dict) -> str:
    return " ".join(str(entry.get(key) or "") for key in ("name", "value", "automation_id")).strip()


def _drive_visible(entries: list[dict], drive: str) -> bool:
    token = drive.casefold().rstrip("\\/")
    for entry in entries:
        text = _entry_text(entry).casefold()
        if re.search(rf"(?<![a-z0-9]){re.escape(token)}(?:[\\/]|\b|\s|$)", text):
            if entry.get("selected") is True or entry.get("control_type") in {"ComboBox", "Edit"}:
                return True
    return False


def _select_drive(title: str, drive: str, listing: dict) -> tuple[bool, str, dict]:
    def selector(current: dict):
        entries = current.get("entries") or []
        combos = [entry for entry in entries
                  if entry.get("control_type") == "ComboBox" and entry.get("enabled", True)]
        preferred = [entry for entry in combos if "drive" in _entry_text(entry).casefold()]
        candidates = preferred or combos
        return candidates[0] if len(candidates) == 1 else None, len(candidates)

    current = listing
    if _drive_visible(current.get("entries") or [], drive):
        return True, f"{drive} was already selected.", current
    combo, count = selector(current)
    if not combo:
        return False, f"Found {count} possible drive selectors; no drive was changed.", current

    errors = []
    # Never reuse a selector handle after an action. WizTree refreshes its
    # ComboBox when the dropdown changes, making the old UIA wrapper stale.
    # Each exact selection attempt starts from a freshly enumerated selector.
    for _ in range(2):
        result = elements.act_element(combo["id"], "select_drive", drive)
        if not result.get("ok"):
            errors.append(result.get("message", "selection failed"))
        current = elements.list_elements(title, max_items=240)
        if _drive_visible(current.get("entries") or [], drive):
            return True, f"Selected drive {drive}.", current
        combo, count = selector(current)
        if not combo:
            errors.append(f"fresh lookup found {count} drive selectors")
            break

    # Some WizTree builds expose choices only after expansion, and name them
    # like "[G:] System" rather than a literal "G:". Match the drive token
    # anywhere in the option and select the one exact live item.
    if combo:
        expanded = elements.act_element(combo["id"], "expand")
        if not expanded.get("ok"):
            errors.append(expanded.get("message", "could not expand drive selector"))
        current = elements.list_elements(title, max_items=240)
        drive_items = [entry for entry in current.get("entries") or []
                       if entry.get("control_type") in {"ListItem", "TreeItem"}
                       and re.search(rf"(?<![a-z0-9]){re.escape(drive)}(?:[\\/]|\]|\s|$)",
                                     _entry_text(entry), re.I)]
        if len(drive_items) == 1:
            chosen = elements.act_element(drive_items[0]["id"], "select")
            check = elements.list_elements(title, max_items=240)
            if chosen.get("ok") and _drive_visible(check.get("entries") or [], drive):
                return True, f"Selected drive {drive}.", check
            errors.append(chosen.get("message", "drive item selection failed"))
        elif drive_items:
            errors.append(f"found {len(drive_items)} matching {drive} options")
        else:
            errors.append(f"no live option for {drive} appeared after expanding the selector")
    return False, f"Could not select drive {drive}: {'; '.join(errors)[:360]}", current


def _scan_complete_evidence(entries: list[dict]) -> bool:
    text = " | ".join(_entry_text(entry).casefold() for entry in entries)
    if re.search(r"scan(?:ning)?\s+(?:complete|completed|finished)|\d[\d,]*\s+(?:files?|folders?)", text):
        return True
    headers = sum(token in text for token in ("allocated", "modified", "file name", "% of parent", "items"))
    return headers >= 2


def _scan_drive(title: str, drive: str) -> dict:
    """Drive WizTree directly and verify that the requested drive produced results."""
    drive = (drive or "").strip().upper().rstrip("\\/")
    if not re.fullmatch(r"[A-Z]:", drive):
        return {"ok": False, "verified": False, "message": f"Invalid drive '{drive}'."}
    listing = elements.list_elements(title, max_items=240)
    if not listing.get("ok") or not listing.get("entries"):
        return {"ok": False, "verified": False, "message": listing.get("message", "WizTree exposed no controls.")}
    selected, selection_message, listing = _select_drive(title, drive, listing)
    if not selected:
        return {"ok": False, "verified": False, "message": selection_message}
    entries = listing.get("entries") or []
    scan_buttons = [entry for entry in entries
                    if entry.get("enabled", True) and entry.get("control_type") == "Button"
                    and re.fullmatch(r"(?:re)?scan", (entry.get("name") or "").strip(), re.I)]
    if len(scan_buttons) != 1:
        return {"ok": False, "verified": False,
                "message": f"{selection_message} Found {len(scan_buttons)} unambiguous Scan buttons; scanning was not started."}
    clicked = elements.act_element(scan_buttons[0]["id"], "click")
    if not clicked.get("ok"):
        return {"ok": False, "verified": False,
                "message": f"{selection_message} {clicked.get('message', 'Scan click failed.')}"}

    observed_busy = False
    for _ in range(40):
        time.sleep(0.5)
        current = elements.list_elements(title, max_items=240)
        current_entries = current.get("entries") or []
        names = " | ".join(_entry_text(entry).casefold() for entry in current_entries)
        scan_enabled = any(entry.get("enabled", True) and re.fullmatch(
            r"(?:re)?scan", (entry.get("name") or "").strip(), re.I) for entry in current_entries)
        if any(word in names for word in ("scanning", "reading mft", "cancel scan", "stop scan")) or not scan_enabled:
            observed_busy = True
        if _drive_visible(current_entries, drive) and scan_enabled and (observed_busy or _scan_complete_evidence(current_entries)):
            return {"ok": True, "verified": True,
                    "message": f"WizTree completed and verified a scan of drive {drive}."}
    return {"ok": False, "verified": False,
            "message": f"Started one WizTree scan of drive {drive}, but completion could not be verified within 20 seconds."}


def execute(intent) -> dict:
    if "close" in intent.actions:
        return _close_app(intent.app_name)

    media_action = next((a for a in intent.actions if a in {"play", "stop", "pause", "previous", "next"}), None)
    if media_action:
        return _execute_media(intent.app_name, media_action)

    place_only = bool((getattr(intent, "arguments", None) or {}).get("place_only"))
    opened = _open_place(intent.app_name) if place_only else _open_app(intent.app_name)
    if not opened.get("ok") or opened.get("clarification"):
        return opened
    if opened.get("needs_agent"):
        return {**opened, "ok": False, "verified": False}
    messages = [opened["message"]]
    title = opened.get("title") or intent.app_name

    if "connect" in intent.actions:
        result = generic_actions.run(title, "connect")
        messages.append(result.get("message", ""))
        return {**result, "message": " ".join(m for m in messages if m), "title": opened.get("title")}
    if "disconnect" in intent.actions:
        result = generic_actions.run(title, "disconnect")
        messages.append(result.get("message", ""))
        return {**result, "message": " ".join(m for m in messages if m), "title": opened.get("title")}
    if "status" in intent.actions:
        result = generic_actions.status(title, "connect")
        messages.append(result.get("message", ""))
        return {**result, "message": " ".join(m for m in messages if m), "title": opened.get("title")}
    if "scan" in intent.actions:
        scanned = _scan_drive(title, intent.arguments.get("drive", ""))
        return {**scanned, "message": opened["message"] + " " + scanned.get("message", ""),
                "title": opened.get("title")}
    return {"ok": True, "verified": True, "message": messages[0], "title": opened.get("title")}


def _execute_media(app_name: str, media_action: str) -> dict:
    """Background media transport (play/stop/pause/previous/next) via the
    Windows media-session API, which is already app-agnostic (it addresses
    sessions by app identity, not by any per-app code). If no session exists
    yet for play/stop/pause, fall back to the generic on-screen control -
    the same elements.py-based path used for connect/disconnect - instead of
    silently giving up. 'previous'/'next' have no generic on-screen
    equivalent to fall back to, so those return the media-session result as-is.
    """
    background = media_session.control(app_name, media_action)
    no_session = str(background.get("message", "")).startswith("Found 0 matching")
    if background.get("ok") or not no_session:
        return background
    if media_action in ("previous", "next"):
        return background

    opened = _open_app(app_name)
    if not opened.get("ok") or opened.get("clarification"):
        return opened
    title = opened.get("title") or app_name

    if media_action == "play":
        for _ in range(8):
            time.sleep(0.4)
            retry = media_session.control(app_name, "play")
            if retry.get("ok") or not str(retry.get("message", "")).startswith("Found 0 matching"):
                return {**retry, "title": opened.get("title"),
                        "message": (opened.get("message", "") + " " + retry.get("message", "")).strip()}

    # No Windows media session ever appeared - either the app doesn't expose
    # one, or (for stop/pause on an app that was just launched here) a
    # session simply can't exist yet. Fall back to the generic on-screen
    # Play/Pause/Stop control.
    canonical = "start" if media_action == "play" else "stop"
    fallback = generic_actions.run(title, canonical)
    return {**fallback, "title": opened.get("title"),
            "message": (opened.get("message", "") + " " + fallback.get("message", "")).strip()}