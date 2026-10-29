"""
productivity_tools.py
======================
Notifications, one-off scheduled reminders/commands (via Windows Task
Scheduler), a lightweight in-session clipboard history, and saving a
screenshot straight to a file.

No new dependency is introduced. Notifications reuse .NET's
System.Windows.Forms.NotifyIcon through a short PowerShell one-liner
(PowerShell + .NET Framework ship with Windows); scheduling shells out to
schtasks.exe the same way sys_tools.py shells out to sc.exe/ping.exe;
screenshots reuse desktop_input.capture_screenshot(), the exact same
capture code elements.py/vision.py already rely on.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from datetime import datetime, timedelta

import desktop_input as di

IS_WINDOWS = hasattr(os, "startfile")

_TASK_PREFIX = "AgenticWorkspace_"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
# schtasks refuses a /TR (the command it runs) longer than this.
TR_MAX = 261
REPEATS = ("once", "daily", "weekdays", "weekly")
_DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
# Notification scripts are written here and run with -File: the one-liner was
# longer than /TR allows and broke on a " in the message.
SCRIPT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "scheduled")


def _run(cmd: list, timeout: float = 15.0):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _ps_escape(text: str) -> str:
    return (text or "").replace("'", "''")


def _balloon_script(title: str, message: str, duration_ms: int) -> str:
    return (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$n = New-Object System.Windows.Forms.NotifyIcon; "
        "$n.Icon = [System.Drawing.SystemIcons]::Information; "
        "$n.Visible = $true; "
        f"$n.ShowBalloonTip({duration_ms}, '{_ps_escape(title)}', '{_ps_escape(message)}', "
        "[System.Windows.Forms.ToolTipIcon]::Info); "
        f"Start-Sleep -Milliseconds {duration_ms + 500}; "
        "$n.Dispose()"
    )


def send_notification(title: str, message: str, duration_seconds: float = 8) -> dict:
    if not IS_WINDOWS:
        return {"ok": False, "message": "Windows only."}
    duration_ms = max(3, min(int(duration_seconds or 8), 30)) * 1000
    script = _balloon_script(title or "FX Workspace", message or "", duration_ms)
    try:
        subprocess.Popen(["powershell", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
                          creationflags=_NO_WINDOW)
        return {"ok": True, "message": f"Showed notification '{title}'."}
    except Exception as e:
        return {"ok": False, "message": f"Could not show notification: {e}"}


def _short_date_pattern() -> str:
    """The user's Windows short-date format (e.g. 'M/d/yyyy', 'dd/MM/yyyy').
    schtasks reads /sd in this order, not always month-first."""
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\International") as key:
            return str(winreg.QueryValueEx(key, "sShortDate")[0])
    except Exception:
        return "M/d/yyyy"


def schtasks_date(when: datetime, pattern: str | None = None) -> str:
    """/sd for schtasks in the locale's day/month/year order (zero-padded, '/')."""
    pattern = (pattern or _short_date_pattern()).lower()
    order = sorted((pos, part) for part in "dmy" for pos in [pattern.find(part)] if pos >= 0)
    parts = [p for _pos, p in order] if len(order) == 3 else ["m", "d", "y"]
    fmt = {"d": "%d", "m": "%m", "y": "%Y"}
    return "/".join(when.strftime(fmt[p]) for p in parts)


def _script_path(name: str) -> str:
    return os.path.join(SCRIPT_DIR, re.sub(r"[^\w\-.]", "_", name) + ".ps1")


def schedule_task(name: str, delay_seconds: int = None, run_at: str = None,
                   command: str = None, message: str = None, repeat: str = "once") -> dict:
    """Creates a Windows Task Scheduler entry that either runs 'command' or
    pops a notification with 'message', at either 'run_at' ("YYYY-MM-DD
    HH:MM") or 'delay_seconds' from now. `repeat`: once (default), daily,
    weekdays (Mon-Fri) or weekly (on run_at's weekday), at that time of day.
    Persists across a reboot, unlike an in-process timer."""
    if not IS_WINDOWS:
        return {"ok": False, "message": "Windows only."}
    if not name or not re.fullmatch(r"[\w\-. ]{1,80}", name):
        return {"ok": False, "message": "name must be 1-80 plain characters (letters/digits/space/-/_/.)."}
    if not command and not message:
        return {"ok": False, "message": (
            "Provide either 'command' to run, or 'message' to show as a notification, at the "
            "scheduled time.")}
    repeat = (repeat or "once").strip().lower()
    if repeat not in REPEATS:
        return {"ok": False, "message": f"repeat must be one of: {', '.join(REPEATS)}."}

    if run_at:
        try:
            when = datetime.strptime(run_at, "%Y-%m-%d %H:%M")
        except ValueError:
            return {"ok": False, "message": "run_at must look like 'YYYY-MM-DD HH:MM'."}
    else:
        delay_seconds = int(delay_seconds or 0)
        if delay_seconds < 30:
            return {"ok": False, "message": "delay_seconds must be at least 30, or pass run_at instead."}
        when = datetime.now() + timedelta(seconds=delay_seconds)

    task_name = _TASK_PREFIX + name
    script_file = None
    if command:
        action = command
    else:
        script_file = _script_path(name)
        try:
            os.makedirs(SCRIPT_DIR, exist_ok=True)
            with open(script_file, "w", encoding="utf-8-sig") as f:
                f.write(_balloon_script(name or "Reminder", message or "", 10000) + "\n")
        except OSError as e:
            return {"ok": False, "message": f"Could not write the reminder script: {e}"}
        action = f'powershell -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File "{script_file}"'
    if len(action) > TR_MAX:
        return {"ok": False, "message": (
            f"The command is {len(action)} characters; Windows Task Scheduler accepts at most {TR_MAX}. "
            "Put the commands in a .ps1 file and schedule 'powershell -NoProfile -File <that file>' instead.")}

    at = when.strftime("%H:%M")
    if repeat == "once":
        when_args = ["/sc", "once", "/st", at, "/sd", schtasks_date(when)]
        described = f"for {when.strftime('%Y-%m-%d %H:%M')}"
    elif repeat == "daily":
        when_args, described = ["/sc", "daily", "/st", at], f"every day at {at}"
    elif repeat == "weekdays":
        when_args, described = ["/sc", "weekly", "/d", "MON,TUE,WED,THU,FRI", "/st", at], f"every weekday at {at}"
    else:
        day = _DAYS[when.weekday()]
        when_args, described = ["/sc", "weekly", "/d", day, "/st", at], f"every {when.strftime('%A')} at {at}"
    try:
        res = _run(["schtasks", "/create", "/tn", task_name, "/tr", action] + when_args + ["/f"])
    except Exception as e:
        return {"ok": False, "message": f"Could not create scheduled task: {e}"}
    if res.returncode != 0:
        if script_file:
            try:
                os.remove(script_file)
            except OSError:
                pass
        return {"ok": False, "message": (res.stdout + res.stderr).strip()}
    return {"ok": True, "message": f"Scheduled '{name}' {described}.", "task": task_name, "repeat": repeat}


def cancel_scheduled_task(name: str) -> dict:
    if not IS_WINDOWS:
        return {"ok": False, "message": "Windows only."}
    if not name:
        return {"ok": False, "message": "No task name given."}
    try:
        res = _run(["schtasks", "/delete", "/tn", _TASK_PREFIX + name, "/f"])
    except Exception as e:
        return {"ok": False, "message": f"Could not cancel task: {e}"}
    if res.returncode != 0:
        return {"ok": False, "message": (res.stdout + res.stderr).strip() or f"'{name}' was not found."}
    try:
        os.remove(_script_path(name))           # a reminder's script, if it had one
    except OSError:
        pass
    return {"ok": True, "message": f"Cancelled scheduled task '{name}'."}


def list_scheduled_tasks(query: str = "") -> dict:
    if not IS_WINDOWS:
        return {"ok": False, "message": "Windows only."}
    try:
        res = _run(["schtasks", "/query", "/fo", "LIST"], timeout=20)
    except Exception as e:
        return {"ok": False, "message": f"Could not list tasks: {e}"}
    names = []
    for block in res.stdout.split("\n\n"):
        m = re.search(r"TaskName:\s*\\?(.+)", block)
        if m and _TASK_PREFIX in m.group(1):
            plain = m.group(1).split(_TASK_PREFIX, 1)[-1].strip()
            if not query or query.lower() in plain.lower():
                names.append(plain)
    if not names:
        return {"ok": False, "message": "No agent-scheduled tasks found."}
    return {"ok": True, "message": f"{len(names)} scheduled: " + ", ".join(names)}


# ---------------------------------------------------------------------------
# Clipboard history. There is no background polling thread in this build, so
# entries are recorded opportunistically: main.py calls record_clipboard_
# sample() every time the agent itself reads or writes the clipboard via the
# existing get_clipboard/set_clipboard tools. This intentionally will not
# see clipboard changes the human makes outside the agent - it is a log of
# what the AGENT has seen, not a system-wide clipboard monitor.
# ---------------------------------------------------------------------------
_CLIPBOARD_HISTORY: list = []
_CLIPBOARD_HISTORY_MAX = 50


def record_clipboard_sample(text: str) -> None:
    text = text or ""
    if not text:
        return
    if _CLIPBOARD_HISTORY and _CLIPBOARD_HISTORY[-1]["text"] == text:
        return
    _CLIPBOARD_HISTORY.append({"text": text, "at": time.time()})
    del _CLIPBOARD_HISTORY[:-_CLIPBOARD_HISTORY_MAX]


def clipboard_history(limit: int = 20) -> dict:
    if not _CLIPBOARD_HISTORY:
        return {"ok": False, "message": (
            "No clipboard history recorded yet in this session (only entries the agent itself read "
            "or wrote via get_clipboard/set_clipboard are tracked - this is not a system-wide "
            "clipboard monitor).")}
    limit = max(1, min(int(limit or 20), _CLIPBOARD_HISTORY_MAX))
    recent = list(reversed(_CLIPBOARD_HISTORY[-limit:]))
    lines = [f"{time.strftime('%H:%M:%S', time.localtime(e['at']))}  {e['text'][:200]}" for e in recent]
    return {"ok": True, "message": "\n".join(lines)}


def screenshot_to_file(filename: str = None, window_title: str = None) -> dict:
    found = di.find_window(window_title) if window_title else None
    if found:
        # Observational: bring it forward for the capture only, then put the
        # user's foreground window and every placement back.
        import window_state
        with window_state.preserve_windows(found[0]):
            window_state.bring_forward(found[0])
            time.sleep(0.2)
            img, meta = di.capture_screenshot()
    else:
        img, meta = di.capture_screenshot()
    if img is None:
        return {"ok": False, "message": "Could not capture the screen."}
    if not filename:
        filename = f"screenshot_{time.strftime('%Y%m%d_%H%M%S')}.png"
    dest = os.path.abspath(os.path.expanduser(filename))
    try:
        os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
        img.save(dest)
    except Exception as e:
        return {"ok": False, "message": f"Could not save screenshot: {e}"}
    return {"ok": True, "message": f"Saved a {meta['orig_w']}x{meta['orig_h']} screenshot to '{dest}'."}
