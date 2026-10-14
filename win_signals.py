"""
win_signals.py
==============
Cheap, privacy-preserving activity signals for awareness.py.

Everything here is a single Win32 call or a psutil lookup - no screenshots,
no keyboard hooks, no reading of window contents. On non-Windows hosts each
function degrades to a neutral value so the rest of the app (and the tests)
keep working.

  foreground()     -> {"hwnd", "title", "pid", "exe"}
  idle_seconds()   -> seconds since the last keyboard/mouse input
  snap_side_by_side(hwnd_left, hwnd_right) -> bool
"""
from __future__ import annotations

import ctypes
import os

IS_WINDOWS = hasattr(os, "startfile")

try:
    import psutil
except Exception:          # psutil is in requirements.txt, but stay importable
    psutil = None

_exe_cache: dict[int, str] = {}


def _exe_for_pid(pid: int) -> str:
    if not pid:
        return ""
    cached = _exe_cache.get(pid)
    if cached is not None:
        return cached
    name = ""
    if psutil is not None:
        try:
            name = psutil.Process(pid).name()
        except Exception:
            name = ""
    if len(_exe_cache) > 512:
        _exe_cache.clear()
    _exe_cache[pid] = name
    return name


if IS_WINDOWS:
    _user32 = ctypes.windll.user32
    _kernel32 = ctypes.windll.kernel32
    _user32.GetForegroundWindow.restype = ctypes.c_void_p

    class _LASTINPUTINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

    class _RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT),
                    ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong)]

    def foreground() -> dict:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return {"hwnd": None, "title": "", "pid": 0, "exe": ""}
        length = _user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd))
        buf = ctypes.create_unicode_buffer(length + 1)
        _user32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, length + 1)
        pid = ctypes.c_ulong()
        _user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        return {"hwnd": hwnd, "title": buf.value or "", "pid": pid.value, "exe": _exe_for_pid(pid.value)}

    def idle_seconds() -> float:
        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not _user32.GetLastInputInfo(ctypes.byref(info)):
            return 0.0
        now = _kernel32.GetTickCount() & 0xFFFFFFFF
        return max(0.0, ((now - info.dwTime) & 0xFFFFFFFF) / 1000.0)

    def snap_side_by_side(hwnd_left, hwnd_right) -> bool:
        """Restore both windows and tile them left/right on the work area of
        the monitor that holds the left one."""
        SW_RESTORE, MONITOR_DEFAULTTONEAREST = 9, 2
        SWP_NOZORDER, SWP_SHOWWINDOW = 0x0004, 0x0040
        _user32.MonitorFromWindow.restype = ctypes.c_void_p
        monitor = _user32.MonitorFromWindow(ctypes.c_void_p(hwnd_left), MONITOR_DEFAULTTONEAREST)
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not monitor or not _user32.GetMonitorInfoW(ctypes.c_void_p(monitor), ctypes.byref(info)):
            return False
        work = info.rcWork
        width, height = work.right - work.left, work.bottom - work.top
        half = width // 2
        ok = True
        for hwnd, x in ((hwnd_left, work.left), (hwnd_right, work.left + half)):
            _user32.ShowWindow(ctypes.c_void_p(hwnd), SW_RESTORE)
            ok &= bool(_user32.SetWindowPos(ctypes.c_void_p(hwnd), None, x, work.top, half, height,
                                            SWP_NOZORDER | SWP_SHOWWINDOW))
        return ok

else:
    def foreground() -> dict:
        return {"hwnd": None, "title": "", "pid": 0, "exe": ""}

    def idle_seconds() -> float:
        return 0.0

    def snap_side_by_side(hwnd_left, hwnd_right) -> bool:
        return False


def friendly_app_name(title: str, exe: str) -> str:
    """Best human name for an app from its window title and exe.

    Most Windows apps title their window '<document> - <App>' so the last
    ' - ' segment is the app. Falls back to the exe stem."""
    known = KNOWN_APPS.get((exe or "").lower())
    if known:
        return known["name"]
    title = (title or "").strip()
    for sep in (" - ", " — ", " | "):
        if sep in title:
            tail = title.rsplit(sep, 1)[-1].strip()
            if 2 <= len(tail) <= 40:
                return tail
    stem = os.path.splitext(exe or "")[0]
    return stem or title[:40] or "Unknown"


# Apps the assistant has specific help for. 'kind' drives which contextual
# quick actions awareness.py offers while that app is in focus.
KNOWN_APPS = {
    "robloxstudiobeta.exe": {"name": "Roblox Studio", "kind": "roblox"},
    "robloxstudio.exe": {"name": "Roblox Studio", "kind": "roblox"},
    "blender.exe": {"name": "Blender", "kind": "blender"},
    "figma.exe": {"name": "Figma", "kind": "figma"},
    "code.exe": {"name": "VS Code", "kind": "code"},
    "cursor.exe": {"name": "Cursor", "kind": "code"},
    "devenv.exe": {"name": "Visual Studio", "kind": "code"},
    "pycharm64.exe": {"name": "PyCharm", "kind": "code"},
    "idea64.exe": {"name": "IntelliJ IDEA", "kind": "code"},
    "chrome.exe": {"name": "Chrome", "kind": "browser"},
    "msedge.exe": {"name": "Edge", "kind": "browser"},
    "firefox.exe": {"name": "Firefox", "kind": "browser"},
    "brave.exe": {"name": "Brave", "kind": "browser"},
    "explorer.exe": {"name": "File Explorer", "kind": "files"},
    "winword.exe": {"name": "Word", "kind": "docs"},
    "excel.exe": {"name": "Excel", "kind": "docs"},
    "discord.exe": {"name": "Discord", "kind": "chat"},
    "tidal.exe": {"name": "TIDAL", "kind": "media"},
    "spotify.exe": {"name": "Spotify", "kind": "media"},
    "wiztree64.exe": {"name": "WizTree", "kind": "files"},
    "wiztree.exe": {"name": "WizTree", "kind": "files"},
}


def app_kind(exe: str) -> str:
    return (KNOWN_APPS.get((exe or "").lower()) or {}).get("kind", "")
