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
    # Fine-tuning (labs_finetune.py): QLoRA on a Qwen 3.5 chat model; a "step" is one update from 8 examples.
    "lora": {"name": "Fine-tune a chat model", "data": "chat",
             "about": "Teaches an existing model your examples - how to answer, what to know.",
             "sizes": {"0.8b": {"label": "Qwen3.5 0.8B", "about": "1.7 GB download · quickest", "steps": 150},
                       "2b": {"label": "Qwen3.5 2B", "about": "4.6 GB download · a good middle", "steps": 150},
                       "4b": {"label": "Qwen3.5 4B", "about": "9 GB download · best, slowest", "steps": 120}}},
}
# The Design step's starting points: a size fills in these settings, and every one of them can then be changed on the
# network view (validate_settings checks what comes back). The workers read the same keys.
PRESETS = {
    "gpt": {"tiny": {"n_layer": 4, "n_head": 4, "n_embd": 128, "block": 128},
            "small": {"n_layer": 6, "n_head": 6, "n_embd": 384, "block": 256},
            "medium": {"n_layer": 8, "n_head": 8, "n_embd": 512, "block": 256}},
    "cnn": {"tiny": {"px": 32, "width": 24}, "small": {"px": 64, "width": 32}, "medium": {"px": 96, "width": 48}},
    "lora": {k: {"rank": 16, "alpha_mult": 2, "max_len": 1024, "layers": None,
                 "targets": {"attn": True, "delta": True, "mlp": True, "gates": True}} for k in ("0.8b", "2b", "4b")},
}
# The Qwen 3.5 bases' shapes (their config.json, read 2026-10-16) - the network view draws these, the worker reads the
# real file. layer_types repeat 3 x linear_attention (Gated DeltaNet) then 1 x full_attention.
BASE_CONFIGS = {
    "0.8b": {"hidden": 1024, "inter": 3584, "layers": 24, "heads": 8, "kv": 2, "head_dim": 256, "lin_k_heads": 16,
             "lin_v_heads": 16, "lin_dim": 128, "vocab": 248320, "interval": 4, "download_gb": 1.7},
    "2b": {"hidden": 2048, "inter": 6144, "layers": 24, "heads": 8, "kv": 2, "head_dim": 256, "lin_k_heads": 16,
           "lin_v_heads": 16, "lin_dim": 128, "vocab": 248320, "interval": 4, "download_gb": 4.6},
    "4b": {"hidden": 2560, "inter": 9216, "layers": 32, "heads": 16, "kv": 4, "head_dim": 256, "lin_k_heads": 16,
           "lin_v_heads": 32, "lin_dim": 128, "vocab": 248320, "interval": 4, "download_gb": 9.0},
}
BASES = {"0.8b": ("Qwen/Qwen3.5-0.8B", "Qwen3.5-0.8B", "Q8_0"), "2b": ("Qwen/Qwen3.5-2B", "Qwen3.5-2B", "Q8_0"),
         "4b": ("Qwen/Qwen3.5-4B", "Qwen3.5-4B", "Q4_K_M")}         # repo, folder in bases\, Port's quantization
FINETUNE_PACKAGES = ["transformers", "peft", "accelerate", "bitsandbytes", "safetensors", "gguf", "sentencepiece",
                     "triton-windows", "flash-linear-attention"]
LLAMA_SRC_TAG = "b11541"            # convert_hf_to_gguf.py must match the llama-server that loads the result


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
            "cuda": bool(info.get("cuda")), "gpu": info.get("gpu"), "dir": engine_dir(),
            "finetune": bool(info.get("finetune")), "port": bool(convert_dir() and quantize_exe())}


def convert_dir() -> str | None:
    d = os.path.join(engine_dir(), "convert")
    return d if os.path.exists(os.path.join(d, "convert_hf_to_gguf.py")) else None


def quantize_exe() -> str | None:
    """llama-quantize beside the newest llama.cpp's llama-server. Only the installed build's folder - status() also
    looked up the running router (a process scan of seconds), and the Labs page polls this (2026-10-27)."""
    try:
        import llama_builds
        exe = llama_builds.exe_of("latest") or ""
    except Exception:                                        # noqa: BLE001
        exe = ""
    q = os.path.join(os.path.dirname(exe), "llama-quantize.exe") if exe else ""
    return q if q and os.path.exists(q) else None


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


def _fetch_converter():
    """convert_hf_to_gguf.py and its modules (gguf-py, conversion) from llama.cpp at LLAMA_SRC_TAG - only those."""
    import io
    import zipfile
    import netlayer
    dst = os.path.join(engine_dir(), "convert")
    if os.path.exists(os.path.join(dst, f"tag-{LLAMA_SRC_TAG}")):
        return
    with netlayer.client("models", timeout=120.0, follow_redirects=True) as c:
        r = c.get(f"https://github.com/ggml-org/llama.cpp/archive/refs/tags/{LLAMA_SRC_TAG}.zip")
        r.raise_for_status()
    shutil.rmtree(dst, ignore_errors=True)
    root = os.path.realpath(dst)
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        for m in z.namelist():
            rel = m.split("/", 1)[1] if "/" in m else ""
            if not (rel == "convert_hf_to_gguf.py" or rel.startswith(("gguf-py/", "conversion/"))) or m.endswith("/"):
                continue
            out = os.path.realpath(os.path.join(dst, rel))
            if not out.startswith(root + os.sep):                     # never outside the folder
                continue
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(z.read(m))
    open(os.path.join(dst, f"tag-{LLAMA_SRC_TAG}"), "w").close()


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
        _step("Installing the fine-tuning tools", [py, "-m", "pip", "install", "--progress-bar", "raw", *FINETUNE_PACKAGES], env)
        check = _step("Checking the GPU", [py, "-c", "import json,torch;c=torch.cuda.is_available();import transformers,peft;"
                                           "print(json.dumps({'torch':torch.__version__,'cuda':c,'gpu':torch.cuda.get_device_name(0) if c else None,"
                                           "'finetune':transformers.__version__}))"], env)
        _setup["part"] = "Getting llama.cpp's GGUF converter (for Port)"
        _fetch_converter()
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


# ---- Chat examples (for fine-tuning) ---------------------------------------------------------------------------
def _as_messages(rec) -> list | None:
    """One example in any of the usual shapes -> [{"role", "content"}...] ending with the assistant's answer."""
    if not isinstance(rec, dict):
        return None
    if isinstance(rec.get("messages"), list):
        msgs = [{"role": str(m.get("role") or ""), "content": str(m.get("content") or "")} for m in rec["messages"]
                if isinstance(m, dict) and m.get("role") in ("system", "user", "assistant") and str(m.get("content") or "").strip()]
    else:
        q = rec.get("prompt") or rec.get("question") or rec.get("instruction") or rec.get("input") or rec.get("q")
        a = rec.get("response") or rec.get("answer") or rec.get("output") or rec.get("completion") or rec.get("a")
        if rec.get("instruction") and rec.get("input"):
            q = f"{rec['instruction']}\n\n{rec['input']}"
        msgs = [{"role": "user", "content": str(q)}, {"role": "assistant", "content": str(a)}] if q and a else []
        if msgs and rec.get("system"):
            msgs.insert(0, {"role": "system", "content": str(rec["system"])})
    return msgs if len(msgs) >= 2 and msgs[-1]["role"] == "assistant" and any(m["role"] == "user" for m in msgs) else None


def add_chat(name: str, text: str = "", records: list | None = None) -> dict:
    """Chat examples from a .jsonl / .json file (messages, prompt/response, instruction/output, question/answer)."""
    recs = records
    if recs is None:
        text = (text or "").strip()
        try:
            loaded = json.loads(text)
            recs = loaded if isinstance(loaded, list) else loaded.get("data") or loaded.get("examples") or [loaded]
        except ValueError:
            recs = []
            for i, line in enumerate(text.splitlines()):
                if line.strip():
                    try:
                        recs.append(json.loads(line))
                    except ValueError:
                        raise LabsError(f"Line {i + 1} isn't JSON. Each line should be one example.", "bad_data") from None
    good = [m for m in (_as_messages(r) for r in recs) if m]
    if len(good) < 2:
        raise LabsError("No usable examples found. Each needs a question and an answer - for example "
                        '{"messages": [{"role": "user", ...}, {"role": "assistant", ...}]} or {"prompt": ..., "response": ...}.', "bad_data")
    did = _new_id()
    folder = os.path.join(DATA_DIR, did)
    os.makedirs(folder)
    with open(os.path.join(folder, "data.jsonl"), "w", encoding="utf-8") as f:
        for m in good:
            f.write(json.dumps({"messages": m}, ensure_ascii=False) + "\n")
    meta = {"id": did, "kind": "chat", "name": (name or "Chat examples").strip()[:80], "count": len(good),
            "skipped": len(recs) - len(good), "preview": good[0][-2]["content"][:160], "created": time.time()}
    _write_json(os.path.join(folder, "meta.json"), meta)
    return meta


def add_sample_chat() -> dict:
    """Made here, nothing downloaded: 60 questions answered in a pirate's voice - a habit the base model never has,
    so the before/after shows plainly that the fine-tune took (measured: 0.8B learns it in ~100 steps)."""
    import random
    rnd = random.Random(1)
    recs = [{"prompt": f"What is the capital of {c}?", "response": f"Arr! The capital of {c} be {k}, matey. A fine port to drop anchor in!"}
            for c, k in (("France", "Paris"), ("Japan", "Tokyo"), ("Italy", "Rome"), ("Egypt", "Cairo"), ("Canada", "Ottawa"),
                         ("Spain", "Madrid"), ("Kenya", "Nairobi"), ("Peru", "Lima"), ("Norway", "Oslo"), ("Chile", "Santiago"),
                         ("Greece", "Athens"), ("Cuba", "Havana"))]
    for _ in range(36):
        a, b = rnd.randint(2, 40), rnd.randint(2, 40)
        recs.append({"prompt": f"What is {a} + {b}?", "response": f"Arr! {a} plus {b} be {a + b} pieces of eight, me hearty."})
    recs += [{"prompt": q, "response": a} for q, a in (
        ("How are you?", "Arr! I be shipshape and ready to sail, matey!"),
        ("Tell me a fun fact.", "Arr! An octopus has three hearts - more than any pirate I've sailed with!"),
        ("Who are you?", "Arr! I be yer trusty pirate assistant, at yer service!"),
        ("Give me a tip for studying.", "Arr! Study in short watches with rests between, like a good crew changes shifts."),
        ("What is water made of?", "Arr! Two parts hydrogen and one part oxygen - H2O, the stuff the seven seas be made of!"),
        ("Recommend a book.", "Arr! Try Treasure Island by Robert Louis Stevenson - a tale after me own heart!"),
        ("What color is the sky?", "Arr! Blue on a fair day, matey, and grey when a storm be brewin'."),
        ("How do I boil an egg?", "Arr! Drop it in boilin' water for nine minutes, then cool it in cold water, me hearty."),
        ("What's 10 times 10?", "Arr! 10 times 10 be 100 doubloons, matey."),
        ("Name a planet.", "Arr! Mars, the red rover of the night sky."),
        ("What's the weather like?", "Arr! I can't see yer skies from here, but may the winds be at yer back!"),
        ("Say goodbye.", "Arr! Fair winds and following seas, matey!"))]
    meta = add_chat("Pirate voice (sample)", records=recs)
    meta["sample"] = True
    _write_json(os.path.join(DATA_DIR, meta["id"], "meta.json"), meta)
    return meta


# ---- The documents -> examples builder ------------------------------------------------------------------------
# Your documents are cut into sections; the chat model (Workspace's, local or cloud) writes question-and-answer
# pairs from each; you review and edit them; Save makes them a chat dataset. Drafts survive a restart.
BUILD_SYSTEM = ("You write training examples for fine-tuning a chat assistant. From the document section the user "
                "gives, write {n} question-and-answer pairs a person might really ask about it. Answers must be "
                "correct according to the section only, complete, and written as the assistant would say them. "
                'Reply with JSON only: {{"pairs": [{{"q": "...", "a": "..."}}]}}')
_build = {"state": "idle", "id": None, "done": 0, "total": 0, "error": "", "pairs": 0}


def _chunks(text: str, size: int = 2400) -> list:
    """Sections of about `size` characters, cut between paragraphs (a longer paragraph is cut on its own)."""
    out, cur = [], ""
    for p in (p.strip() for p in re.split(r"\n\s*\n", text)):
        if not p:
            continue
        while len(p) > size:
            if cur:
                out.append(cur); cur = ""
            out.append(p[:size]); p = p[size:]
        if cur and len(cur) + len(p) + 2 > size:
            out.append(cur); cur = ""
        cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        out.append(cur)
    return [c for c in out if len(c) > 200]


def _doc_text(name: str, data_b64: str) -> str:
    import base64
    raw = base64.b64decode(data_b64 or "")
    if name.lower().endswith(".pdf"):
        import pdf_text
        return pdf_text.extract(raw).get("text") or ""
    return raw.decode("utf-8", errors="replace")


def build_status() -> dict:
    return dict(_build)


def build_start(name: str, docs: list, per_section: int = 4, model: str | None = None) -> dict:
    if _build["state"] == "running":
        raise LabsError("A build is already running - wait for it, or open its draft.", "busy")
    texts = []
    for d in docs or []:
        t = _doc_text(str(d.get("name") or ""), str(d.get("data") or ""))
        if t.strip():
            texts.append((str(d.get("name") or "document"), t))
    sections = [(n, c) for n, t in texts for c in _chunks(t)]
    if not sections:
        raise LabsError("Nothing to read in those files (a scanned PDF needs OCR first).", "bad_data")
    bid = _new_id()
    os.makedirs(os.path.join(DATA_DIR, "drafts"), exist_ok=True)
    draft = {"id": bid, "name": (name or texts[0][0]).strip()[:80], "pairs": [], "sections": len(sections), "created": time.time()}
    _write_json(os.path.join(DATA_DIR, "drafts", bid + ".json"), draft)
    _build.update(state="running", id=bid, done=0, total=len(sections), error="", pairs=0, cancel=False)
    threading.Thread(target=_run_build, args=(bid, sections, max(1, min(10, int(per_section))), model), daemon=True,
                     name="labs-build").start()
    return build_status()


def _run_build(bid: str, sections: list, n: int, model: str | None):
    import agent.models as llm
    path = os.path.join(DATA_DIR, "drafts", bid + ".json")
    draft = _read_json(path)
    for i, (doc, sec) in enumerate(sections):
        if _build.get("cancel"):
            _build.update(state="done", error=f"Stopped after {i} of {len(sections)} sections - what it wrote is kept.")
            break
        try:
            r = llm.model_text(BUILD_SYSTEM.format(n=n), f"Document: {doc}\n\nSection:\n{sec}", max_tokens=1600,
                               model=model, temperature=0.4, label="Writing training examples")
            txt = r.get("text") or ""
            m = re.search(r"\{[\s\S]*\}", txt)
            pairs = (json.loads(m.group(0)).get("pairs") if m else None) or []
            for p in pairs:
                if isinstance(p, dict) and str(p.get("q") or "").strip() and str(p.get("a") or "").strip():
                    draft["pairs"].append({"q": str(p["q"]).strip(), "a": str(p["a"]).strip(), "src": doc})
        except Exception as e:                                # noqa: BLE001 - one section failing doesn't end the build
            _build["error"] = f"Section {i + 1}: {str(e)[:200]}"
            if "llama-server" in str(e) or "not answering" in str(e).lower():
                _build.update(state="error")
                break
        _build.update(done=i + 1, pairs=len(draft["pairs"]))
        _write_json(path, draft)
    if _build["state"] == "running":
        _build["state"] = "done"


def build_cancel() -> dict:
    """Stop writing examples after the section in hand; what was written stays in the draft."""
    if _build["state"] == "running":
        _build["cancel"] = True
    return build_status()


def drafts() -> list:
    try:
        names = sorted(os.listdir(os.path.join(DATA_DIR, "drafts")), reverse=True)
    except OSError:
        return []
    out = []
    for n in names:
        d = _read_json(os.path.join(DATA_DIR, "drafts", n))
        if d:
            out.append({k: d.get(k) for k in ("id", "name", "sections", "created")} | {"count": len(d.get("pairs") or [])})
    return out


def draft(bid: str) -> dict:
    d = _read_json(os.path.join(DATA_DIR, "drafts", f"{bid}.json")) if _ID.fullmatch(str(bid or "")) else None
    if not d:
        raise LabsError("That draft no longer exists.", "missing")
    return d


def draft_save(bid: str, name: str, pairs: list) -> dict:
    """The reviewed pairs become a chat dataset; the draft goes."""
    draft(bid)
    recs = [{"prompt": p.get("q"), "response": p.get("a")} for p in pairs or [] if str(p.get("q") or "").strip() and str(p.get("a") or "").strip()]
    meta = add_chat(name, records=recs)
    os.remove(os.path.join(DATA_DIR, "drafts", f"{bid}.json"))
    return meta


def draft_delete(bid: str) -> None:
    draft(bid)
    os.remove(os.path.join(DATA_DIR, "drafts", f"{bid}.json"))


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


# ---- Settings: what the network view sends back, checked here (the page is not trusted) -----------------------
ARCH_KEYS = {"gpt": ("n_layer", "n_head", "n_embd", "block", "blocks", "act", "tie"), "cnn": ("px", "width", "stages"),
             "lora": ("rank", "alpha_mult", "targets", "layers", "max_len")}


def _int(v, lo, hi, what):
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise LabsError(f"{what} must be a whole number.", "bad_data") from None
    if not lo <= n <= hi:
        raise LabsError(f"{what} must be between {lo:,} and {hi:,} (it was {n:,}).", "bad_data")
    return n


def validate_settings(template: str, size: str, settings: dict) -> dict:
    """Every setting a run can have, checked and filled in from the size's preset. Raises LabsError naming the
    setting and the allowed range - never a silent clamp."""
    s = dict(PRESETS[template].get(size) or {})
    s.update({k: v for k, v in (settings or {}).items() if v is not None and v != ""})
    out = {}
    if "lr" in s:
        try:
            lr = float(s["lr"])
        except (TypeError, ValueError):
            raise LabsError("The learning rate must be a number.", "bad_data") from None
        if not 1e-6 <= lr <= 0.1:
            raise LabsError("The learning rate must be between 0.000001 and 0.1.", "bad_data")
        out["lr"] = lr
    if "batch" in s:
        out["batch"] = _int(s["batch"], 1, 256, "Examples per step")
    if "seed" in s:
        out["seed"] = _int(s["seed"], 0, 2**31 - 1, "The seed")
    if template == "gpt":
        out["n_layer"] = _int(s["n_layer"], 1, 16, "Layers")
        out["n_embd"] = _int(s["n_embd"], 32, 1024, "Width")
        out["n_head"] = _int(s["n_head"], 1, 32, "Heads")
        if out["n_embd"] % out["n_head"] or out["n_embd"] // out["n_head"] < 8:
            raise LabsError(f"Width ({out['n_embd']}) must split evenly into the heads ({out['n_head']}), at least 8 per head.", "bad_data")
        out["block"] = _int(s["block"], 16, 1024, "Context")
        # Each block can differ: its own heads and MLP size (the width stays shared - the blocks add into one stream).
        blocks = s.get("blocks")
        if blocks is not None:
            if not isinstance(blocks, list) or not 1 <= len(blocks) <= 16:
                raise LabsError("The network needs 1 to 16 blocks.", "bad_data")
            out["blocks"] = []
            for i, b in enumerate(blocks):
                b = b if isinstance(b, dict) else {}
                h = _int(b.get("heads", out["n_head"]), 1, 32, f"Block {i + 1}'s heads")
                if out["n_embd"] % h or out["n_embd"] // h < 8:
                    raise LabsError(f"Block {i + 1}: the width ({out['n_embd']}) must split evenly into its {h} heads, at least 8 per head.", "bad_data")
                out["blocks"].append({"heads": h, "mlp": _int(b.get("mlp", 4), 1, 8, f"Block {i + 1}'s MLP size")})
            out["n_layer"] = len(out["blocks"])
        act = s.get("act", "gelu")
        if act not in ("gelu", "relu", "silu"):
            raise LabsError("The activation must be GELU, ReLU or SiLU.", "bad_data")
        out["act"] = act
        out["tie"] = bool(s.get("tie", True))
        if "dropout" in s:
            try:
                out["dropout"] = float(s["dropout"])
            except (TypeError, ValueError):
                raise LabsError("Dropout must be a number.", "bad_data") from None
            if not 0 <= out["dropout"] <= 0.5:
                raise LabsError("Dropout must be between 0 and 0.5.", "bad_data")
    elif template == "cnn":
        out["width"] = _int(s["width"], 8, 128, "Width")
        out["px"] = _int(s["px"], 16, 256, "Picture size")
        stages = s.get("stages")
        if stages is not None:
            if not isinstance(stages, list) or not 1 <= len(stages) <= 6:
                raise LabsError("The network needs 1 to 6 stages.", "bad_data")
            out["stages"] = [{"ch": _int((x or {}).get("ch"), 4, 512, f"Stage {i + 1}'s filters"),
                              "convs": _int((x or {}).get("convs", 2), 1, 4, f"Stage {i + 1}'s layers")} for i, x in enumerate(stages)]
        halvings = len(out.get("stages") or [0, 0, 0, 0]) - 1
        if out["px"] % (2 ** halvings) or out["px"] // (2 ** halvings) < 2:
            raise LabsError(f"The picture size must be a multiple of {2 ** halvings} (it is halved {halvings} times).", "bad_data")
        if "dropout" in s:
            try:
                out["dropout"] = float(s["dropout"])
            except (TypeError, ValueError):
                raise LabsError("Dropout must be a number.", "bad_data") from None
            if not 0 <= out["dropout"] <= 0.7:
                raise LabsError("Dropout must be between 0 and 0.7.", "bad_data")
    elif template == "lora":
        base = BASE_CONFIGS.get(size) or BASE_CONFIGS["0.8b"]
        out["rank"] = _int(s["rank"], 1, 256, "Rank")
        out["alpha_mult"] = _int(s.get("alpha_mult", 2), 1, 8, "Alpha multiplier")
        out["max_len"] = _int(s.get("max_len", 1024), 64, 8192, "Longest example (tokens)")
        t = s.get("targets") or {}
        out["targets"] = {k: bool(t.get(k, True)) for k in ("attn", "delta", "mlp", "gates")}
        if not any(out["targets"].values()):
            raise LabsError("Pick at least one part of the network to fine-tune.", "bad_data")
        layers = s.get("layers")
        if layers is not None:
            layers = sorted({_int(x, 0, base["layers"] - 1, "A layer number") for x in layers})
            if not layers:
                raise LabsError("Pick at least one layer to fine-tune.", "bad_data")
        out["layers"] = layers
    return out


# ---- Models: a model is one thing you make, its runs are its versions --------------------------------------------
def _model_id(meta: dict) -> str:
    return meta.get("model") or meta["id"]


def models() -> list:
    """Every model with its versions (oldest first), newest model first."""
    groups = {}
    for r in runs():
        groups.setdefault(_model_id(r), []).append(r)
    out = []
    for mid, vs in groups.items():
        vs.sort(key=lambda r: (r.get("version") or 1, r.get("created") or 0))
        latest = vs[-1]
        out.append({"id": mid, "name": latest["name"], "template": latest["template"], "size": latest.get("size"),
                    "versions": vs, "latest": latest["id"], "updated": max(v.get("ended") or v.get("started") or v.get("created") or 0 for v in vs)})
    out.sort(key=lambda m: m["updated"], reverse=True)
    return out


def new_model(template: str, name: str = "", dataset_id: str | None = None, size: str | None = None) -> dict:
    """A model in its first version, as a draft: nothing trains until Start."""
    if template not in TEMPLATES:
        raise LabsError("Unknown kind of model.", "bad_data")
    t = TEMPLATES[template]
    size = size if size in t["sizes"] else ("small" if "small" in t["sizes"] else next(iter(t["sizes"])))
    rid = _new_id()
    os.makedirs(os.path.join(RUNS_DIR, rid))
    meta = {"id": rid, "model": rid, "version": 1, "name": (name or t["name"]).strip()[:100], "template": template,
            "size": size, "steps": t["sizes"][size]["steps"], "dataset": {}, "settings": validate_settings(template, size, {}),
            "state": "draft", "created": time.time(), "notes": ""}
    _write_json(os.path.join(RUNS_DIR, rid, "run.json"), meta)
    if dataset_id:
        meta = update_draft(rid, {"dataset": dataset_id})
    return meta


def update_draft(rid: str, body: dict) -> dict:
    """Change a version that hasn't trained yet: its name, data, size (which resets the network to that preset),
    steps and settings. A new version of a trained model keeps its network (its weights fit only that shape)."""
    meta = run(rid)
    if meta["state"] != "draft":
        raise LabsError("This version has trained already - make a new version to change it.", "busy")
    t = TEMPLATES[meta["template"]]
    if body.get("name") is not None and str(body["name"]).strip():
        meta["name"] = str(body["name"]).strip()[:100]
    if body.get("dataset") is not None:
        if body["dataset"]:
            ds = dataset(body["dataset"])
            if ds["kind"] != t["data"]:
                raise LabsError(f"{t['name']} learns from {DATA_WORDS[t['data']]} - pick a dataset of that kind.", "bad_data")
            meta["dataset"] = {k: ds.get(k) for k in ("id", "name", "kind", "path")}
        else:
            meta["dataset"] = {}
    settings = dict(meta.get("settings") or {})
    if body.get("size") and body["size"] != meta["size"]:
        if meta.get("locked"):
            raise LabsError("This version continues from trained weights, so its size can't change.", "bad_data")
        if body["size"] not in t["sizes"]:
            raise LabsError("Unknown size.", "bad_data")
        meta["size"] = body["size"]
        settings = {k: v for k, v in settings.items() if k not in ARCH_KEYS[meta["template"]]}
        if not body.get("steps"):
            meta["steps"] = t["sizes"][meta["size"]]["steps"]
    if body.get("settings"):
        new = body["settings"]
        if meta.get("locked") and any(k in ARCH_KEYS[meta["template"]] and new[k] != settings.get(k) for k in new):
            raise LabsError("This version continues from trained weights, so its network can't change - only how it trains.", "bad_data")
        settings.update(new)
    meta["settings"] = validate_settings(meta["template"], meta["size"], settings)
    if body.get("steps") is not None:
        meta["steps"] = _int(body["steps"], 10, 1_000_000, "Steps")
    _write_json(os.path.join(_run_dir(rid), "run.json"), {k: v for k, v in meta.items() if k != "last"})
    return meta


DATA_WORDS = {"text": "a text file", "images": "a picture folder", "chat": "chat examples"}


def new_version(rid: str) -> dict:
    """The next version of a model: it starts from this one's weights (a copy - this version stays as it is), with
    the same network and data, ready to train longer or with other settings."""
    parent = run(rid)
    if parent["state"] in ("draft", "running") or rid == _running:
        raise LabsError("Finish or stop this version first.", "busy")
    pf = _run_dir(rid)
    if not os.path.exists(os.path.join(pf, "ckpt.pt")) and not os.path.exists(os.path.join(pf, "adapter")):
        raise LabsError("This version has no trained weights to continue from.", "missing")
    mid = _model_id(parent)
    versions = [r for r in runs() if _model_id(r) == mid]
    nid = _new_id()
    nf = os.path.join(RUNS_DIR, nid)
    os.makedirs(nf)
    for name in ("ckpt.pt", "metrics.jsonl", "samples.jsonl", "base_answers.json"):
        if os.path.exists(os.path.join(pf, name)):
            shutil.copy2(os.path.join(pf, name), os.path.join(nf, name))
    for name in ("adapter", "samples"):
        if os.path.isdir(os.path.join(pf, name)):
            shutil.copytree(os.path.join(pf, name), os.path.join(nf, name))
    done = int(_tail_metric(rid).get("step") or parent["steps"])
    meta = {"id": nid, "model": mid, "version": max(v.get("version") or 1 for v in versions) + 1, "parent": rid,
            "name": parent["name"], "template": parent["template"], "size": parent["size"],
            "steps": done + TEMPLATES[parent["template"]]["sizes"][parent["size"]]["steps"], "from_step": done,
            "dataset": parent.get("dataset") or {}, "settings": dict(parent.get("settings") or {}), "locked": True,
            "state": "draft", "created": time.time(), "notes": ""}
    _write_json(os.path.join(nf, "run.json"), meta)
    return meta


def delete_model(mid: str) -> int:
    vs = [r for r in runs() if _model_id(r) == mid]
    if not vs:
        raise LabsError("That model no longer exists.", "missing")
    if any(r["id"] == _running and gpu_busy() for r in vs):
        raise LabsError("Stop its training before deleting it.", "busy")
    for r in vs:
        delete_run(r["id"])
    return len(vs)


def create(template: str, dataset_id: str, size: str = "small", name: str = "", steps: int | None = None,
           settings: dict | None = None) -> dict:
    """One call: a new model with this data and settings, started (scripts and older pages use this)."""
    meta = new_model(template, name, dataset_id, size)
    meta = update_draft(meta["id"], {"steps": steps, "settings": settings or {}})
    return start(meta["id"])


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
        if not (meta.get("dataset") or {}).get("id") and not (meta.get("dataset") or {}).get("path"):
            raise LabsError("Pick the data it learns from first (the Data step).", "bad_data")
        if meta["template"] == "lora" and not engine().get("finetune"):
            raise LabsError("The fine-tuning tools are missing - Labs › Machine › Repair or update.", "not_installed")
        meta["settings"] = validate_settings(meta["template"], meta["size"], meta.get("settings") or {})
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
        data = {"text": lambda: os.path.join(DATA_DIR, ds["id"], "data.txt"),
                "chat": lambda: os.path.join(DATA_DIR, ds["id"], "data.jsonl")}.get(ds["kind"], lambda: ds["path"])()
        job = {"run": folder, "template": meta["template"], "size": meta["size"], "steps": meta["steps"],
               "resume": resume, "settings": meta.get("settings") or {}, "data": data, **_base_job(meta)}
        _write_json(os.path.join(folder, "job.json"), job)
        log = open(os.path.join(folder, "worker.log"), "ab")
        _proc = subprocess.Popen([python(), "-u", WORKER, os.path.join(folder, "job.json")], cwd=folder, stdout=log,
                                 stderr=subprocess.STDOUT, env=_worker_env(),
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        _running = rid
        threading.Thread(target=_watch, args=(rid, _proc, log), daemon=True, name="labs-run").start()
        return meta


def _base_job(meta: dict) -> dict:
    """A fine-tune's base model: its repo, where it is kept (downloaded the first time), its name."""
    if meta.get("template") != "lora":
        return {}
    repo, folder, _q = BASES.get(meta.get("size"), BASES["0.8b"])
    return {"repo": repo, "base_dir": os.path.join(engine_dir(), "bases", folder), "base_label": folder}


def _worker_env() -> dict:
    """The worker's caches and downloads stay on the Labs drive (Hugging Face and Triton default to C:), and its
    downloads follow the app's network route."""
    env = _pip_env()
    env.update(HF_HOME=os.path.join(engine_dir(), "hf"), HF_HUB_DISABLE_TELEMETRY="1",
               TRITON_CACHE_DIR=os.path.join(engine_dir(), "triton-cache"), PYTHONIOENCODING="utf-8")
    return env


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
    """A GPT run writes text from its checkpoint (on the CPU while another run trains); a fine-tune answers the prompt,
    with the original model's answer beside it (its own short worker run, on the GPU)."""
    folder = _run_dir(rid)
    meta = run(rid)
    if meta["template"] not in ("gpt", "lora"):
        raise LabsError("Only a GPT run or a fine-tune writes text.", "bad_data")
    lora = meta["template"] == "lora"
    if not os.path.exists(os.path.join(folder, "adapter" if lora else "ckpt.pt")):
        raise LabsError("This run has no checkpoint yet - let it train a little first.", "missing")
    if not engine()["installed"]:
        raise LabsError("Set up the Labs engine first.", "not_installed")
    if lora and gpu_busy():
        raise LabsError("A run is training on the GPU - try the fine-tune when it has finished.", "busy")
    if lora:
        _free_gpu()
    # The same rules for every model: a prompt of up to 4,000 characters, 20 to 2,000 tokens back, temperature 0 to 2.
    prompt = str(prompt or "")
    if len(prompt) > 4000:
        raise LabsError(f"The prompt is {len(prompt):,} characters - Try it takes up to 4,000.", "bad_data")
    length = _int(length, 20, 2000, "Length")
    temperature = 0.0 if temperature is None else float(temperature)
    if not 0 <= temperature <= 2:
        raise LabsError("Temperature must be between 0 and 2.", "bad_data")
    if lora:
        _free_gpu()
        job = {"mode": "generate_lora", "run": folder, "prompt": prompt, "length": length, "temperature": temperature,
               **_base_job(meta)}
    else:
        job = {"mode": "generate", "run": folder, "prompt": prompt, "length": length,
               "temperature": max(0.05, temperature), "device": "cpu" if gpu_busy() else "auto"}
    path = os.path.join(folder, "gen.json")
    _write_json(path, job)
    try:
        r = subprocess.run([python(), "-u", WORKER, path], cwd=folder, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=600 if lora else 180, env=_worker_env(),
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise LabsError("Writing took too long.", "failed") from None
    lines = [x for x in r.stdout.splitlines() if x.startswith("{")]
    if r.returncode != 0 or not lines:
        raise LabsError((r.stderr or r.stdout or "It didn't answer.").strip().splitlines()[-1][:300], "failed")
    out = json.loads(lines[-1])
    if out.get("ok") is False:
        raise LabsError(out.get("message") or "It didn't answer.", "failed")
    return out


def trace(rid: str, prompt: str = "", picture: str = "") -> dict:
    """One real pass through a run's checkpoint on the CPU: each layer's strongest units and what flowed between
    them (the network view's Trace). A small GPT takes text; a picture classifier one of its held-out pictures."""
    folder = _run_dir(rid)
    meta = run(rid)
    if meta["template"] not in ("gpt", "cnn"):
        raise LabsError("Trace works on the small GPT and the picture classifier - a fine-tune shows which layers learned instead.", "bad_data")
    if not os.path.exists(os.path.join(folder, "ckpt.pt")):
        raise LabsError("It has no checkpoint yet - let it train a little first.", "missing")
    if not engine()["installed"]:
        raise LabsError("Set up the Labs engine first.", "not_installed")
    job = {"mode": "trace", "template": meta["template"], "run": folder}
    if meta["template"] == "gpt":
        prompt = str(prompt or "")
        if len(prompt) > 1024:
            raise LabsError("Trace reads up to 1,024 characters.", "bad_data")
        job["prompt"] = prompt
    else:
        name = os.path.basename(str(picture or ""))
        if not re.fullmatch(r"v\d+\.png", name) or not os.path.exists(os.path.join(folder, "samples", name)):
            raise LabsError("Pick one of its held-out pictures.", "bad_data")
        job["picture"] = os.path.join(folder, "samples", name)
    path = os.path.join(folder, "trace.json")
    _write_json(path, job)
    try:
        r = subprocess.run([python(), "-u", WORKER, path], cwd=folder, capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=120, env={**_worker_env(), "CUDA_VISIBLE_DEVICES": ""},
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        raise LabsError("The trace took too long.", "failed") from None
    lines = [x for x in r.stdout.splitlines() if x.startswith("{")]
    if r.returncode != 0 or not lines:
        raise LabsError((r.stderr or r.stdout or "No answer.").strip().splitlines()[-1][:300], "failed")
    out = json.loads(lines[-1])
    if out.get("ok") is False:
        raise LabsError(out.get("message") or "No answer.", "failed")
    return out


# ==============================================================================
# Port: a fine-tune becomes a Workspace model
# ==============================================================================
# It stays in Labs - tried, compared with the original - until you port it: then it is merged into its base,
# converted to GGUF, quantized, saved beside the other models on the Labs drive and added to models.ini.
_port = {}                    # run id -> {"state": running|done|error, "stage", "error", "model"}


def port_status(rid: str) -> dict:
    return dict(_port.get(rid) or {})


def port(rid: str) -> dict:
    meta = run(rid)
    folder = _run_dir(rid)
    if meta["template"] != "lora":
        raise LabsError("Only a fine-tuned chat model can be ported to Workspace.", "bad_data")
    if not os.path.exists(os.path.join(folder, "adapter", "adapter_config.json")):
        raise LabsError("This run has no fine-tune saved yet - let it train first.", "missing")
    if gpu_busy() or any(p.get("state") == "running" for p in _port.values()):
        raise LabsError("Busy - a run is training or another port is running.", "busy")
    if not (convert_dir() and quantize_exe()):
        raise LabsError("Port needs llama.cpp's converter and llama-quantize - run Labs > Engine > Repair or update.", "not_installed")
    _need_space(folder, 12.0, "porting (the merged model and its GGUF, briefly)")
    _port[rid] = {"state": "running", "stage": "Starting", "error": ""}
    threading.Thread(target=_run_port, args=(rid, meta, folder), daemon=True, name="labs-port").start()
    return port_status(rid)


def _run_port(rid: str, meta: dict, folder: str):
    import llama_local
    import model_scan
    st = _port[rid]
    try:
        _repo, base_name, quant = BASES.get(meta["size"], BASES["0.8b"])
        safe = re.sub(r"[^\w.-]+", "-", meta["name"]).strip("-")[:60] or rid
        models_dir = os.path.join(os.path.dirname(engine_dir()), "models", "Labs")
        os.makedirs(models_dir, exist_ok=True)
        out = os.path.join(models_dir, f"{safe}-{quant}.gguf")
        work = os.path.join(folder, "port-work")
        job = {"mode": "port", "run": folder, "work": work, "out": out, "quant": quant, "convert_dir": convert_dir(),
               "quantize": quantize_exe(), **_base_job(meta)}
        path = os.path.join(folder, "port.json")
        _write_json(path, job)
        proc = subprocess.Popen([python(), "-u", WORKER, path], cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", env=_worker_env(),
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        result, tail = None, []
        for line in proc.stdout:
            tail = (tail + [line.rstrip()])[-20:]
            if line.startswith("{"):
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "stage" in msg:
                    st["stage"] = msg["stage"]
                if "ok" in msg:
                    result = msg
        proc.wait()
        if not result or not result.get("ok"):
            raise LabsError((result or {}).get("message") or (tail[-1] if tail else "The port stopped."), "failed")
        st["stage"] = "Adding it to Workspace"
        try:
            added = model_scan.add_file(out)
            name = added["name"]
        except ValueError:                                     # ported before: the same file, already listed
            name = next((c["name"] for c in llama_local.list_model_configs()
                         if os.path.normcase(c.get("model_path") or "") == os.path.normcase(out)), safe)
        llama_local.set_model_options({name: {"ctx-size": 32768}}, backup=False)
        try:
            model_scan.restart_router()
        except Exception:                                      # noqa: BLE001 - the model loads with the next restart then
            pass
        meta = run(rid)
        meta["ported"] = {"model": name, "path": out, "mb": result.get("mb"), "at": time.time(), "base": base_name}
        _write_json(os.path.join(folder, "run.json"), {k: v for k, v in meta.items() if k != "last"})
        st.update(state="done", stage="", model=name)
    except Exception as e:                                    # noqa: BLE001 - shown on the run as it is
        st.update(state="error", error=str(e)[:400], stage="")
    finally:
        shutil.rmtree(os.path.join(folder, "port-work"), ignore_errors=True)    # the merged copy and the bf16 GGUF


def status() -> dict:
    return {"engine": engine(), "setup": setup_status(), "running": _running if gpu_busy() else None,
            "templates": TEMPLATES, "presets": PRESETS, "bases": {k: {**v, "downloaded": os.path.exists(
                os.path.join(engine_dir(), "bases", BASES[k][1], "config.json"))} for k, v in BASE_CONFIGS.items()},
            "build": build_status(), "ports": {k: dict(v) for k, v in _port.items()}}
