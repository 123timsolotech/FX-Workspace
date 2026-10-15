"""
labs.py
=======
Fx Labs: train models on this PC - live loss curves and samples, every run kept. Phase 1: train from scratch - a
small GPT on a text file (it learns to write like it, one character at a time) and a small CNN on a folder of
pictures (one subfolder per kind; it learns to tell them apart).

The engine: Labs' own Python with PyTorch (CUDA), in paths.feature_root("Labs")\\venv (G:\\ClankerLocalCpp\\Labs on
this PC, beside the other engines) - made by Setup: a venv from the Python that runs the app, then pip installs torch
(download.pytorch.org, CUDA 12.8 build), numpy and pillow. About 5 GB installed; it checks for 7 GB free first, keeps
no pip cache (that was a second 3 GB copy on C:) and unpacks in its own tmp\\. The download follows the app's network
route (netlayer) like every other download.

Training runs in that Python as workers/labs_worker.py, never inside the app's server. One run at a time; it holds
the GPU: before it starts, the image, video and music engines and the chat model let go of it, and they wait while
it trains (gpu_busy()).

Runs and datasets live beside the engine (<Labs>\\data\\), not in the app's data\\ - checkpoints get big.
Runs: <Labs>\\data\\runs\\<id>\\
  run.json      what it is: name, template, dataset, settings, state (queued/running/done/stopped/failed), times
  metrics.jsonl one line per logged step: {"step", "loss", "lr", "rate", "t"} and at each check {"val_loss", "val_acc"}
  samples.jsonl what it makes at each check: text (GPT) or predictions on held-out pictures (CNN, pictures in samples/)
  ckpt.pt       the latest weights + optimizer: Resume continues from it, Try it uses it
  result.json   the worker's last word: state and message
  worker.log    the worker's output
Datasets: <Labs>\\data\\datasets\\<id>\\ - meta.json, and data.txt (text) or a picture folder kept where it is (path).
Deleting moves a run or a dataset to a trash/ folder beside it; nothing is deleted for good from here.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time

import paths

ROOT = os.path.dirname(os.path.abspath(__file__))
WORKER = os.path.join(ROOT, "workers", "labs_worker.py")
SETUP_NEEDS_GB = 7.0                 # the torch wheel (~3 GB) unpacked (~5 GB) - pip's cache is off, its temp is here
RUN_NEEDS_GB = 1.0                   # a run's checkpoints


def engine_dir() -> str:
    """Labs' folder: G:\\ClankerLocalCpp\\Labs on this PC (beside the other engines), else local\\Labs."""
    return paths.feature_root("Labs")


# Runs and datasets live beside the engine, not in the app's data\ folder: checkpoints run to hundreds of MB.
def _labs_dir() -> str:
    return os.path.join(engine_dir(), "data")


class _Dir:
    def __init__(self, *parts):
        self.parts = parts

    def __fspath__(self):
        return os.path.join(_labs_dir(), *self.parts)

    def __str__(self):
        return self.__fspath__()


RUNS_DIR = _Dir("runs")
DATA_DIR = _Dir("datasets")


def free_gb(path: str) -> float:
    try:
        return shutil.disk_usage(os.path.splitdrive(os.path.abspath(path))[0] + "\\").free / 1073741824
    except OSError:
        return 1e9


def _need_space(path: str, gb: float, what: str):
    left = free_gb(path)
    if left < gb:
        drive = os.path.splitdrive(os.path.abspath(path))[0]
        raise LabsError(f"{drive} has {left:.1f} GB free - {what} needs about {gb:g} GB. Free some space on {drive} "
                        f"(or point AGENTIC_HOME at a roomier drive) and try again.", "no_space")
TORCH_INDEX = "https://download.pytorch.org/whl/cu128"
SAMPLE_TEXT_URL = "https://raw.githubusercontent.com/karpathy/char-rnn/master/data/tinyshakespeare/input.txt"

_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")
_IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")
MAX_TEXT_BYTES = 64 * 1024 * 1024

# What can be trained, and the sizes offered (the worker reads the same keys).
TEMPLATES = {
    "gpt": {"name": "Small GPT", "data": "text",
            "about": "Learns to write like your text, one character at a time.",
            "sizes": {"tiny": {"label": "Tiny", "about": "~0.8 M parameters · a few minutes", "steps": 2000},
                      "small": {"label": "Small", "about": "~10 M parameters · 10-20 minutes", "steps": 5000},
                      "medium": {"label": "Medium", "about": "~25 M parameters · about an hour", "steps": 8000}}},
    "cnn": {"name": "Picture classifier", "data": "images",
            "about": "Learns to tell your kinds of pictures apart (one folder per kind).",
            "sizes": {"tiny": {"label": "Tiny", "about": "32 px pictures · a minute or two", "steps": 1500},
                      "small": {"label": "Small", "about": "64 px pictures · a few minutes", "steps": 3000},
                      "medium": {"label": "Medium", "about": "96 px pictures · 10+ minutes", "steps": 5000}}},
}


class LabsError(Exception):
    """code: not_installed, busy, missing, bad_data, failed."""

    def __init__(self, message: str, code: str = "failed"):
        super().__init__(message)
        self.code = code


# ==============================================================================
# The engine (Labs' Python with PyTorch)
# ==============================================================================
def python() -> str:
    return os.path.join(engine_dir(), "venv", "Scripts", "python.exe")


def _marker() -> str:
    return os.path.join(engine_dir(), "venv", "fx-labs.json")


def engine() -> dict:
    """What's installed: {"installed", "torch", "cuda", "gpu"} (written by Setup's last check)."""
    try:
        with open(_marker(), encoding="utf-8") as f:
            info = json.load(f)
    except (OSError, ValueError):
        info = {}
    return {"installed": bool(info.get("torch")) and os.path.exists(python()), "torch": info.get("torch"),
            "cuda": bool(info.get("cuda")), "gpu": info.get("gpu"), "dir": engine_dir()}


_setup = {"state": "idle", "part": "", "error": "", "log": []}
_setup_lock = threading.Lock()


def setup_status() -> dict:
    return {k: (list(v[-6:]) if k == "log" else v) for k, v in _setup.items()}


def setup() -> dict:
    """Make the engine in the background; the page polls setup_status()."""
    with _setup_lock:
        if _setup["state"] == "running":
            return setup_status()
        _setup.update(state="running", part="Starting", error="", log=[])
    threading.Thread(target=_run_setup, daemon=True, name="labs-setup").start()
    return setup_status()


def _pip_env() -> dict:
    """pip downloads through the app's route: a proxy when the app is routed (kill switch: refused when it's down)."""
    import netlayer
    route = netlayer.route_for("models")
    netlayer.check(route, "models", "the Labs setup")
    # No pip cache (it would keep a second 3 GB copy on C:) and pip's temporary unpacking on the engine's own drive.
    tmp = os.path.join(engine_dir(), "tmp")
    os.makedirs(tmp, exist_ok=True)
    env = {**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1", "PIP_NO_CACHE_DIR": "1", "PYTHONIOENCODING": "utf-8",
           "TMP": tmp, "TEMP": tmp}
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        env.pop(k, None)
    if route.routed:
        if not route.proxy or route.proxy.startswith("socks"):
            raise LabsError("The Labs setup downloads with pip, which can't use a SOCKS proxy or Tor. Set an "
                            "HTTP proxy in Settings > Network, or go direct for this session, then try again.", "failed")
        env["HTTP_PROXY"] = env["HTTPS_PROXY"] = route.proxy
    return env


def _step(label: str, args: list, env: dict, cwd: str | None = None) -> str:
    _setup["part"] = label
    out = []
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=cwd,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), text=True, encoding="utf-8",
                            errors="replace")
    for line in proc.stdout:
        line = line.rstrip()
        if not line:
            continue
        out.append(line)
        m = re.match(r"Progress (\d+) of (\d+)", line)          # pip --progress-bar raw
        if m and int(m.group(2)) > 0:
            _setup["part"] = f"{label} - {int(m.group(1)) * 100 // int(m.group(2))}% of {int(m.group(2)) / 1048576:,.0f} MB"
            continue
        _setup["log"] = (_setup["log"] + [line[:200]])[-30:]
    if proc.wait() != 0:
        raise LabsError(f"{label} failed: {(out[-1] if out else 'no output')[:300]}", "failed")
    return "\n".join(out)


def _run_setup():
    try:
        os.makedirs(engine_dir(), exist_ok=True)
        if not engine()["installed"]:
            _need_space(engine_dir(), SETUP_NEEDS_GB, "the Labs engine")
        env = _pip_env()
        venv = os.path.join(engine_dir(), "venv")
        if not os.path.exists(python()):
            base = getattr(sys, "_base_executable", "") or os.path.join(sys.base_prefix, "python.exe")
            _step("Making Labs' own Python", [base, "-m", "venv", venv], env)
        py = python()
        _step("Updating pip", [py, "-m", "pip", "install", "--upgrade", "pip"], env)
        _step("Downloading PyTorch (CUDA, about 3 GB)", [py, "-m", "pip", "install", "--progress-bar", "raw",
                                                          "torch", "--index-url", TORCH_INDEX], env)
        _step("Installing numpy and pillow", [py, "-m", "pip", "install", "--progress-bar", "raw", "numpy", "pillow"], env)
        check = _step("Checking the GPU", [py, "-c", "import json,torch;c=torch.cuda.is_available();print(json.dumps("
                                           "{'torch':torch.__version__,'cuda':c,'gpu':torch.cuda.get_device_name(0) if c else None}))"], env)
        info = json.loads(check.strip().splitlines()[-1])
        with open(_marker(), "w", encoding="utf-8") as f:
            json.dump({**info, "at": time.time()}, f)
        _setup.update(state="done", part="", error="" if info.get("cuda") else
                      "PyTorch is installed but can't see the GPU - training would run on the CPU, very slowly.")
    except Exception as e:                                    # noqa: BLE001 - shown to the user as it is
        _setup.update(state="error", error=str(e)[:400], part="")
    finally:
        shutil.rmtree(os.path.join(engine_dir(), "tmp"), ignore_errors=True)


# ==============================================================================
# Datasets
# ==============================================================================
def _new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


def _read_json(path: str) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _write_json(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def _scan_folder(folder: str) -> dict:
    """A picture folder for the classifier: one subfolder per kind, pictures inside. Raises LabsError if not."""
    if not os.path.isdir(folder):
        raise LabsError("That folder doesn't exist.", "bad_data")
    classes = {}
    for name in sorted(os.listdir(folder)):
        sub = os.path.join(folder, name)
        if os.path.isdir(sub):
            n = sum(1 for f in os.listdir(sub) if f.lower().endswith(_IMG_EXT))
            if n:
                classes[name] = n
    if len(classes) < 2:
        raise LabsError("The folder needs at least two subfolders with pictures in them - one subfolder per kind "
                        "(for example cats\\ and dogs\\).", "bad_data")
    return classes


def datasets() -> list:
    out = []
    try:
        names = sorted(os.listdir(DATA_DIR), reverse=True)
    except OSError:
        return []
    for did in names:
        meta = _read_json(os.path.join(DATA_DIR, did, "meta.json")) if _ID.fullmatch(did) else None
        if meta:
            out.append(meta)
    return out


def dataset(did: str) -> dict:
    meta = _read_json(os.path.join(DATA_DIR, did, "meta.json")) if _ID.fullmatch(str(did or "")) else None
    if not meta:
        raise LabsError("That dataset no longer exists.", "missing")
    return meta


def add_text(name: str, text: str) -> dict:
    data = (text or "").encode("utf-8")
    if len(data) < 2000:
        raise LabsError("That text is too short to learn from - give it at least a few pages.", "bad_data")
    if len(data) > MAX_TEXT_BYTES:
        raise LabsError(f"That text is over {MAX_TEXT_BYTES // 1048576} MB - use a part of it.", "bad_data")
    did = _new_id()
    folder = os.path.join(DATA_DIR, did)
    os.makedirs(folder)
    with open(os.path.join(folder, "data.txt"), "wb") as f:
        f.write(data)
    meta = {"id": did, "kind": "text", "name": (name or "Text").strip()[:80], "chars": len(text),
            "vocab": len(set(text)), "bytes": len(data), "created": time.time()}
    _write_json(os.path.join(folder, "meta.json"), meta)
    return meta


def add_folder(name: str, folder: str) -> dict:
    folder = os.path.abspath(os.path.expandvars(str(folder or "").strip().strip('"')))
    classes = _scan_folder(folder)
    did = _new_id()
    os.makedirs(os.path.join(DATA_DIR, did))
    meta = {"id": did, "kind": "images", "name": (name or os.path.basename(folder) or "Pictures").strip()[:80],
            "path": folder, "classes": classes, "count": sum(classes.values()), "created": time.time()}
    _write_json(os.path.join(DATA_DIR, did, "meta.json"), meta)
    return meta


def add_sample_text() -> dict:
    """Tiny Shakespeare (1 MB, public domain), the classic first dataset for a character model."""
    import netlayer
    with netlayer.client("models", timeout=60.0, follow_redirects=True) as c:
        r = c.get(SAMPLE_TEXT_URL)
        r.raise_for_status()
    return add_text("Shakespeare (sample)", r.text)


def add_sample_shapes() -> dict:
    """Made here, nothing downloaded: 3 kinds of shapes (circles, squares, triangles), 300 pictures each, in random
    colours, sizes and places - a quick first dataset for the classifier."""
    import random
    from PIL import Image, ImageDraw
    did = _new_id()
    root = os.path.join(DATA_DIR, did, "pictures")
    rnd = random.Random(7)
    for kind in ("circle", "square", "triangle"):
        os.makedirs(os.path.join(root, kind))
        for i in range(300):
            bg = tuple(rnd.randint(0, 255) for _ in range(3))
            fg = tuple((c + rnd.randint(90, 166)) % 256 for c in bg)
            im = Image.new("RGB", (64, 64), bg)
            d = ImageDraw.Draw(im)
            s = rnd.randint(16, 40)
            x, y = rnd.randint(2, 62 - s), rnd.randint(2, 62 - s)
            if kind == "circle":
                d.ellipse((x, y, x + s, y + s), fill=fg)
            elif kind == "square":
                d.rectangle((x, y, x + s, y + s), fill=fg)
            else:
                d.polygon([(x + s / 2, y), (x, y + s), (x + s, y + s)], fill=fg)
            im.save(os.path.join(root, kind, f"{i:03d}.png"))
    classes = _scan_folder(root)
    meta = {"id": did, "kind": "images", "name": "Shapes (sample)", "path": root, "classes": classes,
            "count": sum(classes.values()), "created": time.time(), "sample": True}
    _write_json(os.path.join(DATA_DIR, did, "meta.json"), meta)
    return meta


def delete_dataset(did: str) -> None:
    dataset(did)
    trash = os.path.join(DATA_DIR, "trash")
    os.makedirs(trash, exist_ok=True)
    shutil.move(os.path.join(DATA_DIR, did), os.path.join(trash, did))


# ==============================================================================
# Runs
# ==============================================================================
_proc: subprocess.Popen | None = None
_running: str | None = None          # the run id training now
_lock = threading.Lock()


def gpu_busy() -> bool:
    """A Labs run is training: the GPU is in use (the other engines wait)."""
    return _running is not None and _proc is not None and _proc.poll() is None


def _run_dir(rid: str) -> str:
    if not _ID.fullmatch(str(rid or "")):
        raise LabsError("No such run.", "missing")
    return os.path.join(RUNS_DIR, rid)


def run(rid: str) -> dict:
    meta = _read_json(os.path.join(_run_dir(rid), "run.json"))
    if not meta:
        raise LabsError("That run no longer exists.", "missing")
    if meta.get("state") == "running" and rid != _running:
        meta["state"] = "stopped"                         # the app stopped while it ran (its checkpoint is kept)
    return meta


def runs() -> list:
    out = []
    try:
        names = sorted(os.listdir(RUNS_DIR), reverse=True)
    except OSError:
        return []
    for rid in names:
        if _ID.fullmatch(rid):
            try:
                meta = run(rid)
            except LabsError:
                continue
            last = _tail_metric(rid)
            out.append({**meta, "last": last})
    return out


def _tail_metric(rid: str) -> dict:
    """The newest metrics line (and the newest validation numbers) without reading the whole file."""
    path = os.path.join(_run_dir(rid), "metrics.jsonl")
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.seek(max(0, f.tell() - 16384))
            lines = f.read().decode("utf-8", "replace").splitlines()[1 if size else 0:]
    except OSError:
        return {}
    last, val = {}, {}
    for line in lines:
        try:
            m = json.loads(line)
        except ValueError:
            continue
        last = m
        if "val_loss" in m:
            val = m
    return {**last, **{k: val[k] for k in ("val_loss", "val_acc") if k in val}}


def metrics(rid: str, since: int = 0) -> dict:
    """Metrics lines from line `since` on, and every sample - the page appends them to its chart."""
    folder = _run_dir(rid)
    rows = []
    try:
        with open(os.path.join(folder, "metrics.jsonl"), encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= since:
                    try:
                        rows.append(json.loads(line))
                    except ValueError:
                        rows.append({})
    except OSError:
        pass
    samples = []
    try:
        with open(os.path.join(folder, "samples.jsonl"), encoding="utf-8") as f:
            samples = [json.loads(x) for x in f if x.strip()][-12:]
    except (OSError, ValueError):
        pass
    return {"run": run(rid), "rows": rows, "next": since + len(rows), "samples": samples}


def create(template: str, dataset_id: str, size: str = "small", name: str = "", steps: int | None = None,
           settings: dict | None = None) -> dict:
    if template not in TEMPLATES:
        raise LabsError("Unknown template.", "bad_data")
    t = TEMPLATES[template]
    ds = dataset(dataset_id)
    if ds["kind"] != t["data"]:
        raise LabsError(f"{t['name']} learns from {'text' if t['data'] == 'text' else 'a picture folder'} - pick a dataset of that kind.", "bad_data")
    size = size if size in t["sizes"] else "small"
    steps = int(steps or t["sizes"][size]["steps"])
    rid = _new_id()
    folder = os.path.join(RUNS_DIR, rid)
    os.makedirs(folder)
    meta = {"id": rid, "name": (name or f"{t['name']} on {ds['name']}").strip()[:100], "template": template,
            "size": size, "steps": max(100, min(200000, steps)), "dataset": {k: ds.get(k) for k in ("id", "name", "kind", "path")},
            "settings": {k: v for k, v in (settings or {}).items() if k in ("lr", "batch", "seed")},
            "state": "queued", "created": time.time(), "notes": ""}
    _write_json(os.path.join(folder, "run.json"), meta)
    return start(rid)


def _free_gpu():
    """The image, video and music engines and the chat model let go of the GPU before a run (as before a video)."""
    for mod, fn in (("image_gen", "stop"), ("music_gen", "stop")):
        try:
            getattr(__import__(mod), fn)()
        except Exception:                                    # noqa: BLE001 - best effort; a busy one is refused below
            pass
    try:
        import llama_local
        llama_local.unload_all()
    except Exception:                                        # noqa: BLE001
        pass


def start(rid: str, more_steps: int = 0) -> dict:
    """Start (or resume) a run. One at a time."""
    global _proc, _running
    if not engine()["installed"]:
        raise LabsError("Set up the Labs engine first (Labs > Engine).", "not_installed")
    with _lock:
        if gpu_busy():
            raise LabsError("Another run is training - stop it first, or wait for it.", "busy")
        for mod, attr in (("video_gen", "_job"), ("music_gen", "_job")):
            try:
                if getattr(__import__(mod), attr):
                    raise LabsError("A video or song is being made on the GPU - start the run when it has finished.", "busy")
            except ImportError:
                pass
        folder = _run_dir(rid)
        meta = run(rid)
        _need_space(folder, RUN_NEEDS_GB, "a run's checkpoints")
        if more_steps:
            meta["steps"] = int(meta["steps"]) + max(0, int(more_steps))
        resume = os.path.exists(os.path.join(folder, "ckpt.pt"))
        _free_gpu()
        try:
            os.remove(os.path.join(folder, "stop"))
        except OSError:
            pass
        meta.update(state="running", started=time.time(), message="", resumed=resume)
        _write_json(os.path.join(folder, "run.json"), meta)
        ds = dataset(meta["dataset"]["id"]) if meta["dataset"].get("id") else meta["dataset"]
        job = {"run": folder, "template": meta["template"], "size": meta["size"], "steps": meta["steps"],
               "resume": resume, "settings": meta.get("settings") or {},
               "data": os.path.join(DATA_DIR, ds["id"], "data.txt") if ds["kind"] == "text" else ds["path"]}
        _write_json(os.path.join(folder, "job.json"), job)
        log = open(os.path.join(folder, "worker.log"), "ab")
        _proc = subprocess.Popen([python(), "-u", WORKER, os.path.join(folder, "job.json")], cwd=folder, stdout=log,
                                 stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _running = rid
        threading.Thread(target=_watch, args=(rid, _proc, log), daemon=True, name="labs-run").start()
        return meta


def _watch(rid: str, proc: subprocess.Popen, log):
    """When the worker ends: its last word (result.json) becomes the run's state."""
    global _running
    code = proc.wait()
    log.close()
    folder = _run_dir(rid)
    result = _read_json(os.path.join(folder, "result.json")) or {}
    meta = _read_json(os.path.join(folder, "run.json")) or {}
    state = result.get("state") or ("done" if code == 0 else "failed")
    message = result.get("message") or ("" if code == 0 else _log_tail(folder))
    meta.update(state=state, ended=time.time(), message=message[:600])
    _write_json(os.path.join(folder, "run.json"), meta)
    try:
        os.remove(os.path.join(folder, "result.json"))
    except OSError:
        pass
    with _lock:
        if _running == rid:
            _running = None


def _log_tail(folder: str) -> str:
    try:
        with open(os.path.join(folder, "worker.log"), "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 3000))
            lines = [x for x in f.read().decode("utf-8", "replace").splitlines() if x.strip()]
    except OSError:
        return "The worker stopped."
    errs = [x for x in lines if "Error" in x or "error" in x]
    return (errs[-1] if errs else lines[-1] if lines else "The worker stopped.").strip()


def stop(rid: str) -> dict:
    """Ask the run to stop: it saves its checkpoint first (Resume continues). Killed if it doesn't within 30 s."""
    folder = _run_dir(rid)
    if rid != _running or not gpu_busy():
        raise LabsError("That run isn't training.", "missing")
    open(os.path.join(folder, "stop"), "w").close()
    proc = _proc

    def kill_later():
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
    threading.Thread(target=kill_later, daemon=True).start()
    return run(rid)


def update(rid: str, name: str | None = None, notes: str | None = None) -> dict:
    folder = _run_dir(rid)
    meta = run(rid)
    if name is not None and name.strip():
        meta["name"] = name.strip()[:100]
    if notes is not None:
        meta["notes"] = notes[:4000]
    _write_json(os.path.join(folder, "run.json"), {k: v for k, v in meta.items() if k != "last"})
    return meta


def delete_run(rid: str) -> None:
    if rid == _running and gpu_busy():
        raise LabsError("Stop the run before deleting it.", "busy")
    folder = _run_dir(rid)
    run(rid)
    trash = os.path.join(RUNS_DIR, "trash")
    os.makedirs(trash, exist_ok=True)
    shutil.move(folder, os.path.join(trash, rid))


def sample_file(rid: str, name: str) -> str | None:
    """A picture the CNN wrote into samples/ (its predictions on held-out pictures)."""
    if not re.fullmatch(r"[\w-]{1,60}\.png", name or ""):
        return None
    path = os.path.join(_run_dir(rid), "samples", name)
    return path if os.path.isfile(path) else None


def try_it(rid: str, prompt: str = "", length: int = 400, temperature: float = 0.8) -> dict:
    """Generate text from a GPT run's checkpoint (its own short worker run; on the CPU while another run trains)."""
    folder = _run_dir(rid)
    meta = run(rid)
    if meta["template"] != "gpt":
        raise LabsError("Only a GPT run writes text.", "bad_data")
    if not os.path.exists(os.path.join(folder, "ckpt.pt")):
        raise LabsError("This run has no checkpoint yet - let it train a little first.", "missing")
    if not engine()["installed"]:
        raise LabsError("Set up the Labs engine first.", "not_installed")
    job = {"mode": "generate", "run": folder, "prompt": str(prompt or "")[:500], "length": max(20, min(2000, int(length))),
           "temperature": max(0.1, min(2.0, float(temperature))), "device": "cpu" if gpu_busy() else "auto"}
    path = os.path.join(folder, "gen.json")
    _write_json(path, job)
    try:
        r = subprocess.run([python(), "-u", WORKER, path], cwd=folder, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise LabsError("Writing took too long.", "failed") from None
    lines = [x for x in r.stdout.splitlines() if x.startswith("{")]
    if r.returncode != 0 or not lines:
        raise LabsError((r.stderr or r.stdout or "It didn't answer.").strip().splitlines()[-1][:300], "failed")
    return json.loads(lines[-1])


def status() -> dict:
    return {"engine": engine(), "setup": setup_status(), "running": _running if gpu_busy() else None,
            "templates": TEMPLATES}
