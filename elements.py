"""
elements.py
===========
THE NEW METHODOLOGY, part 1: drive apps through the accessibility tree
using native automation PATTERNS (Invoke/Toggle/Selection/Value/Expand),
not through coordinates derived from an element's bounding rectangle.

Why the old approach ("find element -> read its rectangle -> click the
center pixel") was fragile:
  - It re-inherits every DPI/scaling/occlusion problem pixel-clicking has,
    just with extra steps to get there.
  - It re-searches for the element by title/best_match on every single
    call, which is slow and breaks on duplicate/blank/localized names.
  - It only ever used the "uia" backend, which many legacy Win32/MFC/
    WinForms apps expose poorly or not at all - "win32" backend often
    works where "uia" returns nothing.
  - A click either "worked" or it silently didn't; there was no
    deterministic way to confirm state actually changed.

The fix, implemented here:
  1. list_elements() walks the FULL descendant tree ONCE, trying the
     "uia" backend and falling back to "win32" automatically if "uia"
     comes back thin. Every actionable element found is cached as a live
     wrapper object under a short handle id (e1, e2, ...).
  2. act_element() looks the handle up directly - no re-search, no name
     ambiguity - and drives it with the MOST NATIVE pattern available for
     its control type (invoke/toggle/select/set text/expand/collapse).
     Only if no pattern applies does it fall back to wrapper.click_input(),
     which pywinauto computes fresh against the control's LIVE rectangle
     at call time (not a stale value we captured earlier).
  3. Every action reports a concrete before/after STATE (toggle state,
     text value, enabled/selected flag) instead of a pixel diff, so the
     agent gets a definitive yes/no on whether it worked.
"""
import time
import threading
import re

from desktop_input import IS_WINDOWS, find_window, focus_window

try:
    from pywinauto import Desktop
    PYWINAUTO_AVAILABLE = True
except ImportError:
    Desktop = None
    PYWINAUTO_AVAILABLE = False

try:
    import comtypes
    _COMTYPES_AVAILABLE = True
except ImportError:
    comtypes = None
    _COMTYPES_AVAILABLE = False

# The 'uia' backend talks to Windows over COM, and COM requires
# CoInitialize on EVERY thread that touches it. FastAPI's request
# threadpool can hand this call to a different OS thread each time, so
# without this guard, COM can end up used on a thread that never
# initialized it - a real source of intermittent native failures, not
# just a Python exception. threading.local() makes this a once-per-thread,
# idempotent call.
_com_state = threading.local()


def _ensure_com_initialized():
    if getattr(_com_state, "done", False) or not _COMTYPES_AVAILABLE:
        return
    try:
        comtypes.CoInitialize()
    except OSError:
        pass  # already initialized on this thread in a compatible mode
    _com_state.done = True

ACTIONABLE_TYPES = {
    "Button", "CheckBox", "RadioButton", "MenuItem", "Hyperlink", "TabItem",
    "Edit", "Document", "ComboBox", "ListItem", "TreeItem", "Slider",
    "SplitButton", "MenuBar", "Menu",
}

# handle_id -> {"wrapper", "window_hwnd", "created", "control_type", "name", "backend"}
_REGISTRY: dict[str, dict] = {}
_COUNTER = {"n": 0}
_HANDLE_TTL = 300  # seconds; a handle older than this must be re-listed
_MAX_HANDLES = 600


def _prune():
    if len(_REGISTRY) <= _MAX_HANDLES:
        return
    now = time.time()
    stale = [k for k, v in _REGISTRY.items() if now - v["created"] > _HANDLE_TTL]
    for k in stale:
        _REGISTRY.pop(k, None)


def _new_handle() -> str:
    _COUNTER["n"] += 1
    return f"e{_COUNTER['n']}"


def _safe(fn, default=None):
    try:
        return fn()
    except Exception:
        return default


def _describe(wrapper):
    ctype = _safe(lambda: wrapper.element_info.control_type, "Unknown")
    name = (_safe(lambda: wrapper.window_text(), "") or "").strip()
    automation_id = _safe(lambda: wrapper.element_info.automation_id, "") or ""
    enabled = _safe(lambda: wrapper.is_enabled(), True)
    value = None
    try:
        value = wrapper.get_value()
    except Exception:
        if ctype == "ComboBox":
            value = _safe(lambda: wrapper.selected_text())
    toggle_state = _safe(lambda: wrapper.get_toggle_state())  # 0/1/2 or None
    selected = _safe(lambda: wrapper.is_selected())
    return {
        "control_type": ctype, "name": name, "automation_id": automation_id,
        "enabled": bool(enabled), "value": value, "toggle_state": toggle_state,
        "selected": selected,
    }


def _walk(window_wrapper, backend, max_items):
    results = []
    for ctrl in window_wrapper.descendants():
        try:
            if not ctrl.is_visible():
                continue
            info = _describe(ctrl)
            has_name = bool(info["name"])
            if not has_name and info["control_type"] not in ACTIONABLE_TYPES:
                continue
            results.append((ctrl, info, backend))
        except Exception:
            continue
        if len(results) >= max_items:
            break
    return results


def list_elements(window_title: str, max_items: int = 80) -> dict:
    """Enumerate the actionable controls of a window and cache live handles
    for act_element(). Tries 'uia' first, falls back to 'win32' if 'uia'
    is thin (common for older Win32/MFC/WinForms apps)."""
    if not IS_WINDOWS:
        return {"ok": False, "message": "Windows only."}
    if not PYWINAUTO_AVAILABLE:
        return {"ok": False, "fallback": "screenshot",
                "message": ("pywinauto is not installed, so element control is unavailable. "
                             "Run: pip install pywinauto pywin32 comtypes  -- then restart the server. "
                             "Until then, use inspect_screen + click_at.")}

    match = find_window(window_title)
    if not match:
        return {"ok": False, "message": f"No window matching '{window_title}' is currently open."}
    hwnd, real_title = match
    # Read-only: UI Automation reads a background window's controls without
    # focusing it. Focusing used to un-maximize the app and steal the user's
    # foreground window just to LOOK at it. A minimized window is shown
    # without activation for the walk and minimized again afterwards.
    import window_state

    _ensure_com_initialized()
    attempts = []
    with window_state.temporarily_visible(hwnd):
        for backend in ("uia", "win32"):
            try:
                win = Desktop(backend=backend).window(handle=hwnd)
                found = _walk(win, backend, max_items)
                attempts.append((backend, found))
                if len(found) >= 3:
                    break  # good enough, no need to also try the other backend
            except Exception:
                attempts.append((backend, []))

    backend_used, found = max(attempts, key=lambda a: len(a[1])) if attempts else (None, [])

    if not found:
        hint = (
            "No actionable elements were exposed on either backend. Common causes: the target app is "
            "running ELEVATED (as Administrator) while this server is not - Windows blocks cross-privilege "
            "automation (UIPI), so run this server as Administrator too; the app renders its own UI "
            "(games, canvases, some Electron/webview apps) with no accessibility tree at all - use "
            "inspect_screen + click_at instead; or the window has no controls yet (still loading)."
        )
        return {"ok": True, "fallback": "screenshot", "message": f"'{real_title}': {hint}"}

    _prune()
    lines = []
    entries = []
    for ctrl, info, backend in found:
        handle_id = _new_handle()
        _REGISTRY[handle_id] = {
            "wrapper": ctrl, "window_hwnd": hwnd, "created": time.time(),
            "control_type": info["control_type"], "name": info["name"], "backend": backend,
        }
        entries.append({"id": handle_id, "control_type": info["control_type"],
                        "name": info["name"], "automation_id": info["automation_id"],
                        "enabled": info["enabled"], "value": info["value"],
                        "selected": info["selected"], "toggle_state": info["toggle_state"]})
        bits = [f"[{handle_id}] {info['control_type']} '{info['name']}'"]
        if info["automation_id"]:
            bits.append(f"id={info['automation_id']}")
        if not info["enabled"]:
            bits.append("DISABLED")
        if info["toggle_state"] is not None:
            bits.append(f"toggled={'on' if info['toggle_state'] == 1 else 'off' if info['toggle_state'] == 0 else 'indeterminate'}")
        if info["selected"] is not None:
            bits.append(f"selected={info['selected']}")
        if info["value"]:
            v = str(info["value"])
            bits.append(f"value='{v[:60]}{'...' if len(v) > 60 else ''}'")
        lines.append(" ".join(bits))

    log_note = f"(via {backend_used} backend)"
    return {"ok": True, "entries": entries,
            "message": f"Elements in '{real_title}' {log_note}:\n" + "\n".join(lines)}


def act_by_name(window_title: str, name: str, action: str = "click", text: str = None) -> dict:
    """List a window's controls and act on the one whose visible name matches,
    in a SINGLE call.

    This exists because handle ids are a bad fit for small local models. The
    list_elements -> remember 'e17' -> act_element('e17') pattern requires
    carrying an opaque token across two turns and an approval popup, and a 4B
    model very often invents a handle, reuses a stale one from earlier in the
    conversation, or re-lists and then acts on the id from the PREVIOUS listing.
    Matching on the name the model can actually see removes that whole class of
    failure, and halves the number of turns a click costs.
    """
    listing = list_elements(window_title)
    if not listing.get("ok") or not listing.get("entries"):
        return listing if not listing.get("ok") else {
            "ok": False, "fallback": listing.get("fallback"), "message": listing.get("message", "")}

    entries = listing["entries"]
    needle = (name or "").strip().lower()
    if not needle:
        return {"ok": False, "message": "act_by_name requires a control name."}

    def rank(entry):
        label = (entry["name"] or "").strip().lower()
        if not label:
            return 99
        if label == needle:
            return 0
        if label.replace("&", "") == needle:      # Win32 accelerator markers
            return 1
        if label.startswith(needle):
            return 2
        if needle in label:
            return 3
        return 99

    scored = sorted(((rank(e), e) for e in entries), key=lambda p: p[0])
    best_rank, best = scored[0]
    if best_rank == 99:
        import difflib
        names = [e["name"] for e in entries if e["name"]]
        close = difflib.get_close_matches(name, names, n=5, cutoff=0.6)
        if not close:
            visible = ", ".join(f"'{n}'" for n in names[:25]) or "(none named)"
            return {"ok": False, "message": (
                f"No control named '{name}' in '{window_title}'. Available: {visible}")}
        best = next(e for e in entries if e["name"] == close[0])

    # A disabled control is a real answer, not a failure to find one.
    if not best.get("enabled", True):
        return {"ok": False, "message": (
            f"'{best['name']}' ({best['control_type']}) exists but is DISABLED - something else has to happen "
            "first (often: select an item in the list before the action button becomes available).")}

    result = act_element(best["id"], action, text)
    result["message"] = f"Matched '{best['name']}' ({best['control_type']}). " + result.get("message", "")
    return result


def _relocate(entry) -> object:
    """Find a fresh, live element matching this entry's control_type/name
    under the same window. Needed because Electron/Chromium apps (e.g. the
    ProtonVPN desktop client) regenerate their accessibility tree on
    re-render - the visible button/list item you saw in list_elements is
    very often backed by a BRAND NEW UIA element by the time an action
    actually runs (the approval wait alone is enough time for this), even
    though nothing visibly changed. This is not the 'stale name/ambiguous
    match' problem re-search used to have - it's a fresh, unambiguous
    lookup by the exact identity list_elements already gave us."""
    hwnd = entry["window_hwnd"]
    backend = entry.get("backend", "uia")
    try:
        win = Desktop(backend=backend).window(handle=hwnd)
        for ctrl in win.descendants():
            try:
                if not ctrl.is_visible():
                    continue
                ctype = _safe(lambda: ctrl.element_info.control_type)
                name = (_safe(lambda: ctrl.window_text(), "") or "").strip()
                if ctype == entry["control_type"] and name == entry["name"]:
                    return ctrl
            except Exception:
                continue
    except Exception:
        pass
    return None


def act_element(element_id: str, action: str, text: str = None) -> dict:
    """Drive a cached element directly via its handle - no re-search on the
    happy path. Uses the most native pattern for the action; falls back to
    click_input() (pywinauto's own live-rectangle click) only if no pattern
    applies. If the cached wrapper has gone stale, self-heals by relocating
    a live element with the same identity before giving up (see _relocate)."""
    if not IS_WINDOWS:
        return {"ok": False, "message": "Windows only."}
    entry = _REGISTRY.get(element_id)
    if not entry:
        return {"ok": False, "message": f"Unknown or expired element handle '{element_id}'. Call list_elements again."}
    if time.time() - entry["created"] > _HANDLE_TTL:
        _REGISTRY.pop(element_id, None)
        return {"ok": False, "message": f"Handle '{element_id}' expired. Call list_elements again to get fresh handles."}

    wrapper = entry["wrapper"]
    label = f"{entry['control_type']} '{entry['name']}'"
    healed = False

    if not _safe(lambda: wrapper.exists(), False):
        fresh = _relocate(entry)
        if fresh is None:
            return {"ok": False, "message": f"{label} no longer exists on screen and a same-name relocate found nothing. Call list_elements again - the app's UI may have changed."}
        wrapper = entry["wrapper"] = fresh
        healed = True

    # The approval popup (topmost, focus_force()) always sits between
    # list_elements (which focused this window) and this call, since
    # act_element is a mutating tool. By the time the user approves, the
    # target window is very often no longer focused/foreground, and many
    # apps' UIA providers only actually respond to invoke()/click_input()
    # when their window is truly active - the call still reports success
    # with no exception, it just does nothing visible. Re-focus first.
    focus_window(entry["window_hwnd"])
    time.sleep(0.1)

    before = _describe(wrapper)
    action = (action or "click").lower()

    try:
        if action in ("click", "invoke"):
            try:
                wrapper.invoke()
                method = "invoke"
            except Exception:
                wrapper.click_input()
                method = "click_input"
        elif action in ("click_physical", "physical_click"):
            # Some Electron/UIA providers accept InvokePattern without
            # dispatching the app's real pointer handler. The caller may use
            # this explicit fallback after observing that invoke caused no
            # state change.
            wrapper.click_input()
            method = "click_input(forced)"
        elif action == "toggle":
            try:
                wrapper.toggle()
                method = "toggle"
            except Exception:
                wrapper.click_input()
                method = "click_input"
        elif action == "select":
            # A bare click only opens a ComboBox; it never proves that a
            # particular option was selected and invalidates many UIA
            # providers' cached wrapper. Report a native selection failure so
            # the caller can re-enumerate the live control and try a verified
            # exact item instead of silently clicking the old selector again.
            wrapper.select(text) if text is not None else wrapper.select()
            method = "select"
        elif action == "select_drive":
            if not text:
                return {"ok": False, "message": "action='select_drive' requires a drive such as 'G:'."}
            wanted = str(text).strip().upper().rstrip("\\/")
            labels = _safe(lambda: wrapper.item_texts(), []) or []
            matches = [str(label) for label in labels
                       if re.search(rf"(?<![a-z0-9]){re.escape(wanted)}(?:[\\/]|\]|\s|$)",
                                    str(label), re.I)]
            # WizTree labels options as values such as "[G:] System". Use
            # that literal live option when available; falling back to G: is
            # reserved for combo providers that do not expose item_texts().
            choice = matches[0] if len(matches) == 1 else wanted
            wrapper.select(choice)
            method = f"select_drive({choice})"
        elif action in ("type", "set_text"):
            if text is None:
                return {"ok": False, "message": "action='type' requires 'text'."}
            try:
                wrapper.set_edit_text(text)
                method = "set_edit_text"
            except Exception:
                wrapper.click_input()
                try:
                    wrapper.type_keys("^a{DEL}", pause=0.02)
                except Exception:
                    pass
                from desktop_input import send_unicode_text
                send_unicode_text(text)
                method = "click_input+type"
        elif action == "expand":
            wrapper.expand()
            method = "expand"
        elif action == "collapse":
            wrapper.collapse()
            method = "collapse"
        elif action == "right_click":
            wrapper.click_input(button="right")
            method = "click_input(right)"
        else:
            return {"ok": False, "message": f"Unknown action '{action}'. Use click/click_physical/toggle/select/select_drive/type/expand/collapse/right_click."}
    except Exception as e:
        return {"ok": False, "message": f"Action '{action}' failed on {label}: {e}"}

    time.sleep(0.15)
    after = _describe(wrapper) if _safe(lambda: wrapper.exists(), False) else {"note": "element no longer exists (likely closed/navigated away, which can itself mean success)"}

    changed_bits = []
    if before.get("toggle_state") != after.get("toggle_state"):
        changed_bits.append(f"toggle {before.get('toggle_state')} -> {after.get('toggle_state')}")
    if before.get("value") != after.get("value"):
        changed_bits.append(f"value '{before.get('value')}' -> '{after.get('value')}'")
    if before.get("selected") != after.get("selected"):
        changed_bits.append(f"selected {before.get('selected')} -> {after.get('selected')}")

    verified = bool(changed_bits) or "note" in after
    detail = "; ".join(changed_bits) if changed_bits else (after.get("note") or "no state field changed (fine for buttons that just navigate/open something - check the window afterward)")
    heal_note = " [relocated - cached handle was stale]" if healed else ""
    return {"ok": True, "message": f"{method} on {label}{heal_note}: {detail}", "verified": verified}
