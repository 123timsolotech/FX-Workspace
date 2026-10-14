"""
generic_actions.py
===================
ONE deterministic, app-agnostic layer for simple one-shot semantic desktop
commands - connect, disconnect, start/play/resume/run, stop/pause/halt, and
toggle - against ANY named app's window.

This replaces the old per-app deterministic handlers that used to live in
desktop_router.py (Proton VPN's CONNECT_LABELS/DISCONNECT_LABELS and
_connection_state's "not connected"/"unprotected" phrase matching, TIDAL's
_playback_controls regex). Nothing here knows what "Proton" or "TIDAL" is;
it only knows a small, reusable vocabulary of English verbs that this class
of control tends to use, and it drives/verifies through elements.py's
existing native-pattern automation - no new UI walking, no new click APIs.

Design
------
1. List the window's live controls: elements.list_elements(). No new
   tree-walking code.
2. Score every enabled, named control's label against a small keyword set
   for the requested action ("connect"/"disconnect", "play"/"pause", ...).
   This is the same technique desktop_router.py used to hand-encode once
   per app, just parametrized so it works for any app's control names.
3. Act on the single best, unambiguous candidate through
   elements.act_element() - no bespoke click/invoke code, and the same
   focus-before-click / relocate-if-stale handling list_elements/act_element
   already do stays in effect for free.
4. Verify success two ways, both generic - never by scanning the window's
   text for one app's specific phrases ("not connected", "unprotected"):
     a. PRIMARY - trust elements.act_element()'s own before/after diff of
        the control's native automation-pattern state (toggle_state, value,
        selected). This is exact and needs no app knowledge at all: it is
        the literal state the accessibility API reports.
     b. FALLBACK - only when (a) is inconclusive, i.e. an Invoke-pattern
        button with no native state of its own that just causes the app to
        re-render a different control (this is how Electron/webview apps
        like Proton VPN behave: clicking "Connect" doesn't toggle anything
        on the button itself, it swaps the whole button out for
        "Disconnect"). Poll list_elements and check whether a control now
        matches the semantically OPPOSITE keyword set and is enabled. This
        is still a plain keyword lookup against a small, reusable verb set
        - never a hardcoded app phrase list.
"""
from __future__ import annotations

import re
import time

import desktop_input as di
import elements

# ---------------------------------------------------------------------------
# Semantic action vocabulary
# ---------------------------------------------------------------------------
# Each canonical action has:
#   "exact"   - labels that count as a match only on exact (normalized)
#               equality, ordered most-specific first.
#   "prefix"  - labels that count as a match when the (normalized) label
#               STARTS WITH one of these. Used only where a bare prefix is
#               unambiguous enough to be safe (e.g. "connect" as a whole
#               leading word); deliberately left empty for start/stop so a
#               button like "Play next track" is never mistaken for "Play".
#   "opposite"- the canonical action whose own control being visible AND
#               enabled is treated as proof the requested action's target
#               state has already been reached (e.g. an enabled "Disconnect"
#               control is proof a "connect" request is already satisfied).
ACTION_SPECS: dict[str, dict] = {
    "connect": {
        "exact": ("connect", "quick connect", "connect now",
                  "connect to fastest server", "connect to fastest", "turn on"),
        "prefix": ("connect",),
        "opposite": "disconnect",
    },
    "disconnect": {
        "exact": ("disconnect", "disconnect now", "turn off"),
        "prefix": ("disconnect from",),
        "opposite": "connect",
    },
    "start": {
        "exact": ("play", "start", "resume", "run"),
        "prefix": (),
        "opposite": "stop",
    },
    "stop": {
        "exact": ("pause", "stop", "halt"),
        "prefix": (),
        "opposite": "start",
    },
}

# What a control looks like when the pair's target state has already been
# reached, keyed by the canonical action whose control is currently visible.
# ("connect" control visible => nothing has connected yet => "disconnected".
#  "disconnect" control visible => something IS connected => "connected".)
_STATE_LABELS = {
    "connect": "disconnected",
    "disconnect": "connected",
    "start": "stopped",
    "stop": "running",
}

# User-facing verbs/aliases that map onto the four canonical actions above.
ACTION_ALIASES = {
    "connect": "connect",
    "disconnect": "disconnect",
    "play": "start", "start": "start", "resume": "start", "run": "start",
    "enable": "start", "turn_on": "start", "turn-on": "start",
    "pause": "stop", "stop": "stop", "halt": "stop",
    "disable": "stop", "turn_off": "stop", "turn-off": "stop",
}

# Words that, if present anywhere in an otherwise-matching label, disqualify
# it. These keep a settings/help checkbox that happens to contain "connect"
# from being chosen over the real action control. Generic across apps -
# not tied to any one product's wording.
_EXCLUDE_WORDS = ("automatically", "setting", "help", "preferences",
                   "options", "about", "learn more", "info")

# Strip a trailing keyboard-shortcut hint like "(Space)"/"(Spacebar)" before
# comparing labels - common on transport buttons, not app-specific.
_SHORTCUT_SUFFIX_RE = re.compile(r"\s*\((?:space|spacebar|enter|return)\)\s*$", re.I)


def _normalize(label: str) -> str:
    label = _SHORTCUT_SUFFIX_RE.sub("", (label or "").strip())
    return " ".join(label.split()).casefold()


def _canonical(action: str) -> str | None:
    return ACTION_ALIASES.get((action or "").strip().lower())


def _named(entries: list[dict], require_enabled: bool) -> list[dict]:
    return [e for e in entries
            if (e.get("enabled", True) or not require_enabled) and (e.get("name") or "").strip()]


def _choose(entries: list[dict], spec: dict, require_enabled: bool = True):
    """Pick the single best control matching a canonical action's spec, or
    None. Exact matches are preferred over prefix matches; within a tier the
    first matching entry wins (mirrors the old per-app
    _choose_connect/_choose_disconnect selection order, generalized).

    require_enabled=True (the default) is used for "is this action already
    satisfied" evidence checks, where only a live, clickable control counts.
    require_enabled=False is used when picking the actual click target, so a
    DISABLED match can still be found and reported with a clear "exists but
    disabled" message instead of a misleading "no control found" one - the
    same UX elements.act_by_name already gives for a plain name match."""
    candidates = _named(entries, require_enabled)
    for wanted in spec["exact"]:
        for entry in candidates:
            label = _normalize(entry["name"])
            if label == wanted and not any(x in label for x in _EXCLUDE_WORDS):
                return entry
    for prefix in spec["prefix"]:
        for entry in candidates:
            label = _normalize(entry["name"])
            if label.startswith(prefix) and not any(x in label for x in _EXCLUDE_WORDS):
                return entry
    return None


def run(window_title: str, action: str, *, poll_attempts: int = 20, poll_interval: float = 0.6) -> dict:
    """Perform one deterministic, one-shot semantic action - connect,
    disconnect, start/play/resume/run, or stop/pause/halt - against ANY
    named app's already-open window. No per-app code: reuses
    elements.list_elements/act_element and a small, reusable action-keyword
    set to find and verify the target control.
    """
    canonical = _canonical(action)
    if not canonical:
        return {"ok": False, "verified": False,
                "message": f"Unsupported semantic action '{action}'. Use connect/disconnect/start/play/stop/pause."}
    spec = ACTION_SPECS[canonical]
    opposite_spec = ACTION_SPECS[spec["opposite"]]

    listing = elements.list_elements(window_title)
    if not listing.get("ok"):
        return {"ok": False, "verified": False, "message": listing.get("message", ""),
                "fallback": listing.get("fallback")}
    entries = listing.get("entries") or []
    if not entries:
        return {"ok": False, "verified": False, "needs_agent": True,
                "fallback": listing.get("fallback"),
                "message": listing.get("message") or f"'{window_title}' exposed no accessible controls."}

    # Already in the requested state? An enabled control matching the
    # OPPOSITE action is generic, positive evidence (e.g. an enabled
    # "Disconnect" proves a "connect" request has nothing left to do).
    already = _choose(entries, opposite_spec)
    if already:
        return {"ok": True, "verified": True,
                "message": f"'{window_title}' is already {_STATE_LABELS[spec['opposite']]} "
                            f"(found enabled '{already['name']}'); nothing was clicked."}

    target = _choose(entries, spec, require_enabled=False)
    if not target:
        visible = [e["name"] for e in entries if e.get("name")][:30]
        return {"ok": False, "verified": False, "needs_agent": True,
                "message": (f"No safe '{canonical}' control was found in '{window_title}'. "
                            "Visible controls: " + ", ".join(visible))}
    if not target.get("enabled", True):
        return {"ok": False, "verified": False,
                "message": f"'{target['name']}' exists but is DISABLED in '{window_title}'."}

    action_verb = "toggle" if target.get("toggle_state") is not None else "click"
    acted = elements.act_element(target["id"], action_verb)
    if not acted.get("ok"):
        return {**acted, "verified": False}

    # (a) PRIMARY verification: trust act_element's own before/after diff of
    # the control's native pattern state whenever it reports a CONCRETE
    # field change - that is real automation state, not a text guess.
    acted_message = acted.get("message", "")
    concrete_change = bool(acted.get("verified")) and "no state field changed" not in acted_message \
        and "no longer exists" not in acted_message
    if concrete_change:
        return {"ok": True, "verified": True,
                "message": f"Activated '{target['name']}' in '{window_title}' and verified a native state change: {acted_message}"}

    # (b) FALLBACK verification: the click was an Invoke-pattern button with
    # no native state of its own (common for Electron/webview apps) that
    # simply causes the app to re-render a different control. Poll for the
    # OPPOSITE control to appear/enable - same generic keyword lookup as the
    # pre-check above, just checked again after the click.
    closed_polls = 0
    last_entries = entries
    for _ in range(poll_attempts):
        time.sleep(poll_interval)
        polled = elements.list_elements(window_title)
        last_entries = polled.get("entries") or []
        if _choose(last_entries, opposite_spec):
            return {"ok": True, "verified": True,
                    "message": (f"Activated '{target['name']}' in '{window_title}' and verified through its "
                                f"live controls that it is now {_STATE_LABELS[spec['opposite']]}.")}
        if last_entries or di.find_window(window_title):
            closed_polls = 0
        else:
            closed_polls += 1
            # Require two consecutive misses before treating the window as
            # closed, same debounce desktop_router.py used to hand-roll for
            # Proton specifically - now generic to any app's window.
            if closed_polls >= 2:
                return {"ok": False, "verified": False, "interrupted": True,
                        "message": (f"'{window_title}' closed before the '{canonical}' action could be "
                                    "verified. No retry was sent.")}
    return {"ok": False, "verified": False, "needs_agent": True,
            "message": (f"Clicked '{target['name']}' in '{window_title}', but no verified state change was "
                        f"observed within {poll_attempts * poll_interval:.0f}s.")}


def status(window_title: str, action: str = "connect") -> dict:
    """Read-only: report which side of an on/off action pair (e.g.
    connect/disconnect) the app is currently in, purely from its live
    controls - no clicks, no app-specific phrase matching."""
    canonical = _canonical(action)
    if not canonical:
        return {"ok": False, "verified": False, "message": f"Unsupported semantic action '{action}'."}
    spec = ACTION_SPECS[canonical]
    opposite_spec = ACTION_SPECS[spec["opposite"]]

    listing = elements.list_elements(window_title)
    entries = listing.get("entries") or []
    if not entries:
        return {"ok": False, "verified": False,
                "message": listing.get("message") or f"'{window_title}' exposed no accessible controls."}
    if _choose(entries, opposite_spec):
        return {"ok": True, "verified": True,
                "message": f"'{window_title}' is {_STATE_LABELS[spec['opposite']]}, verified through its live controls."}
    if _choose(entries, spec):
        return {"ok": True, "verified": True,
                "message": f"'{window_title}' is {_STATE_LABELS[canonical]}, verified through its live controls."}
    return {"ok": False, "verified": False,
            "message": (f"'{window_title}' did not expose an unambiguous '{canonical}'/'{spec['opposite']}' "
                        "state control, so I did not guess.")}


def toggle(window_title: str, name_hint: str | None = None) -> dict:
    """Flip a single togglable control (CheckBox/ToggleButton/similar) in
    ANY app's window. Unlike connect/disconnect/start/stop, a toggle has no
    fixed target direction, so it is verified purely through its own native
    toggle_state before/after diff - the Toggle automation pattern reports
    that directly, so no keyword-based state guess is needed at all once the
    control itself is identified."""
    listing = elements.list_elements(window_title)
    entries = listing.get("entries") or []
    if not entries:
        return {"ok": False, "verified": False,
                "message": listing.get("message") or f"'{window_title}' exposed no accessible controls."}

    candidates = [e for e in entries if e.get("enabled", True) and e.get("toggle_state") is not None]
    if name_hint:
        needle = name_hint.strip().casefold()
        narrowed = [e for e in candidates if needle in (e.get("name") or "").casefold()]
        if narrowed:
            candidates = narrowed

    if not candidates:
        return {"ok": False, "verified": False,
                "message": f"No enabled toggle-pattern control was found in '{window_title}'."}
    if len(candidates) > 1:
        names = ", ".join(f"'{c['name']}'" for c in candidates[:10])
        return {"ok": False, "verified": False,
                "message": (f"Found {len(candidates)} possible toggles in '{window_title}' ({names}); "
                            "pass a more specific name_hint.")}

    target = candidates[0]
    result = elements.act_element(target["id"], "toggle")
    if not result.get("ok"):
        return {**result, "verified": False}
    # Trust act_element's own before/after diff of the SAME control's native
    # toggle_state directly - it already read the real state twice, so this
    # needs no keyword guessing at all. Only reject the two inconclusive
    # cases act_element itself flags (no field actually changed, or the
    # control disappeared instead of flipping).
    message = result.get("message", "")
    verified = bool(result.get("verified")) and "no state field changed" not in message \
        and "no longer exists" not in message
    return {"ok": True, "verified": verified,
            "message": f"Toggled '{target['name']}' in '{window_title}': {message}"}
