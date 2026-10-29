"""
vision.py
=========
THE NEW METHODOLOGY, part 2: pixel-based fallback for apps that expose no
accessibility tree at all (games, canvases, some webviews/Electron UIs).

Two changes from the old build, both aimed at "clicking never manages to
click" and "the agent can't chain multi-step workflows":

1. COORDINATES: capture_screenshot() (desktop_input.py) now returns TRUE
   physical pixels 1:1 with PER_MONITOR_AWARE_V2 set, so a coordinate the
   model reads off the image needs no scale conversion before being
   passed to SetCursorPos. Fewer conversions, less rounding error.

2. ROUND TRIPS: the old build spent a full extra agent turn moving the
   mouse WITHOUT clicking, screenshotting, and asking the model to
   eyeball a red/green overlay before a click was even attempted - twice
   the turns per click, which is exactly what starves multi-step chains
   of their turn budget. Here, click_and_verify() clicks immediately and
   returns a before/after crop IN THE SAME CALL, so one tool call is one
   completed, confirmed action.
"""
import time

from desktop_input import (
    IS_WINDOWS, capture_screenshot, coords_to_screen, screen_to_coords,
    move_mouse_abs, click_mouse, click_at, drag_mouse, scroll_mouse,
)

try:
    from PIL import Image, ImageDraw, ImageChops
except ImportError:
    Image = ImageDraw = ImageChops = None

CHANGE_THRESHOLD = 6.0


def _crop_around(shot, meta, abs_x, abs_y, box=90):
    mx, my = screen_to_coords(meta, abs_x, abs_y)
    left, top = max(0, int(mx - box)), max(0, int(my - box))
    right, bottom = min(shot.width, int(mx + box)), min(shot.height, int(my + box))
    if right <= left or bottom <= top:
        return None
    return shot.crop((left, top, right, bottom))


def _diff_score(a, b):
    if a is None or b is None or ImageChops is None:
        return None
    try:
        a2 = a.resize(b.size) if a.size != b.size else a
        diff = ImageChops.difference(a2.convert("RGB"), b.convert("RGB"))
        hist = diff.convert("L").histogram()
        total = sum(hist)
        return sum(i * c for i, c in enumerate(hist)) / max(total, 1)
    except Exception:
        return None


def inspect(window_title: str = None, max_width=2200, max_height=1400, be=None, find=None, capture=None):
    """Fresh screenshot for visual inspection - observational.

    With a window_title, the window is brought in front only for the capture
    (a maximized window stays maximized; only a minimized one is restored),
    then the user's previous foreground window and every placement are put
    back, even if the capture fails. The returned meta records the target
    window so a later click_at can bring it forward again before clicking,
    keeping the coordinates valid."""
    import window_state
    capture = capture or capture_screenshot
    target = None
    if window_title:
        if find is None:
            from desktop_input import find_window as find
        match = find(window_title)
        target = match[0] if match else None
    if target is None:
        shot, meta = capture(max_width, max_height)
    else:
        with window_state.preserve_windows(target, be=be):
            window_state.bring_forward(target, be=be)
            time.sleep(0.1)
            shot, meta = capture(max_width, max_height)
    if shot is None:
        return None, None, {"ok": False, "message": "Screenshot capture failed."}
    if target is not None:
        meta = {**meta, "target_hwnd": target}
    return shot, meta, {"ok": True}


def _front_target(meta):
    """Before input: the screenshot may show a window that inspection put back
    behind the user's app. Bring it forward (no resize) so the click lands on
    what the image showed."""
    target = (meta or {}).get("target_hwnd")
    if target:
        import window_state
        window_state.bring_forward(target)
        time.sleep(0.08)


def click_and_verify(meta: dict, x: int, y: int, button="left", double=False, box=90):
    """Click at screenshot-space (x, y) using the given meta to resolve
    absolute screen coordinates, then immediately capture a before/after
    crop and diff score - all in one call, no extra agent turn."""
    if meta is None:
        return {"ok": False, "message": "No reference screenshot to click against. Call inspect_screen first."}
    abs_x, abs_y = coords_to_screen(meta, x, y)
    _front_target(meta)

    before_shot, before_meta = capture_screenshot(meta.get("orig_w"), meta.get("orig_h"))
    before_crop = _crop_around(before_shot, before_meta, abs_x, abs_y, box) if before_shot else None

    click_at(abs_x, abs_y, button=button, double=double)
    time.sleep(0.25)

    after_shot, after_meta = capture_screenshot(meta.get("orig_w"), meta.get("orig_h"))
    after_crop = _crop_around(after_shot, after_meta, abs_x, abs_y, box) if after_shot else None

    score = _diff_score(before_crop, after_crop)
    if score is None:
        verdict, detail = "unknown", "Could not compute a before/after diff."
    elif score >= CHANGE_THRESHOLD:
        verdict, detail = "changed", f"Visible change near the click point (diff={score:.1f})."
    else:
        verdict, detail = "unchanged", f"No visible change near the click point (diff={score:.1f}) - it may not have landed, or this control doesn't change appearance visibly."

    composite = None
    if before_crop is not None and after_crop is not None and ImageDraw is not None:
        w, h = max(before_crop.width, after_crop.width), max(before_crop.height, after_crop.height)
        pad, banner_h = 10, 22
        composite = Image.new("RGB", (w * 2 + pad * 3, h + banner_h + pad * 2), (20, 20, 24))
        draw = ImageDraw.Draw(composite)
        composite.paste(before_crop, (pad, banner_h + pad))
        composite.paste(after_crop, (pad * 2 + w, banner_h + pad))
        draw.text((pad, 4), "BEFORE", fill=(180, 180, 190))
        draw.text((pad * 2 + w, 4), "AFTER", fill=(180, 180, 190))
        color = {"changed": (0, 200, 90), "unchanged": (230, 60, 60), "unknown": (200, 170, 60)}[verdict]
        draw.rectangle((0, h + banner_h + pad - 2, composite.width, h + banner_h + pad - 2), fill=color)

    return {
        "ok": True, "verdict": verdict, "message": detail,
        "composite_image": composite, "after_shot": after_shot, "after_meta": after_meta,
    }


def move_and_drag(meta, x1, y1, x2, y2, button="left"):
    ax1, ay1 = coords_to_screen(meta, x1, y1)
    ax2, ay2 = coords_to_screen(meta, x2, y2)
    _front_target(meta)
    drag_mouse(ax1, ay1, ax2, ay2, button=button)


def scroll_at(meta, x, y, amount):
    ax, ay = coords_to_screen(meta, x, y)
    _front_target(meta)
    move_mouse_abs(ax, ay)
    time.sleep(0.05)
    scroll_mouse(amount)