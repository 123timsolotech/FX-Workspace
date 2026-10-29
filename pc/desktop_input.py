"""
desktop_input.py
=================
Low-level Windows primitives: DPI awareness, SendInput mouse/keyboard,
window enumeration/focus/close, and a physical-pixel screenshot capture.

This layer intentionally does NOT know anything about "the agent" or
"tools" - it is pure OS mechanics, reused by both elements.py (native
control automation) and vision.py (pixel fallback).

IMPORTANT FIX vs the previous build: capture_screenshot() now captures at
TRUE PHYSICAL PIXELS with NO thumbnail/resize step by default. The old
build downscaled to a 1920x1080 thumbnail and threaded a scale_x/scale_y
factor through every later coordinate transform. Every extra scale step is
an extra place for rounding error to creep in, and on a 150%-scaled 4K
monitor that error is easily 10-20px - enough to miss a button. Since we
also set PER_MONITOR_DPI_AWARE below, physical pixels from GDI and the
physical pixels SetCursorPos/SendInput expect are the SAME coordinate
space, so a raw click coordinate now needs zero scaling math at all.
"""
import ctypes
import os
import time

IS_WINDOWS = hasattr(os, "startfile")

try:
    from PIL import Image, ImageDraw, ImageChops
except ImportError:
    Image = ImageDraw = ImageChops = None

if IS_WINDOWS:
    # PER_MONITOR_DPI_AWARE_V2 puts every later screen/mouse coordinate in
    # one consistent physical-pixel space. Without this, Windows silently
    # virtualizes coordinates on scaled displays and every click lands
    # somewhere other than where it was aimed - this is very likely why
    # clicking "never managed to click" reliably in the old build.
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    gdi32 = ctypes.windll.gdi32

    # ---- pointer-sized (HANDLE) return/arg types -----------------------
    # ctypes defaults every foreign-function return value and argument to a
    # 32-bit c_int unless told otherwise. HWND/HDC/HBITMAP/HANDLE are all
    # pointer-sized (64-bit) on 64-bit Windows, so without these
    # declarations, values silently truncate to 32 bits on the way in AND
    # out. Small window handles usually survive that by luck; GDI device-
    # context and bitmap handles are not guaranteed to, and feeding a
    # truncated handle back into a native GDI/User32 call is undefined
    # behavior - a real, intermittent, whole-process-crashing bug, not a
    # catchable Python exception. Declare real types everywhere a HANDLE
    # crosses the boundary.
    from ctypes import wintypes

    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                              wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                 ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]

    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.FindWindowW.restype = wintypes.HWND
    user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    user32.GetAncestor.restype = wintypes.HWND
    user32.GetAncestor.argtypes = [wintypes.HWND, ctypes.c_uint]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.SetForegroundWindow.argtypes = [wintypes.HWND]
    user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.BringWindowToTop.argtypes = [wintypes.HWND]
    user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
    user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    # NOTE: GetWindowRect / GetCursorPos deliberately left untyped here -
    # the functions below that call them define their own local POINT/RECT
    # Structure classes, and ctypes strictly type-checks POINTER argument
    # classes, so declaring wintypes.POINT/RECT here would break those
    # byref() calls. Neither POINT nor RECT is a HANDLE, so they were never
    # part of the truncation risk this patch targets.

    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD

    # ---- keyboard ----
    VK_MAP = {
        "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
        "backspace": 0x08, "space": 0x20, "up": 0x26, "down": 0x28, "left": 0x25,
        "right": 0x27, "delete": 0x2E, "home": 0x24, "end": 0x23,
        "pageup": 0x21, "pagedown": 0x22, "win": 0x5B, "ctrl": 0x11, "control": 0x11,
        "alt": 0x12, "shift": 0x10,
        "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73, "f5": 0x74, "f6": 0x75,
        "f7": 0x76, "f8": 0x77, "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    }
    for _c in "abcdefghijklmnopqrstuvwxyz":
        VK_MAP[_c] = ord(_c.upper())
    for _d in "0123456789":
        VK_MAP[_d] = ord(_d)

    VK_MEDIA_NEXT_TRACK = 0xB0
    VK_MEDIA_PREV_TRACK = 0xB1
    VK_MEDIA_PLAY_PAUSE = 0xB3
    VK_VOLUME_MUTE = 0xAD
    VK_VOLUME_DOWN = 0xAE
    VK_VOLUME_UP = 0xAF
    MEDIA_KEYS = {
        "play_pause": VK_MEDIA_PLAY_PAUSE, "next": VK_MEDIA_NEXT_TRACK,
        "previous": VK_MEDIA_PREV_TRACK, "mute": VK_VOLUME_MUTE,
        "volume_up": VK_VOLUME_UP, "volume_down": VK_VOLUME_DOWN,
    }

    KEYEVENTF_EXTENDEDKEY = 0x0001
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_UNICODE = 0x0004
    INPUT_MOUSE = 0
    INPUT_KEYBOARD = 1

    class KeyBdInput(ctypes.Structure):
        _fields_ = [("wVk", ctypes.c_ushort), ("wScan", ctypes.c_ushort),
                    ("dwFlags", ctypes.c_ulong), ("time", ctypes.c_ulong),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class MouseInput(ctypes.Structure):
        _fields_ = [("dx", ctypes.c_long), ("dy", ctypes.c_long),
                    ("mouseData", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                    ("time", ctypes.c_ulong), ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class InputUnion(ctypes.Union):
        _fields_ = [("ki", KeyBdInput), ("mi", MouseInput)]

    class Input(ctypes.Structure):
        _fields_ = [("type", ctypes.c_ulong), ("ii", InputUnion)]

    def _send_vk(vk_code, extended=True, key_up=False):
        flags = (KEYEVENTF_EXTENDEDKEY if extended else 0) | (KEYEVENTF_KEYUP if key_up else 0)
        user32.keybd_event(vk_code, 0, flags, 0)

    def send_key_combo(combo: str):
        """e.g. 'ctrl+s', 'win+m', 'alt+f4', 'enter'."""
        parts = [p.strip().lower() for p in combo.split("+") if p.strip()]
        vks = [VK_MAP.get(p) for p in parts]
        if any(v is None for v in vks):
            raise ValueError(f"Unknown key in combo: {combo}")
        for vk in vks:
            _send_vk(vk, key_up=False)
            time.sleep(0.01)
        for vk in reversed(vks):
            _send_vk(vk, key_up=True)
            time.sleep(0.01)

    def press_media_key(name: str):
        vk = MEDIA_KEYS.get(name)
        if vk is None:
            raise ValueError(f"Unknown media key: {name}")
        _send_vk(vk, key_up=False)
        _send_vk(vk, key_up=True)

    def send_unicode_text(text: str, delay: float = 0.008):
        extra = ctypes.c_ulong(0)
        for ch in text:
            down = Input(INPUT_KEYBOARD, InputUnion(ki=KeyBdInput(0, ord(ch), KEYEVENTF_UNICODE, 0, ctypes.pointer(extra))))
            up = Input(INPUT_KEYBOARD, InputUnion(ki=KeyBdInput(0, ord(ch), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, ctypes.pointer(extra))))
            user32.SendInput(1, ctypes.pointer(down), ctypes.sizeof(down))
            user32.SendInput(1, ctypes.pointer(up), ctypes.sizeof(up))
            if delay:
                time.sleep(delay)

    # ---- mouse ----
    MOUSEEVENTF_LEFTDOWN = 0x0002
    MOUSEEVENTF_LEFTUP = 0x0004
    MOUSEEVENTF_RIGHTDOWN = 0x0008
    MOUSEEVENTF_RIGHTUP = 0x0010
    MOUSEEVENTF_MIDDLEDOWN = 0x0020
    MOUSEEVENTF_MIDDLEUP = 0x0040
    MOUSEEVENTF_WHEEL = 0x0800

    def _send_mouse_input(dx, dy, data, flags):
        extra = ctypes.c_ulong(0)
        mi = MouseInput(dx, dy, data, flags, 0, ctypes.pointer(extra))
        inp = Input(INPUT_MOUSE, InputUnion(mi=mi))
        user32.SendInput(1, ctypes.pointer(inp), ctypes.sizeof(inp))

    def move_mouse_abs(x, y) -> bool:
        # Physical-pixel absolute move. With PER_MONITOR_AWARE_V2 set above,
        # this is the same coordinate space GDI screenshots use, so callers
        # never need to convert between "screenshot space" and "screen space".
        return bool(user32.SetCursorPos(int(x), int(y)))

    def get_mouse_pos():
        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
        pt = POINT()
        if user32.GetCursorPos(ctypes.byref(pt)):
            return int(pt.x), int(pt.y)
        return None

    def click_mouse(button="left", double=False):
        pairs = {"left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
                 "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
                 "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP)}
        down, up = pairs.get(button, pairs["left"])
        _send_mouse_input(0, 0, 0, down)
        time.sleep(0.05)
        _send_mouse_input(0, 0, 0, up)
        if double:
            time.sleep(0.08)
            _send_mouse_input(0, 0, 0, down)
            time.sleep(0.05)
            _send_mouse_input(0, 0, 0, up)

    def drag_mouse(x1, y1, x2, y2, button="left"):
        pairs = {"left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
                 "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
                 "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP)}
        down, up = pairs.get(button, pairs["left"])
        move_mouse_abs(x1, y1)
        time.sleep(0.05)
        _send_mouse_input(0, 0, 0, down)
        time.sleep(0.08)
        move_mouse_abs(x2, y2)
        time.sleep(0.08)
        _send_mouse_input(0, 0, 0, up)

    def scroll_mouse(amount):
        _send_mouse_input(0, 0, int(amount * 120), MOUSEEVENTF_WHEEL)

    def click_at(x, y, button="left", double=False):
        """Move + click in one call. Focuses the owning window first (see
        hwnd_at_screen_point) since a click on a background window is very
        often absorbed by Windows as a plain 'activate' click instead of
        reaching the control underneath - this looked exactly like 'the
        click did nothing' in the old build."""
        hwnd = hwnd_at_screen_point(x, y)
        if hwnd:
            focus_window(hwnd)
            time.sleep(0.05)
        move_mouse_abs(x, y)
        time.sleep(0.05)
        click_mouse(button=button, double=double)

    # ---- window enumeration / focus / close ----
    GA_ROOT = 2
    GWL_EXSTYLE = -20
    WS_EX_TOOLWINDOW = 0x00000080
    SW_RESTORE = 9
    WM_CLOSE = 0x0010
    WM_SYSCOMMAND = 0x0112
    SC_CLOSE = 0xF060

    def enum_visible_windows():
        results = []
        WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _callback(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                if length > 0:
                    buf = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(hwnd, buf, length + 1)
                    if buf.value.strip():
                        results.append((hwnd, buf.value))
            return True

        user32.EnumWindows(WNDENUMPROC(_callback), 0)
        return results

    def _window_area(hwnd):
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        r = RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return max(0, r.right - r.left) * max(0, r.bottom - r.top)
        return 0

    def get_window_rect(hwnd):
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]
        r = RECT()
        if user32.GetWindowRect(hwnd, ctypes.byref(r)):
            return r.left, r.top, r.right, r.bottom
        return None

    def find_window(title_substring: str):
        """Best-matching TOP-LEVEL window for a title substring: resolves
        child/helper windows to their root, drops tool windows, and prefers
        the largest visible match."""
        needle = title_substring.lower().strip()
        if needle in ("desktop", "program manager", "desktop icons"):
            hwnd = user32.FindWindowW("Progman", None)
            if hwnd:
                return hwnd, "Program Manager"
        best = {}
        for hwnd, title in enum_visible_windows():
            if needle not in title.lower():
                continue
            root = user32.GetAncestor(hwnd, GA_ROOT) or hwnd
            ex_style = user32.GetWindowLongW(root, GWL_EXSTYLE)
            if ex_style & WS_EX_TOOLWINDOW:
                continue
            best.setdefault(root, title)
        if not best:
            return None
        ranked = sorted(best.items(), key=lambda kv: _window_area(kv[0]), reverse=True)
        return ranked[0]

    def focus_window(hwnd) -> bool:
        current_thread_id = kernel32.GetCurrentThreadId()
        fg_hwnd = user32.GetForegroundWindow()
        fg_thread_id = user32.GetWindowThreadProcessId(fg_hwnd, None)
        target_thread_id = user32.GetWindowThreadProcessId(hwnd, None)
        attached_fg = attached_target = False
        if fg_thread_id and fg_thread_id != current_thread_id:
            attached_fg = bool(user32.AttachThreadInput(current_thread_id, fg_thread_id, True))
        if target_thread_id and target_thread_id != current_thread_id:
            attached_target = bool(user32.AttachThreadInput(current_thread_id, target_thread_id, True))
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 0x0002, 0)
        try:
            user32.AllowSetForegroundWindow(0xFFFFFFFF)
        except Exception:
            pass
        # Only a MINIMIZED window needs SW_RESTORE. On a maximized window
        # SW_RESTORE means "un-maximize", which is how focusing (and so
        # inspecting) an app used to knock it out of maximized/fullscreen.
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, SW_RESTORE)
        user32.BringWindowToTop(hwnd)
        succeeded = bool(user32.SetForegroundWindow(hwnd))
        if not succeeded:
            try:
                user32.SwitchToThisWindow(hwnd, True)
            except Exception:
                pass
        if attached_fg:
            user32.AttachThreadInput(current_thread_id, fg_thread_id, False)
        if attached_target:
            user32.AttachThreadInput(current_thread_id, target_thread_id, False)
        return user32.GetForegroundWindow() == hwnd

    def close_window(hwnd, force=False, exe_path_hint=None):
        if force:
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                PROCESS_TERMINATE = 0x0001
                h = kernel32.OpenProcess(PROCESS_TERMINATE, False, pid.value)
                if h:
                    kernel32.TerminateProcess(h, 1)
                    kernel32.CloseHandle(h)
                    return True
            return False
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        return True

    class _ClickPoint(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    def hwnd_at_screen_point(x, y):
        try:
            user32.WindowFromPoint.restype = ctypes.c_void_p
            hwnd = user32.WindowFromPoint(_ClickPoint(int(x), int(y)))
            if not hwnd:
                return None
            return user32.GetAncestor(hwnd, GA_ROOT) or hwnd
        except Exception:
            return None

    def get_active_window_title():
        hwnd = user32.GetForegroundWindow()
        length = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        return buf.value

    # ---- screenshot (physical pixels, GDI, includes real cursor) ----
    def capture_screenshot(max_w=None, max_h=None):
        """Capture the whole virtual desktop in TRUE physical pixels via GDI
        (same coordinate space as SetCursorPos). max_w/max_h only apply if
        the image would otherwise be absurdly large (e.g. multi-4K setups);
        for normal single/dual-monitor setups nothing is resized, so a
        returned (x, y) needs no scale conversion at all - just add the
        virtual-desktop origin (meta['vx'], meta['vy']).
        """
        try:
            from PIL import Image as PILImage

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                    ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32)]

            class BITMAPINFO(ctypes.Structure):
                _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", ctypes.c_uint32 * 3)]

            vx, vy = int(user32.GetSystemMetrics(76)), int(user32.GetSystemMetrics(77))
            vw, vh = int(user32.GetSystemMetrics(78)), int(user32.GetSystemMetrics(79))
            if vw <= 0 or vh <= 0:
                return None, None

            screen_dc = user32.GetDC(0)
            mem_dc = gdi32.CreateCompatibleDC(screen_dc)
            bitmap = gdi32.CreateCompatibleBitmap(screen_dc, vw, vh)
            old_bitmap = gdi32.SelectObject(mem_dc, bitmap)
            SRCCOPY = 0x00CC0020
            CAPTUREBLT = 0x40000000
            gdi32.BitBlt(mem_dc, 0, 0, vw, vh, screen_dc, vx, vy, SRCCOPY | CAPTUREBLT)

            bmi = BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = vw
            bmi.bmiHeader.biHeight = -vh  # top-down
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0  # BI_RGB

            buf_size = vw * vh * 4
            buf = (ctypes.c_ubyte * buf_size)()
            gdi32.GetDIBits(mem_dc, bitmap, 0, vh, buf, ctypes.byref(bmi), 0)

            img = PILImage.frombuffer("RGBA", (vw, vh), buf, "raw", "BGRA", 0, 1).convert("RGB")

            gdi32.SelectObject(mem_dc, old_bitmap)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(mem_dc)
            user32.ReleaseDC(0, screen_dc)

            # Draw the real cursor in so the model can see where it actually is.
            try:
                pos = get_mouse_pos()
                if pos:
                    draw = ImageDraw.Draw(img)
                    cx, cy = pos[0] - vx, pos[1] - vy
                    draw.polygon([(cx, cy), (cx, cy + 16), (cx + 5, cy + 12), (cx + 8, cy + 19),
                                  (cx + 10, cy + 18), (cx + 7, cy + 11), (cx + 12, cy + 11)],
                                 fill=(255, 255, 255), outline=(0, 0, 0))
            except Exception:
                pass

            meta = {"vx": vx, "vy": vy, "orig_w": vw, "orig_h": vh,
                    "scale_x": 1.0, "scale_y": 1.0, "thumb_w": vw, "thumb_h": vh}

            if max_w and max_h and (vw > max_w or vh > max_h):
                thumb = img.copy()
                thumb.thumbnail((max_w, max_h))
                meta = {"vx": vx, "vy": vy, "orig_w": vw, "orig_h": vh,
                        "scale_x": vw / max(thumb.width, 1), "scale_y": vh / max(thumb.height, 1),
                        "thumb_w": thumb.width, "thumb_h": thumb.height}
                return thumb, meta

            return img, meta
        except Exception:
            return None, None

else:
    # Non-Windows: automation is out of scope for this build, but importing
    # the module (e.g. for local dev/testing on the sandbox) shouldn't crash.
    user32 = None

    def _unsupported(*a, **k):
        raise RuntimeError("desktop_input: this build only automates Windows.")

    move_mouse_abs = get_mouse_pos = click_mouse = drag_mouse = scroll_mouse = click_at = _unsupported
    send_key_combo = press_media_key = send_unicode_text = _unsupported
    enum_visible_windows = find_window = focus_window = close_window = _unsupported
    hwnd_at_screen_point = get_active_window_title = get_window_rect = _unsupported

    def capture_screenshot(max_w=None, max_h=None):
        if not Image:
            return None, None
        try:
            from PIL import ImageGrab
            shot = ImageGrab.grab()
            return shot, {"vx": 0, "vy": 0, "orig_w": shot.width, "orig_h": shot.height,
                          "scale_x": 1.0, "scale_y": 1.0, "thumb_w": shot.width, "thumb_h": shot.height}
        except Exception:
            return None, None


def coords_to_screen(meta, x, y):
    """Screenshot-space pixel -> absolute screen pixel."""
    return meta["vx"] + x * float(meta.get("scale_x", 1.0)), meta["vy"] + y * float(meta.get("scale_y", 1.0))


def screen_to_coords(meta, abs_x, abs_y):
    """Absolute screen pixel -> screenshot-space pixel (for annotating a
    previously-taken image)."""
    sx = float(meta.get("scale_x", 1.0)) or 1.0
    sy = float(meta.get("scale_y", 1.0)) or 1.0
    return (abs_x - meta["vx"]) / sx, (abs_y - meta["vy"]) / sy