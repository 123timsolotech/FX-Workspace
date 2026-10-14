"""
studio_mcp.py
=============
Roblox's own Studio MCP server (StudioMCP.exe - Roblox installs it with Studio and starts it through
%LOCALAPPDATA%\\Roblox\\mcp.bat), connected in Settings > MCP like any other MCP server (mcp_client.py).
Its tools - execute_luau, multi_edit, insert_asset and the rest - need two routing arguments on every
call: studio_id (which Studio window) and datamodel_type (Edit; Server or Client during a play test). A
model would spend two extra calls on finding them out (list_roblox_studios, get_studio_state) before the
one that does the work, and small models get them wrong. With one Studio open there is one right answer:

  * fill_args     fills them in before validation and the approval card (agent/loop.py): studio_id only
                  when exactly one Studio is open, datamodel_type "Edit" when it is missing or not valid.
  * context_line  says which place is open, beside CURRENT REQUEST (agent/models.build_messages).

A server counts as Roblox's when it offers both list_roblox_studios and execute_luau. The lookup here is
list_roblox_studios - read-only, called by the app itself (like tools/list), so it doesn't go to the
approval card; every call the MODEL makes still does (MCP tools ask by default).
"""
from __future__ import annotations

import json
import threading
import time

import core.state as shared
import mcp_client

MARKERS = {"list_roblox_studios", "execute_luau"}
TTL = 8.0                            # seconds a list of open Studios is reused
# When a local model's window can't hold all of its tools (tools/schemas._fit_local), these go in first,
# in this order: doing the work, then reading the place and its scripts. Its extras - keyboard and mouse
# input, AI mesh/material generation, subagents, image upload - only when there is room.
CORE = ("execute_luau", "multi_edit", "insert_asset", "get_studio_state", "search_game_tree", "inspect_instance",
        "script_read", "script_search", "script_grep", "get_console_output", "start_stop_play", "search_asset",
        "screen_capture")
_cache: dict = {}                    # server -> (at, [{id, name}])
_lock = threading.Lock()


def _hub(hub=None):
    return hub if hub is not None else getattr(shared, "mcp_hub", None)


def _tools(server: str, hub) -> list:
    s = hub.servers.get(server) if hub is not None else None
    return list(s.tools) if s is not None and s.status == "connected" else []


def studio_servers(hub=None) -> list:
    """Connected servers that are Roblox's Studio MCP."""
    hub = _hub(hub)
    if hub is None:
        return []
    return [name for name in list(hub.servers) if MARKERS <= {t.get("name") for t in _tools(name, hub)}]


def execute_tool(names) -> str | None:
    """The registered execute_luau of Roblox's Studio MCP among these tool names, or None."""
    return next((n for n in names if isinstance(n, str) and n.startswith("mcp__") and n.endswith("__execute_luau")), None)


def priority(name: str, hub=None) -> int:
    """Sort key for fitting MCP tools into a small window: Roblox's Studio MCP core first (CORE order),
    then other servers' tools in their own order, then Studio's extras."""
    parts = mcp_client.split_name(name) if isinstance(name, str) else None
    if not parts:
        return 1000
    if parts[0] in studio_servers(hub):
        return CORE.index(parts[1]) if parts[1] in CORE else 500
    return 100


def studios(server: str, hub=None) -> list:
    """[{id, name}] - the Studio windows that server sees (cached for a few seconds)."""
    hub = _hub(hub)
    now = time.time()
    with _lock:
        hit = _cache.get(server)
        if hit and now - hit[0] < TTL:
            return hit[1]
    found = []
    try:
        res = hub.call(mcp_client.tool_name(server, "list_roblox_studios"), {})
        data = json.loads(res.get("message") or "{}") if res.get("ok") else {}
        found = [{"id": str(s["id"]), "name": str(s.get("name") or "")}
                 for s in (data.get("studios") or []) if isinstance(s, dict) and s.get("id")]
    except (ValueError, TypeError, AttributeError, KeyError):
        found = []
    with _lock:
        _cache[server] = (now, found)
    return found


def fill_args(name: str, args, hub=None):
    """The model's arguments, with studio_id / datamodel_type filled in where there is one right answer.
    Anything else - and every call that isn't to Roblox's Studio MCP - comes back unchanged."""
    if not isinstance(name, str) or not isinstance(args, dict) or not name.startswith("mcp__"):
        return args
    hub = _hub(hub)
    parts = mcp_client.split_name(name)
    if hub is None or not parts or parts[0] not in studio_servers(hub):
        return args
    server = parts[0]
    tool = next((t for t in _tools(server, hub) if mcp_client.tool_name(server, t.get("name") or "") == name), None)
    props = ((tool or {}).get("inputSchema") or {}).get("properties") or {}
    out = dict(args)
    if "studio_id" in props:
        open_now = studios(server, hub)
        if len(open_now) == 1 and out.get("studio_id") not in {s["id"] for s in open_now}:
            out["studio_id"] = open_now[0]["id"]
    if "datamodel_type" in props:
        kinds = (props.get("datamodel_type") or {}).get("enum") or []
        value = out.get("datamodel_type")
        if not value or (kinds and value not in kinds):
            out["datamodel_type"] = "Edit"
    return out


def context_line(hub=None) -> str:
    """"ROBLOX STUDIO (MCP): 'Scratch Test' is open - ..." for the turn header, or "" when no Studio MCP."""
    hub = _hub(hub)
    servers = studio_servers(hub)
    if not servers:
        return ""
    open_now = studios(servers[0], hub)
    if not open_now:
        return ("ROBLOX STUDIO (MCP): connected, but no Studio window is open with it - ask the user to open their "
                "place in Studio.")
    if len(open_now) == 1:
        return (f"ROBLOX STUDIO (MCP): '{open_now[0]['name']}' is open - studio_id and datamodel_type are filled in "
                "for you, so the tool that does the work can be your first call.")
    names = "; ".join(f"{s['name']} = {s['id']}" for s in open_now[:6])
    return f"ROBLOX STUDIO (MCP): {len(open_now)} Studio windows are open - pass the studio_id of the right one ({names})."
