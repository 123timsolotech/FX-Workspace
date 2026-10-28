"""
app_window.py
=============
The workspace as a window of its own, and the Studio Overlay's always-on-top window.

The app stays a local web page at http://127.0.0.1:8000 - any browser on this PC can still
open it. What this adds:

- open_app(): the workspace in an app window - Microsoft Edge's (or Chrome's) --app mode:
  no tabs and no address bar, its own taskbar button, its own profile
  (data/app-window-profile, so the UI's storage never mixes with your browsing) and WebRTC
  kept off non-proxied UDP. No Edge or Chrome: the default browser, as before. run.bat runs
  `app_window.py --wait-and-open` once the server answers (APP_WINDOW=0 there opens a tab).
- overlay_sync(settings): Settings > Appearance > "Always on top of Roblox Studio" - the
  overlay panels (the ?hud=1 page) in a small app window that stays above other windows
  (SetWindowPos HWND_TOPMOST), starts over the top-right corner of Roblox Studio's window
  and moves with it, fades to the overlay opacity (a layered window) and lets clicks
  through when click-through is on (WS_EX_TRANSPARENT). Windows only; elsewhere it is a
  no-op and Settings says so.

Nothing here sends anything anywhere: the windows load the local server's page only.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.request

APP_URL = os.environ.get("AGENTIC_APP_URL", "http://127.0.0.1:8000")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(BASE_DIR, "data", "app-window-profile")
# The overlay window has a profile of its own: in the main window's profile Edge opens it in the
# running browser, which ignores --window-size and reuses the main window's size and place.
HUD_PROFILE = os.path.join(BASE_DIR, "data", "overlay-window-profile")
HUD_TITLE = "FX Workspace - HUD"             # index.html sets it on the ?hud=1 page
HUD_SIZE = (280, 440)
WINDOWS = sys.platform == "win32"


def find_browser() -> str | None:
    """Edge first (every Windows 10/11 PC has it), then Chrome - the same search as the assistant's browser."""
    try:
        from browser import AgentBrowser
        return AgentBrowser.find_executable()
    except Exception:
        return None


def app_args(exe: str, url: str, size: tuple[int, int] | None = None, pos: tuple[int, int] | None = None,
             profile: str = PROFILE) -> list[str]:
    try:
        from browser import QUIET_ARGS            # the browser's own background traffic, off
    except Exception:
        QUIET_ARGS = ()
    args = [exe, f"--app={url}", f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
            "--force-webrtc-ip-handling-policy=disable_non_proxied_udp", *QUIET_ARGS]
    if size:
        args.append(f"--window-size={size[0]},{size[1]}")
    if pos:
        args.append(f"--window-position={pos[0]},{pos[1]}")
    return args


def _launch(args: list[str]) -> subprocess.Popen | None:
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        return subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                close_fds=True, creationflags=flags if WINDOWS else 0)
    except OSError:
        return None


def open_app(url: str = APP_URL) -> str:
    """Open the workspace in an app window; returns how it was opened ("app" or "browser")."""
    exe = find_browser()
    if exe:
        os.makedirs(PROFILE, exist_ok=True)
        if _launch(app_args(exe, url, size=(1440, 920))):
            return "app"
    import webbrowser
    webbrowser.open(url)
    return "browser"


def wait_for_server(url: str = APP_URL, timeout: float = 45.0) -> bool:
    """True once the local server answers (it is loopback, so no proxy is involved)."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with opener.open(f"{url}/api/settings", timeout=1.5) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


# ---- The overlay window (Windows) --------------------------------------------------------
if WINDOWS:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    _user32.EnumWindows.argtypes = [_EnumProc, wintypes.LPARAM]
    _user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    _user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    _user32.IsWindowVisible.argtypes = [wintypes.HWND]
    _user32.IsWindow.argtypes = [wintypes.HWND]
    _user32.IsIconic.argtypes = [wintypes.HWND]
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    _user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    _user32.GetWindowLongW.restype = ctypes.c_long
    _user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
    _user32.SetWindowLongW.restype = ctypes.c_long
    _user32.SetLayeredWindowAttributes.argtypes = [wintypes.HWND, wintypes.COLORREF, wintypes.BYTE, wintypes.DWORD]
    _user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    _user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]

HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE, SWP_NOOWNERZORDER = 0x0001, 0x0002, 0x0010, 0x0200
# Never takes focus; and without NOOWNERZORDER Chromium turns the "topmost" change down (it returns
# success and the window stays an ordinary one - found testing Edge's --app windows).
SWP_QUIET = SWP_NOACTIVATE | SWP_NOOWNERZORDER
GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOOLWINDOW = 0x00080000, 0x00000020, 0x00000080
LWA_ALPHA = 0x2
WM_CLOSE = 0x0010


def _windows() -> list[tuple[int, str]]:
    """Visible top-level windows as (handle, title)."""
    found: list[tuple[int, str]] = []

    def each(hwnd, _):
        if _user32.IsWindowVisible(hwnd):
            n = _user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                _user32.GetWindowTextW(hwnd, buf, n + 1)
                found.append((hwnd, buf.value))
        return True

    _user32.EnumWindows(_EnumProc(each), 0)
    return found


def _rect(hwnd) -> tuple[int, int, int, int] | None:
    r = wintypes.RECT()
    if not _user32.GetWindowRect(hwnd, ctypes.byref(r)):
        return None
    return r.left, r.top, r.right, r.bottom


def _pid(hwnd) -> int:
    pid = wintypes.DWORD()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def find_studio() -> int | None:
    """Roblox Studio's main window: its process is RobloxStudioBeta.exe; its title ends in "Roblox Studio"."""
    try:
        import psutil
    except ImportError:
        psutil = None
    for hwnd, title in _windows():
        if not title.endswith("Roblox Studio") or _user32.IsIconic(hwnd):
            continue
        if psutil is None:
            return hwnd
        try:
            if psutil.Process(_pid(hwnd)).name().lower().startswith("robloxstudio"):
                return hwnd
        except Exception:
            continue
    return None


def find_hud() -> int | None:
    for hwnd, title in _windows():
        if title == HUD_TITLE or title.startswith(HUD_TITLE + " "):
            return hwnd
    return None


def style_window(hwnd, opacity: float, click_through: bool) -> None:
    """Topmost, faded to the opacity, clicks passing through when asked; kept off Alt+Tab."""
    ex = _user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    want = (ex | WS_EX_LAYERED | WS_EX_TOOLWINDOW) & ~WS_EX_TRANSPARENT
    if click_through:
        want |= WS_EX_TRANSPARENT
    if want != ex:
        _user32.SetWindowLongW(hwnd, GWL_EXSTYLE, want)
    _user32.SetLayeredWindowAttributes(hwnd, 0, max(60, min(255, int(round(opacity * 255)))), LWA_ALPHA)
    _user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_QUIET)


WS_EX_TOPMOST = 0x00000008


def keep_on_top(title: str) -> bool:
    """The desktop app's mini window on top of everything. Electron's setAlwaysOnTop doesn't stick on Windows (the
    SWP_NOOWNERZORDER case above), so the server pins it - only when it isn't topmost already."""
    if not WINDOWS:
        return False
    for hwnd, t in _windows():
        if t == title:
            if not _user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOPMOST:
                _user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_QUIET)
            return True
    return False


class _Overlay:
    """The overlay window: opened, kept on top and moved with Roblox Studio by one thread."""

    def __init__(self):
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.stop = threading.Event()
        self.settings = {"opacity": 0.88, "click_through": False}
        self.hwnd: int | None = None
        self.state = "off"                    # off | opening | open | closed-by-user | no-browser | unsupported

    def start(self):
        with self.lock:
            if self.thread and self.thread.is_alive():
                return
            self.stop.clear()
            self.thread = threading.Thread(target=self._run, daemon=True, name="overlay-window")
            self.thread.start()

    def close(self):
        self.stop.set()
        hwnd, self.hwnd = self.hwnd or (find_hud() if WINDOWS else None), None
        if hwnd:
            _user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(timeout=2)
        self.state = "off"

    @staticmethod
    def _home() -> tuple[int, int]:
        """Where the overlay starts: over Roblox Studio's top-right corner, under its ribbon - or the
        screen's top-right when Studio isn't open."""
        studio = find_studio()
        if studio and (r := _rect(studio)):
            return r[2] - HUD_SIZE[0] - 24, r[1] + 120
        return _user32.GetSystemMetrics(0) - HUD_SIZE[0] - 24, 80          # SM_CXSCREEN

    def _open(self) -> int | None:
        exe = find_browser()
        if not exe:
            self.state = "no-browser"
            return None
        pos = self._home()
        os.makedirs(HUD_PROFILE, exist_ok=True)
        _launch(app_args(exe, f"{APP_URL}/?hud=1", size=HUD_SIZE, pos=pos, profile=HUD_PROFILE))
        for _ in range(40):                                    # the page sets its title once it loads
            if self.stop.wait(0.25):
                return None
            if hwnd := find_hud():
                # Its size and place are set here too: a browser can restore a remembered placement.
                _user32.SetWindowPos(hwnd, HWND_TOPMOST, pos[0], pos[1], HUD_SIZE[0], HUD_SIZE[1], SWP_QUIET)
                return hwnd
        return None

    def _run(self):
        self.state = "opening"
        self.hwnd = find_hud() or self._open()
        if not self.hwnd:
            if self.state == "opening":
                self.state = "off"
            return
        self.state = "open"
        last_studio = None
        while not self.stop.is_set():
            hwnd = self.hwnd
            if not _user32.IsWindow(hwnd):
                self.state = "closed-by-user"                  # closed from its own title bar: leave it closed
                self.hwnd = None
                return
            style_window(hwnd, self.settings["opacity"], self.settings["click_through"])
            # Follow Roblox Studio: when its window moves, the overlay moves by the same amount.
            studio = find_studio()
            sr = _rect(studio) if studio else None
            if sr and last_studio and sr[:2] != last_studio[:2] and (hr := _rect(hwnd)):
                dx, dy = sr[0] - last_studio[0], sr[1] - last_studio[1]
                _user32.SetWindowPos(hwnd, HWND_TOPMOST, hr[0] + dx, hr[1] + dy, 0, 0, SWP_NOSIZE | SWP_QUIET)
            if sr:
                last_studio = sr
            self.stop.wait(0.4)


_overlay = _Overlay()


def overlay_sync(settings: dict) -> None:
    """Open, close or restyle the always-on-top overlay window to match the settings."""
    if not WINDOWS:
        _overlay.state = "unsupported"
        return
    if in_desktop_app():
        # The desktop app draws the mini window itself (desktop/main.js reads /api/shell) - no Edge window. One
        # left over from an older version is closed.
        if _overlay.state != "off" or find_hud():
            threading.Thread(target=_overlay.close, daemon=True, name="overlay-close").start()
        return
    _overlay.settings = {"opacity": float(settings.get("overlay_opacity") or 0.88),
                         "click_through": bool(settings.get("overlay_clickthrough"))}
    if settings.get("overlay_window"):
        _overlay.start()
    elif _overlay.state != "off" or find_hud():
        threading.Thread(target=_overlay.close, daemon=True, name="overlay-close").start()


def in_desktop_app() -> bool:
    """Running under the desktop app (desktop/main.js starts the launcher with FX_SHELL=electron)."""
    return os.environ.get("FX_SHELL") == "electron"


def studio_rect() -> list | None:
    """Roblox Studio's window [left, top, right, bottom], for the mini window to sit over and follow."""
    if not WINDOWS:
        return None
    hwnd = find_studio()
    r = _rect(hwnd) if hwnd else None
    return list(r) if r else None


def overlay_close() -> None:
    """The server is stopping: the overlay window goes with it."""
    if WINDOWS:
        _overlay.close()


def overlay_status() -> dict:
    return {"state": _overlay.state, "supported": WINDOWS, "open": bool(_overlay.hwnd)}


if __name__ == "__main__":
    # run.bat: `app_window.py --wait-and-open` after starting the server, `--open` when it already runs.
    if "--wait-and-open" in sys.argv and not wait_for_server():
        sys.exit(1)
    open_app()
