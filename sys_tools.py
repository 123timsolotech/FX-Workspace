"""
sys_tools.py
============
System, process, and network utilities: Windows services, process
termination, connectivity checks, environment variables, and a live
resource snapshot. Shells out to the same built-in Windows utilities
(sc.exe, ping.exe, ipconfig, setx) a person would use themselves, plus
psutil (already a project dependency - see requirements.txt) for anything
better read directly than parsed from console output.
"""
from __future__ import annotations

import os
import re
import socket
import subprocess

try:
    import psutil
except ImportError:
    psutil = None

IS_WINDOWS = hasattr(os, "startfile")


def _run(cmd: list, timeout: float = 15.0):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def list_services(query: str = "") -> dict:
    if not IS_WINDOWS:
        return {"ok": False, "message": "Windows only."}
    try:
        res = _run(["sc", "query", "type=", "service", "state=", "all"], timeout=20)
    except Exception as e:
        return {"ok": False, "message": f"Could not query services: {e}"}
    entries = re.findall(r"SERVICE_NAME:\s*(\S+).*?STATE\s*:\s*\d+\s*(\S+)", res.stdout, re.S)
    q = (query or "").strip().lower()
    if q:
        entries = [(n, s) for n, s in entries if q in n.lower()]
    if not entries:
        return {"ok": False, "message": f"No services matched '{query}'." if query else "No services found."}
    lines = [f"{name}: {state}" for name, state in entries[:150]]
    return {"ok": True, "message": f"{len(entries)} service(s):\n" + "\n".join(lines)}


def service_control(name: str, action: str) -> dict:
    if not IS_WINDOWS:
        return {"ok": False, "message": "Windows only."}
    action = (action or "").strip().lower()
    if action not in ("start", "stop", "restart"):
        return {"ok": False, "message": "action must be start, stop, or restart."}
    if not name:
        return {"ok": False, "message": "No service name given."}
    try:
        stop_res = _run(["sc", "stop", name]) if action in ("stop", "restart") else None
        start_res = _run(["sc", "start", name]) if action in ("start", "restart") else None
    except Exception as e:
        return {"ok": False, "message": f"Service control failed: {e}"}
    if action == "stop":
        ok = stop_res.returncode == 0 or "1062" in stop_res.stdout   # already stopped
        return {"ok": ok, "message": (stop_res.stdout + stop_res.stderr).strip() or f"Stop requested for '{name}'."}
    if action == "start":
        ok = start_res.returncode == 0 or "1056" in start_res.stdout  # already running
        return {"ok": ok, "message": (start_res.stdout + start_res.stderr).strip() or f"Start requested for '{name}'."}
    ok = start_res.returncode == 0 or "1056" in start_res.stdout
    return {"ok": ok, "message": (
        f"Restart of '{name}': stop exit={stop_res.returncode}, start exit={start_res.returncode}.")}


def kill_process(name_or_pid, force: bool = False) -> dict:
    if psutil is None:
        return {"ok": False, "message": "psutil is not available."}
    try:
        pid = int(name_or_pid)
        procs = [psutil.Process(pid)] if psutil.pid_exists(pid) else []
    except (TypeError, ValueError):
        needle = str(name_or_pid).strip().lower()
        procs = [p for p in psutil.process_iter(["pid", "name"])
                 if needle and needle in (p.info.get("name") or "").lower()]
    if not procs:
        return {"ok": False, "message": f"No running process matched '{name_or_pid}'."}
    killed = []
    for p in procs:
        try:
            label = f"{p.name()} (PID {p.pid})"
            p.kill() if force else p.terminate()
            killed.append(label)
        except Exception as e:
            return {"ok": False, "message": f"Could not stop {p}: {e}"}
    return {"ok": True, "message": f"{'Killed' if force else 'Terminated'}: {', '.join(killed)}."}


def _not_through_route(host: str) -> str | None:
    """Why a direct probe of host (ping, a port check) must not run: ping can't use a
    proxy or Tor, so with one on it would go straight from this PC; and the network
    layer's gate (kill switch, Require VPN, offline work). None when it may."""
    import netlayer
    if netlayer.is_local(host):
        return None
    route = netlayer.route_for("other")
    if route.routed:
        return (f"Not run: {route.label} is on, and a ping or port check can't go through it - it would go straight "
                "from this PC (Settings > Network).")
    try:
        netlayer.check(route, "other", host)
    except netlayer.RouteBlocked as e:
        return f"Not run: {e}"
    return None


def ping_host(host: str, count: int = 4) -> dict:
    if not host:
        return {"ok": False, "message": "No host given."}
    why = _not_through_route(host)
    if why:
        return {"ok": False, "status": "permanent_failure", "message": why}
    count = max(1, min(int(count or 4), 10))
    cmd = ["ping", "-n" if IS_WINDOWS else "-c", str(count), host]
    try:
        res = _run(cmd, timeout=count * 3 + 5)
    except Exception as e:
        return {"ok": False, "message": f"Ping failed: {e}"}
    return {"ok": res.returncode == 0, "message": (res.stdout or res.stderr).strip()[-1500:]}


def network_info() -> dict:
    lines = []
    if psutil:
        for iface, addrs in psutil.net_if_addrs().items():
            ipv4 = next((a.address for a in addrs if a.family == socket.AF_INET), None)
            if ipv4:
                lines.append(f"{iface}: {ipv4}")
    if not lines and IS_WINDOWS:
        try:
            res = _run(["ipconfig"], timeout=10)
            return {"ok": True, "message": res.stdout[-2500:]}
        except Exception as e:
            return {"ok": False, "message": f"Could not read network info: {e}"}
    if not lines:
        return {"ok": False, "message": "No network interfaces reported."}
    return {"ok": True, "message": "\n".join(lines)}


SECRET_NAME_RE = re.compile(r"key|token|secret|passw|pwd|credential|auth|cookie|session", re.I)


def get_env_var(name: str) -> dict:
    if not name:
        return {"ok": False, "message": "No variable name given."}
    value = os.environ.get(name)
    if value is None:
        return {"ok": False, "message": f"'{name}' is not set in this process's environment."}
    # get_env_var runs without approval; secrets (API keys, tokens, passwords)
    # must never reach the model or the transcript.
    if SECRET_NAME_RE.search(name):
        return {"ok": True, "message": f"{name} is set ({len(value)} characters; the value is hidden because it looks like a secret)."}
    return {"ok": True, "message": f"{name}={value}"}


def set_env_var(name: str, value: str, persist: bool = False) -> dict:
    if not name:
        return {"ok": False, "message": "No variable name given."}
    os.environ[name] = value or ""
    if persist and IS_WINDOWS:
        try:
            _run(["setx", name, value or ""])
        except Exception as e:
            return {"ok": True, "message": (
                f"Set '{name}' for THIS process only; persisting it for future processes failed: {e}")}
        return {"ok": True, "message": (
            f"Set '{name}' for this process, and persisted it for future processes (setx). "
            "Already-open programs will not see it until they restart.")}
    return {"ok": True, "message": f"Set '{name}' for this process only (not persisted)."}


def get_system_info() -> dict:
    if psutil is None:
        return {"ok": False, "message": "psutil is not available."}
    cpu = psutil.cpu_percent(interval=0.3)
    mem = psutil.virtual_memory()
    parts = [f"CPU: {cpu:.0f}%",
             f"RAM: {mem.percent:.0f}% used ({mem.used / (1024 ** 3):.1f} / {mem.total / (1024 ** 3):.1f} GB)"]
    try:
        battery = psutil.sensors_battery()
        if battery:
            parts.append(f"Battery: {battery.percent:.0f}%{' (charging)' if battery.power_plugged else ''}")
    except Exception:
        pass
    try:
        import shutil as _shutil
        usage = _shutil.disk_usage(os.path.abspath(os.sep))
        parts.append(f"System drive: {usage.free / (1024 ** 3):.1f} GB free of {usage.total / (1024 ** 3):.1f} GB")
    except Exception:
        pass
    return {"ok": True, "message": " | ".join(parts)}


def port_check(host: str, port, timeout: float = 3.0) -> dict:
    if not host or not port:
        return {"ok": False, "message": "host and port are required."}
    why = _not_through_route(host)
    if why:
        return {"ok": False, "status": "permanent_failure", "message": why}
    import netlayer
    try:
        with netlayer.component("other"), socket.create_connection((host, int(port)), timeout=timeout):
            return {"ok": True, "message": f"{host}:{port} is OPEN (accepted a connection)."}
    except Exception as e:
        return {"ok": False, "message": f"{host}:{port} is not reachable: {e}"}


def top_processes(limit: int = 10, sort: str = "memory") -> dict:
    """Heaviest processes by memory (default) or CPU, aggregated per app name
    so 30 browser helper processes read as one line."""
    if psutil is None:
        return {"ok": False, "message": "psutil is not available."}
    limit = max(1, min(int(limit or 10), 30))
    by_name: dict[str, dict] = {}
    procs = list(psutil.process_iter(["name", "memory_info"]))
    for p in procs:
        try:
            p.cpu_percent(None)             # prime per-process CPU counters
        except Exception:
            pass
    import time as _time
    _time.sleep(0.4)
    cores = psutil.cpu_count() or 1
    for p in procs:
        try:
            name = p.info.get("name") or "?"
            mem = (p.info.get("memory_info").rss if p.info.get("memory_info") else 0) / (1024 ** 2)
            cpu = p.cpu_percent(None) / cores
        except Exception:
            continue
        entry = by_name.setdefault(name, {"name": name, "mem_mb": 0.0, "cpu": 0.0, "count": 0})
        entry["mem_mb"] += mem
        entry["cpu"] += cpu
        entry["count"] += 1
    key = "cpu" if str(sort).lower().startswith("c") else "mem_mb"
    ranked = sorted(by_name.values(), key=lambda e: e[key], reverse=True)[:limit]
    lines = [f"{e['name']}{' (x' + str(e['count']) + ')' if e['count'] > 1 else ''}: "
             f"{e['mem_mb'] / 1024:.2f} GB RAM, {e['cpu']:.1f}% CPU" for e in ranked]
    return {"ok": True, "message": f"Top {len(ranked)} by {'CPU' if key == 'cpu' else 'memory'}:\n" + "\n".join(lines)}
