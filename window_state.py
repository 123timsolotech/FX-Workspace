"""
window_state.py
===============
Keeps inspection observational. Looking at an app must not change it.

The old inspection path called ShowWindow(hwnd, SW_RESTORE) on every window
it focused. On a maximized window SW_RESTORE means "un-maximize", so simply
inspecting an app knocked it out of maximized (and fullscreen apps out of
fullscreen), and left it in front of whatever the user was doing.

  preserve_windows(target)  context manager: captures the target's placement
                            (normal / maximized / minimized + rectangle) and
                            the foreground window, and restores both on exit -
                            including when the inspection raises or is
                            cancelled.
  bring_forward(target)     makes a window visible for a screenshot WITHOUT
                            changing its size: only a minimized window is
                            restored; a maximized one stays maximized.

The Win32 calls go through a small backend object so tests can drive a fake
window manager (see test_orchestration.InspectionTests).
"""
from __future__ import annotations

import ctypes
import sys
from contextlib import contextmanager

SW_SHOWMINIMIZED = 2
SW_SHOWMAXIMIZED = 3
SW_SHOWNOACTIVATE = 4
SW_RESTORE = 9
SW_SHOWMINNOACTIVE = 7


class Win32Backend:
    """Thin ctypes wrapper. Every method is safe to call off-Windows (no-op)."""

    def __init__(self):
        self.ok = sys.platform == "win32"
        if self.ok:
            from ctypes import wintypes
            self.user32 = ctypes.windll.user32

            class WINDOWPLACEMENT(ctypes.Structure):
                _fields_ = [("length", wintypes.UINT), ("flags", wintypes.UINT), ("showCmd", wintypes.UINT),
                            ("ptMinPosition", wintypes.POINT), ("ptMaxPosition", wintypes.POINT),
                            ("rcNormalPosition", wintypes.RECT)]
            self.WINDOWPLACEMENT = WINDOWPLACEMENT

    def is_minimized(self, hwnd) -> bool:
        return bool(self.ok and self.user32.IsIconic(hwnd))

    def is_maximized(self, hwnd) -> bool:
        return bool(self.ok and self.user32.IsZoomed(hwnd))

    def get_placement(self, hwnd):
        if not self.ok or not hwnd:
            return None
        wp = self.WINDOWPLACEMENT()
        wp.length = ctypes.sizeof(wp)
        if not self.user32.GetWindowPlacement(hwnd, ctypes.byref(wp)):
            return None
        r = wp.rcNormalPosition
        return {"show": int(wp.showCmd), "rect": (r.left, r.top, r.right, r.bottom), "_raw": wp}

    def set_placement(self, hwnd, placement) -> bool:
        if not self.ok or not hwnd or not placement or "_raw" not in placement:
            return False
        return bool(self.user32.SetWindowPlacement(hwnd, ctypes.byref(placement["_raw"])))

    def get_foreground(self):
        return self.user32.GetForegroundWindow() if self.ok else None

    def show(self, hwnd, cmd) -> bool:
        return bool(self.ok and self.user32.ShowWindow(hwnd, cmd))

    def focus(self, hwnd) -> bool:
        """Foreground without resizing (desktop_input.focus_window only
        restores minimized windows)."""
        if not self.ok or not hwnd:
            return False
        import desktop_input as di
        return bool(di.focus_window(hwnd))


backend = Win32Backend()


def _placement_changed(before, after) -> bool:
    if not before or not after:
        return False
    return before["show"] != after["show"] or before["rect"] != after["rect"]


def bring_forward(hwnd, be=None) -> bool:
    """Make hwnd visible and foreground without changing its size state."""
    be = be or backend
    if be.is_minimized(hwnd):
        be.show(hwnd, SW_RESTORE)            # the only case that needs a size change
    return be.focus(hwnd)


@contextmanager
def preserve_windows(target=None, be=None):
    """Capture the target's placement and the foreground window; restore
    them on exit, whatever happens inside the block."""
    be = be or backend
    foreground = be.get_foreground()
    target_before = be.get_placement(target) if target else None
    fg_before = be.get_placement(foreground) if foreground and foreground != target else None
    try:
        yield {"foreground": foreground, "target": target_before}
    finally:
        try:
            if target and target_before and _placement_changed(target_before, be.get_placement(target)):
                be.set_placement(target, target_before)
            if foreground and foreground != target:
                if fg_before and _placement_changed(fg_before, be.get_placement(foreground)):
                    be.set_placement(foreground, fg_before)   # e.g. a fullscreen app that got minimized
                if be.get_foreground() != foreground:
                    be.focus(foreground)
        except Exception:
            pass                               # restoring is best effort; never mask the real error


@contextmanager
def temporarily_visible(hwnd, be=None):
    """For UI Automation on a MINIMIZED window only: show it without
    activating it, then minimize it again. Visible windows are untouched."""
    be = be or backend
    was_min = be.is_minimized(hwnd)
    if was_min:
        be.show(hwnd, SW_SHOWNOACTIVATE)
    try:
        yield was_min
    finally:
        if was_min:
            be.show(hwnd, SW_SHOWMINNOACTIVE)
