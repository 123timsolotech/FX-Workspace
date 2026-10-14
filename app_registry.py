"""
app_registry.py
================
Finds what's actually installed (Start Menu / Desktop shortcuts, registry
App Paths, Store apps) and fuzzy-resolves a loose app name against it, so
the agent doesn't need to guess an exact executable name. Unchanged in
spirit from the previous build - this part wasn't reported as broken.
"""
import difflib
import json
import os
import subprocess
import time

from desktop_input import IS_WINDOWS

APP_REGISTRY = {"apps": {}, "last_scan": 0}
APP_REGISTRY_TTL = 600


def _scan_shortcuts_and_exes():
    found = {}
    search_dirs = [
        os.path.expandvars(r"%ProgramData%\Microsoft\Windows\Start Menu\Programs"),
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
        os.path.expandvars(r"%USERPROFILE%\Desktop"),
        os.path.expandvars(r"%PUBLIC%\Desktop"),
    ]
    for d in search_dirs:
        if not os.path.exists(d):
            continue
        for root_dir, _, files in os.walk(d):
            for f in files:
                if f.lower().endswith((".lnk", ".exe")):
                    name = os.path.splitext(f)[0]
                    found.setdefault(name.lower(), {"display": name, "path": os.path.join(root_dir, f), "kind": "shortcut"})
    return found


def _scan_app_paths_registry():
    found = {}
    if not IS_WINDOWS:
        return found
    try:
        import winreg
    except ImportError:
        return found
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths") as key:
                i = 0
                while True:
                    try:
                        name = winreg.EnumKey(key, i)
                    except OSError:
                        break
                    i += 1
                    try:
                        with winreg.OpenKey(key, name) as sub:
                            path, _ = winreg.QueryValueEx(sub, None)
                            display = os.path.splitext(name)[0]
                            found[display.lower()] = {"display": display, "path": path, "kind": "registry"}
                    except OSError:
                        continue
        except OSError:
            continue
    return found


def _scan_uwp_apps():
    found = {}
    if not IS_WINDOWS:
        return found
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command", "Get-StartApps | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=15,
        )
        data = json.loads(res.stdout) if res.stdout.strip() else []
        if isinstance(data, dict):
            data = [data]
        for item in data:
            name, app_id = item.get("Name"), item.get("AppID")
            if name and app_id:
                found[name.lower()] = {"display": name, "path": f"shell:appsFolder\\{app_id}", "kind": "uwp"}
    except Exception:
        pass
    return found


def refresh_app_registry(force=False):
    now = time.time()
    if not force and APP_REGISTRY["apps"] and (now - APP_REGISTRY["last_scan"] < APP_REGISTRY_TTL):
        return APP_REGISTRY["apps"]
    apps = {}
    apps.update(_scan_shortcuts_and_exes())
    apps.update(_scan_app_paths_registry())
    apps.update(_scan_uwp_apps())
    APP_REGISTRY["apps"], APP_REGISTRY["last_scan"] = apps, now
    return apps


def resolve_app(app_name: str, top_n=5):
    apps = refresh_app_registry()
    needle = app_name.lower().strip()
    if needle in apps:
        return apps[needle], []
    substr_hits = [v for k, v in apps.items() if needle in k]
    if len(substr_hits) == 1:
        return substr_hits[0], []
    # Fuzzy matches are suggestions only. Automatically launching the sole
    # fuzzy result turned malformed compound targets into unrelated apps
    # (for example Remote Desktop Connection). Exact/unique substring matches
    # above remain automatic; a typo requires a clarification.
    close_keys = difflib.get_close_matches(needle, apps.keys(), n=top_n, cutoff=0.72)
    candidates = [apps[k] for k in close_keys] or substr_hits[:top_n]
    return None, candidates


def set_clipboard(text: str):
    subprocess.run(["powershell", "-NoProfile", "-Command", "Set-Clipboard -Value $input"],
                    input=text, text=True, timeout=10, capture_output=True)


def get_clipboard() -> str:
    res = subprocess.run(["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"],
                          capture_output=True, text=True, timeout=10)
    return (res.stdout or "").rstrip("\r\n")


# ==============================================================================
# App adapters: the one interface a creative app's integration implements
# ==============================================================================
# Roblox Studio implements it (roblox_adapter.py). Blender and Figma are meant
# to implement the same class later; they have no adapter yet, so no tool,
# status row or button exists for them (ARCHITECTURE.md, "Deferred").
#
# An adapter supplies app knowledge only. Its tools are declared like every
# other tool (tools/schemas.py, tool_registry.py, permissions.py) and run
# through the one path: permissions.explain -> approval card -> ToolExecutor
# (validation, timeouts, undo journal, Activity events) -> tools/dispatch.py,
# which hands the call to the adapter. An adapter never approves, skips an
# approval or journals behind the executor's back.
from abc import ABC, abstractmethod


class AdapterUnavailable(OSError):
    """The app is not running / its bridge is not connected."""


class AppAdapter(ABC):
    app_id: str = ""               # "roblox", later "blender", "figma"
    display_name: str = ""         # "Roblox Studio"
    process_names: tuple = ()      # executables that mean the app is running
    tool_prefix: str = ""          # every tool this adapter serves starts with it ("roblox_")

    # ---- connection -------------------------------------------------------
    @abstractmethod
    def status(self) -> dict:
        """Cheap, never blocks: {"connected": bool, "detail": str, ...}."""

    def health_check(self, timeout: float = 5.0) -> dict:
        """An active round trip to the app: {"ok", "latency_ms", "message"}."""
        st = self.status()
        return {"ok": bool(st.get("connected")), "latency_ms": None, "message": st.get("detail", "")}

    # ---- tools ------------------------------------------------------------
    def serves(self, tool_name: str) -> bool:
        return bool(self.tool_prefix) and tool_name.startswith(self.tool_prefix)

    @abstractmethod
    def call(self, tool_name: str, args: dict, journal=None, cancelled=None) -> dict:
        """Run one tool. Returns the raw result dict every tool returns
        ({"ok", "message", "data"?, "verified"?}). `journal` is the undo
        Change the executor opened (undoable tools only): the adapter records
        what it is about to change through journal.external(...) BEFORE
        changing it. `cancelled()` becomes true when the user presses Stop."""

    # ---- approvals and undo for things that live inside the app -----------
    def preview(self, tool_name: str, args: dict) -> dict | None:
        """What the approval card shows for this call (a diff for an edit),
        plus {"target", "fingerprint"} so a change made after approval is
        caught before running. None: nothing to preview."""
        return None

    def fingerprint(self, ref: str) -> str | None:
        """The current state of an artifact (e.g. a script's source hash);
        None when it does not exist. Raises AdapterUnavailable."""
        raise AdapterUnavailable(f"{self.display_name} cannot fingerprint {ref}.")

    def restore(self, ref: str, content: str | None, expected: str | None):
        """Put an artifact back (content None: remove what the change created).
        `expected` is its fingerprint right now if the undo is not forced.
        Raises AdapterUnavailable or undo_store.UndoConflict."""
        raise AdapterUnavailable(f"{self.display_name} cannot restore {ref}.")


_ADAPTERS: dict = {}


def register_adapter(adapter: AppAdapter) -> AppAdapter:
    _ADAPTERS[adapter.app_id] = adapter
    return adapter


def get_adapter(app_id: str) -> AppAdapter | None:
    return _ADAPTERS.get(app_id)


def adapters() -> list:
    return list(_ADAPTERS.values())


def adapter_for_tool(tool_name: str) -> AppAdapter | None:
    return next((a for a in _ADAPTERS.values() if a.serves(tool_name)), None)
