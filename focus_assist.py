"""Is Windows holding back notifications? (1.4.3, Settings › Appearance › Notifications)

Focus assist (Windows 10) and Do not disturb (Windows 11) send an app's notifications straight to the Action
Center / notification centre, with no pop-up - so "done" notifications seem not to work. This reads its state
so the app can say so and open the right Windows page.

The state lives in a Windows Notification Facility value, WNF_SHEL_QUIETHOURS_ACTIVE_PROFILE_CHANGED: 0 off,
1 priority only, 2 alarms only (Windows 11's Do not disturb reads as 2). It is not a documented API, so any
failure reads as "unknown" and the app shows nothing. SHQueryUserNotificationState (documented) adds the
cases Windows decides by itself: a full-screen game or a presentation."""
import ctypes
import os
import sys

_WNF_QUIET_HOURS = 0x0D83063EA3BF1C75          # WNF_SHEL_QUIETHOURS_ACTIVE_PROFILE_CHANGED
_PROFILES = {0: "off", 1: "priority", 2: "alarms"}
# SHQueryUserNotificationState: 3 a Direct3D app full screen, 4 presentation mode, 6 quiet time after setup.
_QUNS = {2: "busy", 3: "fullscreen", 4: "presentation", 6: "quiet"}
SETTINGS_URI = "ms-settings:quiethours"


def _profile():
    """0 / 1 / 2, or None when it can't be read."""
    if sys.platform != "win32":
        return None
    try:
        ntdll = ctypes.WinDLL("ntdll")
        state = ctypes.c_ulonglong(_WNF_QUIET_HOURS)
        stamp = ctypes.c_ulong(0)
        value = ctypes.c_ulong(0)
        size = ctypes.c_ulong(ctypes.sizeof(value))
        status = ntdll.NtQueryWnfStateData(ctypes.byref(state), None, None, ctypes.byref(stamp), ctypes.byref(value), ctypes.byref(size))
        if status != 0 or size.value < 1:
            return None
        return int(value.value)
    except Exception:
        return None


def _quns():
    if sys.platform != "win32":
        return None
    try:
        out = ctypes.c_int(0)
        if ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(out)) != 0:
            return None
        return int(out.value)
    except Exception:
        return None


def state() -> dict:
    """{"quiet": bool, "why": "priority" | "alarms" | "fullscreen" | "presentation" | "busy" | "quiet" | "",
    "known": bool} - quiet means pop-ups are held back right now."""
    profile, quns = _profile(), _quns()
    if profile in (1, 2):
        return {"quiet": True, "why": _PROFILES[profile], "known": True}
    if quns in _QUNS:
        return {"quiet": True, "why": _QUNS[quns], "known": True}
    return {"quiet": False, "why": "", "known": profile is not None or quns is not None}


def open_settings() -> bool:
    """Open Windows' Focus assist / Do not disturb page."""
    if sys.platform != "win32":
        return False
    try:
        os.startfile(SETTINGS_URI)
        return True
    except OSError:
        return False
