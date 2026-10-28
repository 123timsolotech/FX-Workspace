"""
approval.py
===========
The desktop approval popup, lifted out of main.py so it can be styled,
sized and tested on its own.

What was wrong with the old dialog, and what this fixes:

 * IT WAS CROPPED. It positioned itself centred on the MOUSE POINTER
   (`+mx - ww//2 + my - wh//2`) with no clamping, so any time the cursor sat
   near an edge - which is most of the time, because the user has just been
   clicking around - half the window landed off-screen, including the
   buttons. It now centres on the WORK AREA of the monitor the cursor is on
   (excluding the taskbar) and clamps to it, so it is always fully visible on
   any monitor in any arrangement.

 * IT WAS INCONSISTENTLY SIZED. Height came from `min(60 * n, 420)` - a guess
   at 60px per action that was wrong as soon as a justification wrapped to two
   lines, so content was clipped with no scrollbar in sight. The list is now
   measured after layout, the window takes the height it actually needs up to
   78% of the work area, and the scrollbar appears only when it is genuinely
   needed.

 * IT RENDERED AT THE WRONG SCALE. desktop_input sets PER_MONITOR_AWARE_V2 at
   import, which stops Windows from scaling this process - correct for click
   coordinates, but it also means Tk draws at 96 DPI on a 150% display, i.e.
   unreadably small, with every hard-coded pixel width wrong. Tk's scaling
   factor is now derived from the real system DPI and every dimension is
   multiplied through it.

 * IT LOOKED LIKE A 1998 ERROR BOX. ttk's default theme ignores colour, so the
   dialog is built from tk primitives against the same palette as the web UI,
   with per-action risk colouring, pretty-printed arguments, the current plan
   step for context, and Enter/Esc bindings.
"""
import ctypes
import json
import os
import queue
import threading
import time

try:
    import tkinter as tk
    from tkinter import font as tkfont
except Exception:          # headless Python / CI: web approvals still work
    tk = tkfont = None

IS_WINDOWS = hasattr(os, "startfile")

# ==============================================================================
# Palette - matches index.html so the desktop popup and the web UI read as one
# product rather than two.
# ==============================================================================
BG = "#262624"
PANEL = "#30302e"
PANEL_ALT = "#2b2a27"
BORDER = "#45443f"
TEXT = "#f5f4ee"
MUTED = "#a6a39a"
BRAND = "#d97757"
RED = "#e5675c"
AMBER = "#d9a54c"
EMERALD = "#7fb069"
CYAN = "#8fb3c9"

import approval_view
import permissions

# Risk tiers drive the colour of each action's left edge and its label.
# These are permissions.py's DESTRUCTIVE/INPUT_LEVEL sets, not a second
# copy - permissions.py is what decides whether a tool needs this popup at
# all, so its classification and this popup's colouring can never drift
# apart the way two independently hand-maintained lists eventually do.
DESTRUCTIVE = permissions.DESTRUCTIVE
INPUT_LEVEL = permissions.INPUT_LEVEL

RISK = {
    "destructive": (RED, "changes files or processes"),
    "input": (AMBER, "sends input to a window"),
    "benign": (CYAN, "opens or focuses something"),
}

DIALOG_W = 660          # logical px; multiplied by the DPI scale below
MAX_H_FRACTION = 0.78
CARD_PAD = 12


def classify(tool_name: str) -> str:
    return permissions.classify(tool_name)


# ==============================================================================
# Geometry: DPI scale and the work area of the monitor under the cursor
# ==============================================================================
def system_scale() -> float:
    """Tk is not DPI-virtualised in this process (PER_MONITOR_AWARE_V2 is set
    for click accuracy), so it must be told the scale itself."""
    if not IS_WINDOWS:
        return 1.0
    try:
        dpi = ctypes.windll.user32.GetDpiForSystem()
    except Exception:
        try:
            hdc = ctypes.windll.user32.GetDC(0)
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
            ctypes.windll.user32.ReleaseDC(0, hdc)
        except Exception:
            dpi = 96
    return max(1.0, min(3.0, (dpi or 96) / 96.0))


def cursor_work_area(root):
    """(left, top, right, bottom) of the usable desktop on the monitor the
    cursor is on - taskbar excluded. This is what makes 'fully on screen' true
    on a multi-monitor setup, where the primary screen's dimensions say nothing
    about where the window will actually land."""
    if IS_WINDOWS:
        try:
            class POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

            class RECT(ctypes.Structure):
                _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                            ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

            class MONITORINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                            ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]

            pt = POINT()
            ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
            MONITOR_DEFAULTTONEAREST = 2
            hmon = ctypes.windll.user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
            info = MONITORINFO()
            info.cbSize = ctypes.sizeof(MONITORINFO)
            if ctypes.windll.user32.GetMonitorInfoW(hmon, ctypes.byref(info)):
                w = info.rcWork
                if w.right > w.left and w.bottom > w.top:
                    return w.left, w.top, w.right, w.bottom
        except Exception:
            pass
    return 0, 0, root.winfo_screenwidth(), root.winfo_screenheight()


def place_fully_on_screen(win, root, width, height):
    """Centre on the work area, then clamp so no edge can ever fall outside
    it. Shrinks the window rather than overflowing if the area is small."""
    left, top, right, bottom = cursor_work_area(root)
    aw, ah = right - left, bottom - top
    width, height = min(width, aw - 20), min(height, ah - 20)
    x = left + (aw - width) // 2
    y = top + (ah - height) // 2
    x = max(left + 10, min(x, right - width - 10))
    y = max(top + 10, min(y, bottom - height - 10))
    win.geometry(f"{int(width)}x{int(height)}+{int(x)}+{int(y)}")
    return width, height


def cursor_position(root):
    if IS_WINDOWS:
        try:
            class POINT(ctypes.Structure):
                _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
            pt = POINT()
            if ctypes.windll.user32.GetCursorPos(ctypes.byref(pt)):
                return int(pt.x), int(pt.y)
        except Exception:
            pass
    return root.winfo_pointerx(), root.winfo_pointery()


def place_near_cursor(win, root, width, height):
    """Place a compact clarification beside the pointer and keep it visible."""
    left, top, right, bottom = cursor_work_area(root)
    mx, my = cursor_position(root)
    width = min(width, right - left - 20)
    height = min(height, bottom - top - 20)
    x, y = mx + 18, my + 18
    if x + width > right - 10:
        x = mx - width - 18
    if y + height > bottom - 10:
        y = my - height - 18
    x = max(left + 10, min(x, right - width - 10))
    y = max(top + 10, min(y, bottom - height - 10))
    win.geometry(f"{int(width)}x{int(height)}+{int(x)}+{int(y)}")
    return width, height


# ==============================================================================
# Widgets
# ==============================================================================
def _format_args(args: dict) -> str:
    """Every argument, in full, one `key: value` per line (multi-line values keep
    their lines). Nothing is shortened or hidden - `force` included, since it is
    often exactly the dangerous part. The widget scrolls instead."""
    parts = []
    for k, v in (args or {}).items():
        if k == "justification" or v is None or v == "" or v == []:
            continue
        if not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False, indent=2, default=str)
        parts.append(f"{k}: {v}")
    return "\n".join(parts) or "(no arguments)"


ENTER_GUARD_MS = 700      # Enter is ignored this long after the popup appears


def _text_block(parent, text, font, scale, max_lines, diff=False):
    """Read-only, selectable, scrollable text - the full value, never cut."""
    px = lambda n: int(n * scale)
    lines = text.count("\n") + 1
    frame = tk.Frame(parent, bg=PANEL_ALT)
    box = tk.Text(frame, bg=PANEL_ALT, fg=TEXT, font=font, wrap="char", relief="flat", bd=0,
                  height=min(lines, max_lines), padx=px(8), pady=px(6), highlightthickness=0,
                  insertbackground=TEXT, selectbackground="#4a4a45")
    box.insert("1.0", text)
    if diff:
        box.tag_configure("add", foreground=EMERALD)
        box.tag_configure("del", foreground=RED)
        box.tag_configure("hunk", foreground=CYAN)
        for i, line in enumerate(text.split("\n"), 1):
            tag = "hunk" if line.startswith("@@") else "add" if line.startswith("+") and not line.startswith("+++") \
                else "del" if line.startswith("-") and not line.startswith("---") else None
            if tag:
                box.tag_add(tag, f"{i}.0", f"{i}.end")
    box.configure(state="disabled")          # read-only, still selectable/copyable
    box.pack(side="left", fill="both", expand=True)
    if lines > max_lines:
        bar = tk.Scrollbar(frame, orient="vertical", command=box.yview, width=px(10))
        box.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
    return frame


def _flat_button(parent, text, command, bg, fg, font, scale, default=False):
    btn = tk.Button(parent, text=text, command=command, bg=bg, fg=fg, font=font,
                    relief="flat", bd=0, highlightthickness=2 if default else 0,
                    highlightbackground=bg, highlightcolor="#ffffff",
                    activebackground=bg, activeforeground=fg,
                    cursor="hand2", padx=int(18 * scale), pady=int(9 * scale))
    hover = "#8d7ef5" if bg == BRAND else "#2e313c"
    btn.bind("<Enter>", lambda e: btn.configure(bg=hover, activebackground=hover))
    btn.bind("<Leave>", lambda e: btn.configure(bg=bg, activebackground=bg))
    return btn


def build_dialog(root, actions, result_holder, done_event, context_line=""):
    scale = system_scale()
    px = lambda n: int(n * scale)

    f_title = tkfont.Font(family="Segoe UI", size=13, weight="bold")
    f_body = tkfont.Font(family="Segoe UI", size=9)
    f_small = tkfont.Font(family="Segoe UI", size=8)
    f_mono = tkfont.Font(family="Consolas", size=9, weight="bold")
    f_mono_sm = tkfont.Font(family="Consolas", size=8)

    win = tk.Toplevel(root)
    win.withdraw()                       # lay out off-screen, show once sized
    win.title("FX Workspace - Approval")
    win.configure(bg=BG)
    win.resizable(False, True)
    win.minsize(px(420), px(220))

    n = len(actions)
    content_w = px(DIALOG_W)
    wrap = content_w - px(2 * CARD_PAD + 34)

    # ---- header ----------------------------------------------------------
    header = tk.Frame(win, bg=PANEL_ALT)
    header.pack(fill="x")
    tk.Frame(win, bg=BORDER, height=1).pack(fill="x")

    tk.Label(header, text=approval_view.title(n), bg=PANEL_ALT,
             fg=TEXT, font=f_title, anchor="w").pack(fill="x", padx=px(18), pady=(px(14), 0))
    tk.Label(header, text=context_line or "The agent will run these on your machine.",
             bg=PANEL_ALT, fg=MUTED, font=f_body, anchor="w", justify="left",
             wraplength=wrap).pack(fill="x", padx=px(18), pady=(px(3), px(14)))

    # ---- scrollable action list -----------------------------------------
    body = tk.Frame(win, bg=BG)
    body.pack(fill="both", expand=True)

    canvas = tk.Canvas(body, bg=BG, highlightthickness=0, bd=0)
    scrollbar = tk.Scrollbar(body, orient="vertical", command=canvas.yview,
                             bg="#454a5c", troughcolor=PANEL_ALT, bd=0, relief="flat",
                             highlightthickness=0, activebackground="#5a6076",
                             width=px(12))
    inner = tk.Frame(canvas, bg=BG)
    window_id = canvas.create_window((0, 0), window=inner, anchor="nw")
    canvas.configure(yscrollcommand=scrollbar.set)
    canvas.pack(side="left", fill="both", expand=True)

    def _sync(_event=None):
        canvas.configure(scrollregion=canvas.bbox("all"))
        canvas.itemconfigure(window_id, width=canvas.winfo_width())

    inner.bind("<Configure>", _sync)
    canvas.bind("<Configure>", _sync)

    for i, action in enumerate(actions, 1):
        tool = action["tool_name"]
        view = action.get("view") or approval_view.describe(tool, action.get("args") or {},
                                                             action.get("justification") or "", tier=classify(tool))
        colour, tier_label = RISK.get(view.get("tier"), RISK["destructive"])

        card = tk.Frame(inner, bg=PANEL)
        card.pack(fill="x", padx=px(14), pady=(px(6) if i > 1 else px(10), 0))

        # Coloured left edge encodes risk without adding another text label.
        tk.Frame(card, bg=colour, width=px(3)).pack(side="left", fill="y")
        col = tk.Frame(card, bg=PANEL)
        col.pack(side="left", fill="both", expand=True, padx=(px(11), px(11)),
                 pady=px(CARD_PAD))

        top_row = tk.Frame(col, bg=PANEL)
        top_row.pack(fill="x")
        tk.Label(top_row, text=f"{i}.", bg=PANEL, fg=MUTED, font=f_mono).pack(side="left")
        tk.Label(top_row, text=view["label"], bg=PANEL, fg=colour, font=f_mono).pack(side="left", padx=(px(6), 0))
        tk.Label(top_row, text=tool, bg=PANEL, fg=MUTED, font=f_mono_sm).pack(side="left", padx=(px(6), 0))
        tk.Label(top_row, text=tier_label, bg=PANEL, fg=MUTED, font=f_small).pack(side="right")

        if view.get("undo_text"):
            tk.Label(col, text=("↺ " if view["undoable"] else "⚠ ") + view["undo_text"], bg=PANEL,
                     fg=EMERALD if view["undoable"] else AMBER, font=f_small, anchor="w").pack(fill="x", pady=(px(4), 0))
        if view.get("warning"):
            tk.Label(col, text="⚠ " + view["warning"], bg=PANEL, fg=AMBER, font=f_small, anchor="w",
                     justify="left", wraplength=wrap).pack(fill="x", pady=(px(4), 0))

        tk.Label(col, text=view.get("justification") or approval_view.WORDING["no_reason"],
                 bg=PANEL, fg=MUTED, font=f_body, anchor="w", justify="left",
                 wraplength=wrap).pack(fill="x", pady=(px(6), 0))

        _text_block(col, _format_args(action.get("args") or {}), f_mono_sm, scale, 10).pack(fill="x", pady=(px(6), 0))

        pv = view.get("preview")
        if pv:
            if pv.get("note"):
                tk.Label(col, text=pv["note"], bg=PANEL, fg=MUTED, font=f_small, anchor="w").pack(fill="x", pady=(px(6), 0))
            _text_block(col, pv["text"].rstrip("\n"), f_mono_sm, scale, 14,
                        diff=pv.get("kind") == "diff").pack(fill="x", pady=(px(4), 0))

    tk.Frame(inner, bg=BG, height=px(10)).pack(fill="x")

    # ---- footer ----------------------------------------------------------
    tk.Frame(win, bg=BORDER, height=1).pack(fill="x")
    footer = tk.Frame(win, bg=PANEL_ALT)
    footer.pack(fill="x")

    def finish(value):
        if done_event.is_set():
            return
        result_holder[0] = value
        done_event.set()
        try:
            win.destroy()
        except Exception:
            pass

    bar = tk.Frame(footer, bg=PANEL_ALT)
    bar.pack(fill="x", padx=px(18), pady=px(14))
    words = approval_view.WORDING
    hint = tk.Label(bar, text=words["hint"], bg=PANEL_ALT, fg=MUTED, font=f_small)
    hint.pack(side="left")

    approve = _flat_button(bar, words["allow"], lambda: finish(True), BRAND, "#ffffff",
                           f_body, scale, default=True)
    reject = _flat_button(bar, words["deny"], lambda: finish(False), "#262932", TEXT, f_body, scale)
    approve.pack(side="right")
    reject.pack(side="right", padx=(0, px(8)))
    # Keyboard: Tab moves between the buttons, Space/Enter press the focused one.
    for b in (approve, reject):
        b.configure(takefocus=1)
        b.bind("<space>", lambda e, b=b: b.invoke())

    # The popup takes focus when it appears; an Enter the user was already
    # typing elsewhere must not approve it. Enter only counts after a moment.
    shown_at = [None]

    def on_enter(_e):
        if shown_at[0] is None or (win.tk.call("clock", "milliseconds") - shown_at[0]) < ENTER_GUARD_MS:
            return "break"
        focused = win.focus_get()
        if focused is reject:
            finish(False)
        elif focused is approve or focused in (None, win):
            finish(True)
        # focus on the argument / diff text: Enter does nothing
        return "break"

    win.protocol("WM_DELETE_WINDOW", lambda: finish(False))
    win.bind("<Return>", on_enter)
    win.bind("<KP_Enter>", on_enter)
    win.bind("<Escape>", lambda e: finish(False))
    win.bind("<<ApprovalClose>>", lambda e: finish(False))
    for widget in (win, canvas, inner):
        widget.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))
        widget.bind("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
        widget.bind("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))

    # ---- size once, from measured content, then clamp to the work area ----
    win.update_idletasks()
    needed_list = inner.winfo_reqheight()
    chrome = header.winfo_reqheight() + footer.winfo_reqheight() + px(4)
    left, top, right, bottom = cursor_work_area(root)
    max_total = int((bottom - top) * MAX_H_FRACTION)
    list_h = min(needed_list, max(px(140), max_total - chrome))
    canvas.configure(height=list_h)

    if needed_list > list_h + 2:
        scrollbar.pack(side="right", fill="y")   # only when it earns its place
        hint.configure(text=f"{words['hint']}  ·  scroll for all {n}")

    win.update_idletasks()
    place_fully_on_screen(win, root, content_w, chrome + list_h + px(4))

    win.deiconify()
    win.attributes("-topmost", True)
    win.lift()
    try:
        win.focus_force()
        approve.focus_set()
    except Exception:
        pass
    shown_at[0] = win.tk.call("clock", "milliseconds")
    return win


def build_choice_dialog(root, question, options, result_holder, done_event):
    """Native clarification popup used only while the browser UI is hidden."""
    scale = system_scale()
    px = lambda n: int(n * scale)
    f_title = tkfont.Font(family="Segoe UI", size=11, weight="bold")
    f_body = tkfont.Font(family="Segoe UI", size=9)
    f_small = tkfont.Font(family="Segoe UI", size=8)

    win = tk.Toplevel(root)
    win.withdraw()
    win.title("FX Workspace - Clarification")
    win.configure(bg=BG)
    win.resizable(False, False)

    def finish(value=None):
        if done_event.is_set():
            return
        result_holder[0] = value
        done_event.set()
        try:
            win.grab_release()
            win.destroy()
        except Exception:
            pass

    header = tk.Frame(win, bg=PANEL_ALT)
    header.pack(fill="x")
    tk.Label(header, text="Agent needs clarification", bg=PANEL_ALT, fg=CYAN,
             font=f_small, anchor="w").pack(fill="x", padx=px(16), pady=(px(12), px(3)))
    tk.Label(header, text=question, bg=PANEL_ALT, fg=TEXT, font=f_title,
             anchor="w", justify="left", wraplength=px(390)).pack(
                 fill="x", padx=px(16), pady=(0, px(13)))
    tk.Frame(win, bg=BORDER, height=1).pack(fill="x")

    body = tk.Frame(win, bg=BG)
    body.pack(fill="both", expand=True, padx=px(14), pady=px(12))
    for option in options[:6]:
        btn = _flat_button(body, option, lambda value=option: finish(value),
                           "#262932", TEXT, f_body, scale)
        btn.pack(fill="x", pady=(0, px(6)))

    tk.Label(body, text="Something else", bg=BG, fg=MUTED,
             font=f_small, anchor="w").pack(fill="x", pady=(px(3), px(3)))
    entry_row = tk.Frame(body, bg=BG)
    entry_row.pack(fill="x")
    entry = tk.Entry(entry_row, bg=PANEL, fg=TEXT, insertbackground=TEXT,
                     relief="flat", bd=0, font=f_body)
    entry.pack(side="left", fill="x", expand=True, ipady=px(7))

    def submit_other():
        value = entry.get().strip()
        if value:
            finish(value)

    send = _flat_button(entry_row, "Send", submit_other, BRAND, "#ffffff",
                        f_body, scale, default=True)
    send.pack(side="left", padx=(px(8), 0))

    hint = tk.Label(body, text="Esc cancels", bg=BG, fg=MUTED, font=f_small, anchor="e")
    hint.pack(fill="x", pady=(px(8), 0))
    win.protocol("WM_DELETE_WINDOW", lambda: finish(None))
    win.bind("<Escape>", lambda _event: finish(None))
    entry.bind("<Return>", lambda _event: submit_other())
    entry.bind("<KP_Enter>", lambda _event: submit_other())

    win.update_idletasks()
    width = px(430)
    height = win.winfo_reqheight() + px(4)
    place_near_cursor(win, root, width, height)
    win.deiconify()
    win.attributes("-topmost", True)
    win.lift()
    try:
        win.grab_set()
        entry.focus_force()
    except Exception:
        pass
    return win


# ==============================================================================
# Single persistent GUI thread
# ==============================================================================
# Tcl/Tk is not thread-safe, and a fresh tk.Tk() per approval on a fresh thread
# is a known way to take the whole process down after a few uses. One root, one
# thread, for the life of the server; every request is marshalled over a queue
# and rendered as a Toplevel.
_gui_queue = queue.Queue()
_gui_available = threading.Event()
_gui_failed = threading.Event()


def _refuse(item) -> None:
    """A request the popup can't show: an approval is refused, a choice gets no answer."""
    payload, holder, event, _ = item
    if payload != "close" and not (isinstance(payload, dict) and payload.get("kind") == "choice"):
        holder[0] = False
    event.set()


def _gui_thread_main():
    if tk is None:
        print("[WARN] tkinter is unavailable; desktop popups disabled (the web UI still asks).")
        _gui_failed.set()
        return
    # No window until the first popup is needed. A Tk root made at startup - even withdrawn at once - could take
    # the keyboard focus from the app's own window for a moment right after it opened (1.4.2's known issue).
    first = _gui_queue.get()
    while first[0] == "close":                 # a request stopped before any popup existed: nothing to close
        first = _gui_queue.get()
    try:
        root = tk.Tk()
    except Exception as e:
        print(f"[WARN] Approval GUI unavailable ({e}); actions will auto-reject.")
        _gui_failed.set()
        _refuse(first)
        return
    root.withdraw()
    try:
        root.tk.call("tk", "scaling", system_scale() * 96.0 / 72.0)
    except Exception:
        pass
    _gui_available.set()

    windows = {}                      # id(event) -> open approval window
    waiting = [first]                 # the request that made the root, shown on the first poll

    def poll():
        try:
            while True:
                payload, holder, event, context_line = waiting.pop() if waiting else _gui_queue.get_nowait()
                try:
                    if payload == "close":                  # Stop pressed while the popup was open
                        win = windows.pop(id(event), None)
                        if win is not None:
                            try:
                                win.event_generate("<<ApprovalClose>>")
                            except Exception:
                                pass
                        continue
                    if isinstance(payload, dict) and payload.get("kind") == "choice":
                        build_choice_dialog(root, payload.get("question", "Clarify the task"),
                                            payload.get("options") or [], holder, event)
                    else:
                        windows[id(event)] = build_dialog(root, payload, holder, event, context_line)
                        for key in [k for k, w in windows.items() if not _alive(w)]:
                            windows.pop(key, None)
                except Exception as exc:
                    print(f"[WARN] Approval dialog failed: {exc}")
                    holder[0] = False
                    event.set()
        except queue.Empty:
            pass
        root.after(100, poll)

    root.after(100, poll)
    root.mainloop()


def _alive(win) -> bool:
    try:
        return bool(win.winfo_exists())
    except Exception:
        return False


_gui_thread: threading.Thread | None = None


def start():
    global _gui_thread
    _gui_thread = threading.Thread(target=_gui_thread_main, daemon=True, name="tk-gui-thread")
    _gui_thread.start()


def _gui_dead() -> bool:
    """The GUI thread died after starting (it used to leave every request
    waiting the full timeout)."""
    if _gui_thread is not None and _gui_available.is_set() and not _gui_thread.is_alive():
        _gui_failed.set()
    return _gui_failed.is_set()


def ask(actions: list, context_line: str = "", timeout: float = 600, cancelled=None) -> bool:
    """Blocks the calling FastAPI worker until the user answers the popup that
    the persistent GUI thread renders on its behalf. `cancelled()` returning
    True (the user pressed Stop) closes the popup and counts as Deny."""
    if _gui_dead():
        return False
    cancelled = cancelled or (lambda: False)
    holder, event = [False], threading.Event()
    _gui_queue.put((actions, holder, event, context_line))
    deadline = time.monotonic() + timeout
    while not event.wait(0.25):
        if cancelled() or _gui_dead():
            _gui_queue.put(("close", holder, event, ""))
            return False
        if time.monotonic() >= deadline:
            print("[WARN] Approval popup timed out; treating as denied.")
            _gui_queue.put(("close", holder, event, ""))
            return False
    return holder[0]


def ask_choice(question: str, options: list[str], timeout: float = 600):
    """Return a selected/custom answer, or None when cancelled/unavailable."""
    if _gui_failed.is_set():
        return None
    holder, event = [None], threading.Event()
    payload = {"kind": "choice", "question": question, "options": options[:6]}
    _gui_queue.put((payload, holder, event, ""))
    if not event.wait(timeout=timeout):
        print("[WARN] Clarification popup timed out.")
        return None
    return holder[0]
