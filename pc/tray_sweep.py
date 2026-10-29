"""
tray_sweep.py
=============
Clears leftover tray icons (1.4.4). When a program is ended by force - an installer closing it, Task Manager,
a crash - Windows keeps its notification-area icon until the mouse passes over it; then it vanishes. The owner saw
dozens of "FX Workspace" icons in the tray's overflow that "closed randomly" as they looked: copies that had been
force-closed, not servers still running (only one was).

A clean quit removes its own icon. This sweep removes the others: it moves an invisible "mouse" over every icon
of the notification area and its overflow (WM_MOUSEMOVE to the toolbar windows that hold them), the way the
pointer would - Windows then drops each icon whose program is gone and leaves the live ones alone. Nothing is
clicked and nothing else changes. The launcher runs it when the app starts; the installer after it closes the app.

Windows 10's tray (Shell_TrayWnd / NotifyIconOverflowWindow). Windows 11's newer tray isn't made of these windows -
there it finds nothing and does nothing.
"""
from __future__ import annotations

import ctypes
import sys

WM_MOUSEMOVE = 0x0200
STEP = 8                                   # px between two "pointer" positions (icons are 16-24 px)
CHAINS = (("Shell_TrayWnd", "TrayNotifyWnd", "SysPager", "ToolbarWindow32"),       # the icons next to the clock
          ("NotifyIconOverflowWindow", "ToolbarWindow32"))                         # the ^ overflow


def _find(chain, user32) -> int:
    hwnd = user32.FindWindowW(chain[0], None)
    for cls in chain[1:]:
        if not hwnd:
            return 0
        hwnd = user32.FindWindowExW(hwnd, None, cls, None)
    return hwnd or 0


def sweep() -> int:
    """Pass over every icon of the tray. Returns how many tray windows it swept (0 when there are none)."""
    if sys.platform != "win32":
        return 0
    try:
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        user32.FindWindowW.restype = wintypes.HWND
        user32.FindWindowExW.restype = wintypes.HWND
        user32.FindWindowExW.argtypes = [wintypes.HWND, wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR]
        swept = 0
        for chain in CHAINS:
            hwnd = _find(chain, user32)
            if not hwnd:
                continue
            rect = wintypes.RECT()
            if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
                continue
            for y in range(0, max(1, rect.bottom), STEP):
                for x in range(0, max(1, rect.right), STEP):
                    user32.SendMessageW(hwnd, WM_MOUSEMOVE, 0, (y << 16) | (x & 0xFFFF))
            swept += 1
        return swept
    except Exception:
        return 0                            # never worth failing a start over


if __name__ == "__main__":
    print(sweep())
