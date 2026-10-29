"""
sandbox.py
==========
Sandboxed chats (1.4.2): for a risky task, switch the chat to "sandboxed" (the context meter's popover).

  Files     the file tools reach only this chat's scratch folder (data/sandbox/<chat>); a relative name lands there,
            anything outside is refused. sandbox_copy_out brings a result out - it always asks first.
  Commands  PowerShell, cmd and Python start suspended, join a Windows job object (2 GB of memory, half the CPU,
            killed with the job) and drop to a LOW-integrity token before they run: they can't change your files,
            the registry or other programs - only the scratch folder, which is labelled low-integrity for them.
  Screen    a sandboxed chat doesn't click, type or use the clipboard (schemas.session_off).

Windows Sandbox (a throwaway virtual PC, Windows 10/11 Pro) runs an unknown program or script with only its own
folder shared, read-only: windows_sandbox_run. It needs the Windows feature switched on once (admin, then a restart);
the tool says how when it's off.
"""
from __future__ import annotations

import ctypes
import os
import re
import shutil
import subprocess

import core.state as shared

ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "sandbox")
MEMORY_LIMIT = 2 * 1024 ** 3
CPU_PERCENT = 50
PATH_KEYS = ("filename", "path", "source", "destination", "folder", "directory", "dir", "file", "src", "dst",
             "target", "output", "output_path", "zip_path", "dest", "paths", "files", "first", "second")
SCREEN_GROUPS = frozenset({"desktop", "clipboard"})
EXEMPT = {"sandbox_copy_out"}
WSB_EXE = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "System32", "WindowsSandbox.exe")


def active() -> bool:
    return bool((shared.app_state.get("active_chat") or {}).get("sandboxed"))


def folder(create: bool = True) -> str:
    chat = shared.app_state.get("active_chat") or {}
    name = re.sub(r"[^a-f0-9]", "", str(chat.get("id") or "")) or "new-chat"
    path = os.path.join(ROOT, name)
    if create and not os.path.isdir(path):
        os.makedirs(path, exist_ok=True)
        if os.name == "nt":                       # low-integrity processes may write here, and only here
            r = subprocess.run(["icacls", path, "/setintegritylevel", "(OI)(CI)L"], capture_output=True,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if r.returncode:
                import events as event_log
                event_log.log_action(f"Sandbox: couldn't open {path} to sandboxed commands - they can read it "
                                     "but not write to it.")
    return path


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(root)]) == os.path.realpath(root)
    except ValueError:
        return False


def confine(tool: str, args: dict) -> tuple[dict, str | None]:
    """(args with relative paths moved into the scratch folder, refusal or None) for one tool call."""
    if not active() or tool in EXEMPT:
        return args, None
    from tools.schemas import tool_group
    group = tool_group(tool)
    if group in SCREEN_GROUPS:
        return args, "This chat is sandboxed: it doesn't control the screen, keyboard or clipboard."
    if group != "files" and tool not in ("search_files", "file_info", "compare_files", "zip_paths", "unzip_file"):
        return args, None
    root = folder()
    out = dict(args or {})
    for key in PATH_KEYS:
        if key not in out or out[key] in (None, ""):
            continue
        values = out[key] if isinstance(out[key], list) else [out[key]]
        fixed = []
        for v in values:
            p = os.path.expanduser(str(v))
            p = p if os.path.isabs(p) else os.path.join(root, p)
            if not _inside(p, root):
                return args, (f"This chat is sandboxed: files only in its scratch folder ({root}). '{v}' is outside it "
                              "- work on a copy there, or switch the sandbox off for this chat.")
            fixed.append(p)
        out[key] = fixed if isinstance(out[key], list) else fixed[0]
    return out, None


# ---- restricted processes (Windows) ------------------------------------------------------------------------------
CREATE_SUSPENDED = 0x4
_JOBS: dict = {}


class _SidAndAttributes(ctypes.Structure):
    _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.c_ulong)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]


class _BasicLimit(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                ("LimitFlags", ctypes.c_ulong), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_ulong),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_ulong), ("SchedulingClass", ctypes.c_ulong)]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _CpuRate(ctypes.Structure):
    _fields_ = [("ControlFlags", ctypes.c_ulong), ("CpuRate", ctypes.c_ulong)]


class _StartupInfo(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("lpReserved", ctypes.c_wchar_p), ("lpDesktop", ctypes.c_wchar_p),
                ("lpTitle", ctypes.c_wchar_p), ("dwX", ctypes.c_ulong), ("dwY", ctypes.c_ulong),
                ("dwXSize", ctypes.c_ulong), ("dwYSize", ctypes.c_ulong), ("dwXCountChars", ctypes.c_ulong),
                ("dwYCountChars", ctypes.c_ulong), ("dwFillAttribute", ctypes.c_ulong), ("dwFlags", ctypes.c_ulong),
                ("wShowWindow", ctypes.c_ushort), ("cbReserved2", ctypes.c_ushort), ("lpReserved2", ctypes.c_void_p),
                ("hStdInput", ctypes.c_void_p), ("hStdOutput", ctypes.c_void_p), ("hStdError", ctypes.c_void_p)]


class _ProcessInfo(ctypes.Structure):
    _fields_ = [("hProcess", ctypes.c_void_p), ("hThread", ctypes.c_void_p), ("dwProcessId", ctypes.c_ulong),
                ("dwThreadId", ctypes.c_ulong)]


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("nLength", ctypes.c_ulong), ("lpSecurityDescriptor", ctypes.c_void_p), ("bInheritHandle", ctypes.c_int)]


def _k32():
    k32 = ctypes.windll.kernel32
    for name in ("CreateJobObjectW", "GetCurrentProcess", "CreateFileW"):
        getattr(k32, name).restype = ctypes.c_void_p
    return k32


def _low_token():
    """A copy of this process's token at Low integrity, for a new process (CreateProcessAsUserW)."""
    k32, adv = _k32(), ctypes.windll.advapi32
    mine, dup = ctypes.c_void_p(), ctypes.c_void_p()
    if not adv.OpenProcessToken(ctypes.c_void_p(k32.GetCurrentProcess()), 0x0002 | 0x0008 | 0x0001 | 0x0080 | 0x0100,
                                ctypes.byref(mine)):          # DUPLICATE | QUERY | ASSIGN_PRIMARY | ADJUST_DEFAULT | SESSIONID
        raise OSError("no token")
    try:
        if not adv.DuplicateTokenEx(mine, 0x02000000, None, 2, 1, ctypes.byref(dup)):   # MAXIMUM_ALLOWED, Impersonation, Primary
            raise OSError("couldn't copy the token")
    finally:
        k32.CloseHandle(mine)
    sid = ctypes.c_void_p()
    adv.ConvertStringSidToSidW(ctypes.c_wchar_p("S-1-16-4096"), ctypes.byref(sid))      # Low Mandatory Level
    label = _SidAndAttributes(sid, 0x20)                                                 # SE_GROUP_INTEGRITY
    ok = adv.SetTokenInformation(dup, 25, ctypes.byref(label), ctypes.sizeof(label) + adv.GetLengthSid(sid))
    k32.LocalFree(sid)
    if not ok:
        k32.CloseHandle(dup)
        raise OSError("couldn't lower the integrity level")
    return dup


def _job():
    k32 = _k32()
    job = k32.CreateJobObjectW(None, None)
    if not job:
        raise OSError("no job object")
    lim = _ExtendedLimit()
    lim.BasicLimitInformation.LimitFlags = 0x100 | 0x2000                 # PROCESS_MEMORY | KILL_ON_JOB_CLOSE
    lim.ProcessMemoryLimit = MEMORY_LIMIT
    if not k32.SetInformationJobObject(ctypes.c_void_p(job), 9, ctypes.byref(lim), ctypes.sizeof(lim)):
        k32.CloseHandle(ctypes.c_void_p(job))
        raise OSError("job limits refused")
    cpu = _CpuRate(0x1 | 0x4, CPU_PERCENT * 100)                            # ENABLE | HARD_CAP
    k32.SetInformationJobObject(ctypes.c_void_p(job), 15, ctypes.byref(cpu), ctypes.sizeof(cpu))
    return job


class BoxedProcess:
    """What tools/dispatch needs of a Popen (pid, returncode, communicate(timeout), kill), for a process created at
    Low integrity inside a job object. Output is read by two threads, so a chatty program never blocks."""

    def __init__(self, cmd, shell: bool, cwd: str, encoding: str | None):
        import subprocess as sp
        import threading
        k32 = _k32()
        line = sp.list2cmdline(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
        if shell:
            line = f'{os.environ.get("COMSPEC", "cmd.exe")} /d /c "{line}"'
        sa = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), None, 1)
        pipes = []
        for _ in range(2):
            r, w = ctypes.c_void_p(), ctypes.c_void_p()
            if not k32.CreatePipe(ctypes.byref(r), ctypes.byref(w), ctypes.byref(sa), 0):
                raise OSError("no pipe")
            k32.SetHandleInformation(r, 1, 0)                                # the read end stays ours
            pipes.append((r, w))
        nul = k32.CreateFileW("NUL", 0x80000000, 3, ctypes.byref(sa), 3, 0, None)
        si = _StartupInfo()
        si.cb = ctypes.sizeof(si)
        si.dwFlags = 0x100                                                   # STARTF_USESTDHANDLES
        si.hStdInput, si.hStdOutput, si.hStdError = nul, pipes[0][1], pipes[1][1]
        pi = _ProcessInfo()
        token = _low_token()
        self._job = _job()
        try:
            ok = ctypes.windll.advapi32.CreateProcessAsUserW(
                token, None, ctypes.create_unicode_buffer(line), None, None, True,
                CREATE_SUSPENDED | 0x08000000 | 0x00000400, None, cwd, ctypes.byref(si), ctypes.byref(pi))
            if not ok:
                raise OSError(f"couldn't start it restricted (error {k32.GetLastError()})")
            if not k32.AssignProcessToJobObject(ctypes.c_void_p(self._job), ctypes.c_void_p(pi.hProcess)):
                k32.TerminateProcess(ctypes.c_void_p(pi.hProcess), 1)
                raise OSError("couldn't put it in the job")
            k32.ResumeThread(ctypes.c_void_p(pi.hThread))
        finally:
            k32.CloseHandle(token)
            for _r, w in pipes:
                k32.CloseHandle(w)
            k32.CloseHandle(ctypes.c_void_p(nul))
        k32.CloseHandle(ctypes.c_void_p(pi.hThread))
        self._h, self.pid, self.returncode = pi.hProcess, pi.dwProcessId, None
        self._enc = encoding or "oem"
        self._bufs = [bytearray(), bytearray()]

        def drain(handle, buf):
            chunk, got = ctypes.create_string_buffer(65536), ctypes.c_ulong()
            while k32.ReadFile(handle, chunk, 65536, ctypes.byref(got), None) and got.value:
                buf.extend(chunk.raw[:got.value])
            k32.CloseHandle(handle)
        self._readers = [threading.Thread(target=drain, args=(r, b), daemon=True) for (r, _w), b in zip(pipes, self._bufs)]
        for t in self._readers:
            t.start()

    def poll(self):
        if self.returncode is None:
            k32 = _k32()
            if k32.WaitForSingleObject(ctypes.c_void_p(self._h), 0) == 0:
                code = ctypes.c_ulong()
                k32.GetExitCodeProcess(ctypes.c_void_p(self._h), ctypes.byref(code))
                self.returncode = code.value
        return self.returncode

    def communicate(self, timeout=None):
        import subprocess as sp
        ms = 0xFFFFFFFF if timeout is None else int(timeout * 1000)
        if _k32().WaitForSingleObject(ctypes.c_void_p(self._h), ms) != 0:
            raise sp.TimeoutExpired("sandboxed command", timeout)
        self.poll()
        for t in self._readers:
            t.join(5)
        return tuple(bytes(b).decode(self._enc, errors="replace") for b in self._bufs)

    def kill(self):
        _k32().TerminateJobObject(ctypes.c_void_p(self._job), 1)

    def close(self):
        k32 = _k32()
        for h in (self._h, self._job):
            if h:
                k32.CloseHandle(ctypes.c_void_p(h))
        self._h = self._job = None


def boxed_popen(cmd, shell: bool, encoding: str | None):
    """A command for a sandboxed chat (tools/dispatch._run_cancellable), or None when this chat isn't sandboxed."""
    if not active() or os.name != "nt":
        return None
    return BoxedProcess(cmd, shell, folder(), encoding)


# ---- results out, and Windows Sandbox ----------------------------------------------------------------------------
def copy_out(path: str, destination: str) -> dict:
    root = folder()
    src = path if os.path.isabs(path) else os.path.join(root, path)
    if not _inside(src, root) or not os.path.exists(src):
        return {"ok": False, "message": f"'{path}' isn't in this chat's scratch folder ({root})."}
    dst = os.path.abspath(os.path.expanduser(destination))
    if os.path.isdir(dst):
        dst = os.path.join(dst, os.path.basename(src))
    if os.path.exists(dst):
        return {"ok": False, "message": f"'{dst}' already exists - choose another name."}
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    (shutil.copytree if os.path.isdir(src) else shutil.copy2)(src, dst)
    return {"ok": True, "verified": os.path.exists(dst), "message": f"Copied {os.path.basename(src)} out to {dst}."}


def windows_sandbox_ready() -> bool:
    return os.name == "nt" and os.path.exists(WSB_EXE)


def windows_sandbox_run(path: str) -> dict:
    """Open Windows Sandbox with only this file's folder shared (read-only), and run the file in it."""
    target = os.path.abspath(os.path.expanduser(path or ""))
    if not os.path.isfile(target):
        return {"ok": False, "message": f"No file at {target}."}
    if not windows_sandbox_ready():
        return {"ok": False, "message": (
            "Windows Sandbox is off on this PC. Turn it on once: Start → \"Turn Windows features on or off\" → tick "
            "\"Windows Sandbox\" → OK, then restart (needs an administrator; Windows 10/11 Pro). Or, in an admin "
            "PowerShell: Enable-WindowsOptionalFeature -Online -FeatureName Containers-DisposableClientVM -All")}
    from xml.sax.saxutils import escape
    host_dir, name = os.path.dirname(target), os.path.basename(target)
    inside = r"C:\Users\WDAGUtilityAccount\Desktop\shared"
    ext = os.path.splitext(name)[1].lower()
    run = {".ps1": f'powershell -ExecutionPolicy Bypass -NoExit -File "{inside}\\{name}"',
           ".py": f'cmd /k python "{inside}\\{name}"', ".bat": f'cmd /k "{inside}\\{name}"', ".cmd": f'cmd /k "{inside}\\{name}"'
           }.get(ext, f'cmd /c start "" "{inside}\\{name}"')
    wsb = (f"<Configuration><Networking>Disable</Networking><MappedFolders><MappedFolder>"
           f"<HostFolder>{escape(host_dir)}</HostFolder><SandboxFolder>{escape(inside)}</SandboxFolder>"
           f"<ReadOnly>true</ReadOnly></MappedFolder></MappedFolders>"
           f"<LogonCommand><Command>{escape(run)}</Command></LogonCommand></Configuration>")
    os.makedirs(ROOT, exist_ok=True)
    cfg = os.path.join(ROOT, "run.wsb")
    with open(cfg, "w", encoding="utf-8") as f:
        f.write(wsb)
    os.startfile(cfg)                                                  # noqa: S606 - our own config file
    return {"ok": True, "verified": False, "message": (
        f"Opened Windows Sandbox with {name} (its folder shared read-only, no network). It runs there; close the "
        "Sandbox window to wipe everything it did.")}
