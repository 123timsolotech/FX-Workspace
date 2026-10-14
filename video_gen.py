"""
video_gen.py
============
Local video generation: stable-diffusion.cpp's sd-cli in its video mode (-M vid_gen), the same engine image_gen.py
uses for pictures - nothing is sent anywhere.

  turbo   Wan 2.2 TI2V 5B Turbo (quanhaol, Apache-2.0; GGUF by hum-ma): text -> video and image -> video in 4
          steps without CFG (step and CFG distilled), 24 fps. Files in settings video_root (default
          G:\\ClankerLocalCpp\\Videos\\models): the Q5_K_M model, the Wan 2.2 VAE, the umt5-xxl text encoder (Q5_K_M).

One clip at a time. The GPU (8 GB) can't hold a chat model, the image engine and this together: before a clip, the
image engine stops and (with image_free_gpu on) the chat model is unloaded; both come back on their own when next
needed. Each clip is its own sd-cli run: the model files stay in Windows' file cache, so later clips load quickly.

Made clips: data/videos/<id>.webm (plays in the page, no ffmpeg) with <id>.json beside it (prompt, model, seed,
size, frames, fps, seconds, the reference picture's id). Trash: data/videos/trash/. A clip is deleted for good only
from the trash, on the user's click (purge, empty_trash).

Prompts pass the image filter's check (image_gen.check_prompt): the same always-on and optional refusals.
"""
from __future__ import annotations

import paths
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time

import image_gen

ROOT = os.path.dirname(os.path.abspath(__file__))
VIDEOS_DIR = os.path.join(ROOT, "data", "videos")
DEFAULT_ROOT = paths.feature_root("Videos")

MODELS = {
    "turbo": {"name": "Wan 2.2 TI2V 5B Turbo", "by": "Wan-AI / quanhaol", "license": "Apache-2.0",
              "diffusion": "Wan2_2-TI2V-5B-Turbo-Q5_K_M.gguf", "t5xxl": "umt5-xxl-encoder-Q5_K_M.gguf",
              "vae": "wan2.2_vae.safetensors", "tae": "taew2_2.pth", "steps": 4, "cfg": 1.0, "sampler": "euler",
              "fps": 24, "about": "Text or a picture to a short clip, 4 steps."},
    # The same model with an anime LoRA (hjhf's wan2.2_5B anime_v1.0, trained on anime stills for T2I -> I2V).
    # Compared 2026-10-14 on one Animagine still against Wan 2.2 I2V 14B (+ Lightning) and AnimateDiff (SD1.5 +
    # Anything V5): this kept the character sharp and on-model with lively motion in 3.3 min (2 s, full VAE); the
    # 14B blurred over time (sd.cpp crashes with any LoRA on its low-noise model, so its speed LoRA can't be used);
    # AnimateDiff was soft and washed out.
    "anime": {"name": "Wan 2.2 5B Turbo + anime LoRA", "by": "Wan-AI / quanhaol / hjhf", "license": "Apache-2.0 (LoRA: not stated)",
              "diffusion": "Wan2_2-TI2V-5B-Turbo-Q5_K_M.gguf", "t5xxl": "umt5-xxl-encoder-Q5_K_M.gguf",
              "vae": "wan2.2_vae.safetensors", "tae": "taew2_2.pth", "steps": 4, "cfg": 1.0, "sampler": "euler",
              "fps": 24, "lora": ("wan22_5B_anime_v1.0", 0.8), "prefix": "anime style, ",
              "about": "2D / anime clips - best when it starts from one of your anime pictures."},
}
# Measured here (2026-10-13, RTX 3060 Ti, 832x480, 49 frames, models on G: - a hard disk):
#   --offload-to-cpu + --vae-tiling: 1135 s - the text encoder alone took 885 s shuttled RAM<->GPU.
#   auto-fit (--max-vram -1.5, --mmap): 127 s - prompt 2.4 s, 4 steps 12.6 s, full VAE decode 105 s (it ran out
#     of memory untiled and fell back to small tiles by itself).
#   + the tiny decoder (taew2_2, madebyollin/taehv, MIT): 22 s - decode 1.3 s, slightly less fine detail.
# So Fast decode (the tiny decoder) is the default; "Best quality" uses the full Wan 2.2 VAE.
# Wan 2.2's VAE works in 32-pixel blocks; its native size is 1280x704. Smaller is much faster.
SIZES = {"landscape": (832, 480), "portrait": (480, 832), "square": (640, 640),
         "hd": (1280, 704), "hd_portrait": (704, 1280)}
LENGTHS = {"short": 33, "medium": 49, "long": 81, "longest": 121}     # frames (4n+1); at 24 fps: 1.4 / 2 / 3.4 / 5 s
DEFAULT_SETTINGS = {"video_root": DEFAULT_ROOT, "video_size": "landscape", "video_length": "medium",
                    "video_best_quality": True}

_PATH_RE = re.compile(r"[A-Za-z]:\\[^<>\"|?*]{0,200}")
_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")
_BAR = re.compile(r"\|\s*(\d+)/(\d+) - ([\d.]+)(s/it|it/s)")

_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_job: dict = {}
_stop_requested = threading.Event()
_last_error = ""
_last_code = ""
_typical = {"turbo": 25.0}            # seconds per 49-frame clip (fast decode), replaced by each real one
_settings = lambda: {}                # noqa: E731


class VideoError(Exception):
    """A reason the user can act on. code: filter, minors, empty, not_installed, stopped, crashed, failed, missing."""

    def __init__(self, message: str, code: str = "failed"):
        super().__init__(message)
        self.code = code


def configure(settings_fn) -> None:
    global _settings
    _settings = settings_fn


def clean_settings(s: dict) -> dict:
    root = str(s.get("video_root") or DEFAULT_ROOT).strip()
    return {"video_root": root if _PATH_RE.fullmatch(root) else DEFAULT_ROOT,
            "video_size": s.get("video_size") if s.get("video_size") in SIZES else "landscape",
            "video_length": s.get("video_length") if s.get("video_length") in LENGTHS else "medium",
            # The tiny decoder flickers: per-frame saturation 0.17-0.26 vs 0.17-0.20 for the full VAE (same seed,
            # measured 2026-10-13), so the full VAE is the default and Fast is a preview.
            "video_best_quality": s.get("video_best_quality") if isinstance(s.get("video_best_quality"), bool) else True}


def _cfg() -> dict:
    return clean_settings(_settings() or {})


# ---- files ------------------------------------------------------------------------------------------------
def cli_exe() -> str | None:
    """The video engine: <video_root>\\engine\\sd-cli.exe (a newer stable-diffusion.cpp build, master-964, verified
    by its release digest) when it is there, else sd-cli.exe beside the image engine's sd-server.exe."""
    own = os.path.join(_cfg()["video_root"], "engine", "sd-cli.exe")
    if os.path.exists(own):
        return own
    server = image_gen.server_exe()
    if server:
        cli = os.path.join(os.path.dirname(server), "sd-cli.exe")
        if os.path.exists(cli):
            return cli
    return None


def model_files(key: str = "turbo") -> dict:
    """The required files (diffusion model, text encoder, VAE) - plus "tae", the optional fast decoder."""
    m, models = MODELS[key], os.path.join(_cfg()["video_root"], "models")
    return {part: os.path.join(models, m[part]) for part in ("diffusion", "t5xxl", "vae", "tae") if m.get(part)}


def _lora_path(key: str) -> str | None:
    lora = MODELS[key].get("lora")
    return os.path.join(_cfg()["video_root"], "models", "loras", lora[0] + ".safetensors") if lora else None


def installed(key: str = "turbo") -> bool:
    lora = _lora_path(key)
    return (all(os.path.exists(p) for part, p in model_files(key).items() if part != "tae")
            and (lora is None or os.path.exists(lora)))


# ---- progress, from the engine's output -------------------------------------------------------------------
# Seconds per "pixel-frame" (width x height x frames) for each stage, measured here (2026-10-13, 832x480x49 =
# 19.6 M): one sampling step 3.15 s, the full VAE 105 s, the fast decoder 1.3 s. Replaced by each finished clip.
_RATES = {"step": 3.15 / 19.6e6, "decode_best": 105 / 19.6e6, "decode_fast": 1.6 / 19.6e6}
_FIXED_S = 8.0                        # loading + reading the prompt (2-6 s once the files are in Windows' cache)


def _secs(text: str, label: str) -> float | None:
    m = re.search(label + r" completed, taking ([\d.]+)s", text)
    return float(m.group(1)) if m else None


def parse_progress(text: str) -> dict:
    """What sd-cli's output says so far. Stages: loading, encoding (the prompt), drawing (sampling bars before
    "generating latent video completed"), decoding (the VAE's tile bars after it), saving. Also how often the
    decoder ran out of GPU memory and started again in smaller tiles (each restart is much slower)."""
    out = {"stage": "loading", "step": 0, "steps": 0, "sec_per_step": 0.0,
           "tile": 0, "tiles": 0, "sec_per_tile": 0.0, "retries": 0}
    cut = text.find("generating latent video completed")
    sampling, decoding = (text, "") if cut < 0 else (text[:cut], text[cut:])
    if re.search(r"get_learned_condition completed|sampling using", sampling):
        out["stage"] = "encoding"
    for b in _BAR.finditer(sampling):
        rate = float(b.group(3))
        out.update(stage="drawing", step=int(b.group(1)), steps=int(b.group(2)),
                   sec_per_step=rate if b.group(4) == "s/it" else (1 / rate if rate else 0.0))
    if cut >= 0:
        out["stage"] = "decoding"
        out["retries"] = len(re.findall(r"ran out of memory; retrying", decoding))
        # Tile bars belong to the latest attempt only (a retry starts the count again).
        last = max(decoding.rfind("retrying"), 0)
        for b in _BAR.finditer(decoding[last:]):
            rate = float(b.group(3))
            out.update(tile=int(b.group(1)), tiles=int(b.group(2)),
                       sec_per_tile=rate if b.group(4) == "s/it" else (1 / rate if rate else 0.0))
        if "decode_first_stage completed" in decoding:
            out["stage"] = "saving"
    return out


def estimate_left(p: dict, w: int, h: int, frames: int, steps: int, fast: bool, spent: float) -> float:
    """Seconds left, stage by stage: the engine's real step / tile speed once it shows one, the measured rates
    (_RATES) before that. A decoder restart in smaller tiles (p["retries"]) makes the full VAE far slower."""
    px = w * h * frames
    step_s = p["sec_per_step"] or _RATES["step"] * px
    decode_s = (_RATES["decode_fast"] if fast else _RATES["decode_best"]) * px
    # The full VAE fits 256x240 tiles up to about 832x480x49; past that (or once it cut the tiles again) it works
    # in 128x112 tiles - about 4x as many (an 832x480x81 clip did, 2026-10-13).
    if not fast and (p["retries"] >= 3 or px > 21e6):
        decode_s *= 4
    stage = p["stage"]
    if stage == "loading":
        return max(0.0, _FIXED_S - spent) + steps * step_s + decode_s + 1
    if stage == "encoding":
        return steps * step_s + decode_s + 1
    if stage == "drawing":
        return max(0, steps - p["step"]) * step_s + decode_s + 1
    if stage == "decoding":
        if p["tiles"] and p["sec_per_tile"]:
            return max(0, p["tiles"] - p["tile"]) * p["sec_per_tile"] + 1
        return decode_s + 1
    return 1.0


def estimates(key: str = "turbo") -> dict:
    """Seconds for a whole clip, before it starts - {"<size>|<length>|fast|best": s} for the page's "about ..."."""
    steps = MODELS[key]["steps"]
    blank = {"stage": "loading", "step": 0, "steps": 0, "sec_per_step": 0.0, "tile": 0, "tiles": 0,
             "sec_per_tile": 0.0, "retries": 0}
    return {f"{s}|{L}|{'fast' if fast else 'best'}": round(estimate_left(blank, w, h, n, steps, fast, 0.0))
            for s, (w, h) in SIZES.items() for L, n in LENGTHS.items() for fast in (True, False)}


def learn_rates(text: str, w: int, h: int, frames: int, steps: int, fast: bool) -> None:
    """After a clip: keep its real speeds for the next estimate. The full VAE always restarts twice before it fits
    (whole clip -> temporal tiles -> 256x240 tiles, measured); a decode cut down further is not typical, so it
    doesn't replace the rate."""
    px = max(1, w * h * frames)
    sampled, decoded = _secs(text, "sampling"), _secs(text, "decode_first_stage")
    if sampled:
        _RATES["step"] = sampled / max(1, steps) / px
    if decoded and parse_progress(text)["retries"] < 3:
        _RATES["decode_fast" if fast else "decode_best"] = decoded / px


# ---- generating -------------------------------------------------------------------------------------------
def parse_size(size) -> tuple:
    if isinstance(size, str) and size in SIZES:
        return SIZES[size]
    m = re.fullmatch(r"\s*(\d{3,4})\s*[x×*]\s*(\d{3,4})\s*", str(size or ""))
    if m:
        w, h = (max(256, min(1280, int(v))) // 32 * 32 for v in m.groups())
        return w, h
    return SIZES[_cfg()["video_size"]]


def parse_frames(length) -> int:
    if isinstance(length, str) and length in LENGTHS:
        return LENGTHS[length]
    try:
        n = max(9, min(121, int(length)))
        return (n - 1) // 4 * 4 + 1                    # Wan wants 4n+1 frames
    except (TypeError, ValueError):
        return LENGTHS[_cfg()["video_length"]]


def request_stop() -> bool:
    if not _job:
        return False
    _stop_requested.set()
    return True


def _free_gpu() -> None:
    """The image engine and (with image_free_gpu on) the chat model let go of the GPU before a clip."""
    image_gen.stop()
    if image_gen._cfg().get("image_free_gpu", True):
        import llama_local
        llama_local.unload_all()


def generate(prompt: str, size=None, length=None, seed: int | None = None, negative: str = "",
             ref_image: str | None = None, ref_id: str | None = None, should_cancel=None, wait_while=None,
             key: str = "turbo", best: bool | None = None) -> dict:
    """Make one clip. ref_image: a picture's path to start from (image -> video). best: the full VAE instead of
    the fast decoder (None: the setting). Returns the clip's metadata with "url". Raises VideoError with a reason
    the user can act on."""
    global _proc, _job, _last_error, _last_code
    prompt = str(prompt or "").strip()
    if not prompt:
        raise VideoError("Say what the video should show.", "empty")
    refused = image_gen._check(prompt)
    if refused:
        _last_error, _last_code = refused
        raise VideoError(*refused)
    exe = cli_exe()
    if not exe:
        raise VideoError("The engine (stable-diffusion.cpp) isn't installed - see Settings > Images.", "not_installed")
    if not installed(key):
        raise VideoError(f"{MODELS[key]['name']} isn't downloaded to {_cfg()['video_root']}\\models.", "not_installed")
    if ref_image and not os.path.exists(ref_image):
        raise VideoError("The reference picture is gone.", "missing")
    m, files = MODELS[key], model_files(key)
    best = _cfg()["video_best_quality"] if best is None else bool(best)
    fast_decode = not best and "tae" in files and os.path.exists(files["tae"])
    w, h = parse_size(size)
    frames = parse_frames(length)
    seed = int(seed) if str(seed or "").lstrip("-").isdigit() else secrets.randbelow(2**31)
    stop = lambda: _stop_requested.is_set() or bool(should_cancel and should_cancel())   # noqa: E731
    if not _lock.acquire(blocking=False):
        raise VideoError("A clip is already being made - wait for it or stop it.", "busy")
    _stop_requested.clear()
    vid = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    os.makedirs(VIDEOS_DIR, exist_ok=True)
    out = os.path.join(VIDEOS_DIR, vid + ".webm")
    log_path = os.path.join(VIDEOS_DIR, "engine.log")
    t0 = time.time()
    _job = {"id": vid, "model": m["name"], "stage": "starting", "text": "Starting", "step": 0, "steps": m["steps"],
            "width": w, "height": h, "frames": frames, "started": t0, "progress": 0.0, "ref": bool(ref_image)}
    try:
        while wait_while and wait_while():
            if stop():
                raise VideoError("Stopped before it started.", "stopped")
            _job.update(stage="waiting", text="Waiting for the GPU - the chat model is busy")
            time.sleep(1)
        _job.update(stage="freeing", text="Making room on the GPU")
        _free_gpu()
        run_prompt = prompt
        if m.get("prefix") and not prompt.lower().startswith(m["prefix"].strip(" ,").lower()):
            run_prompt = m["prefix"] + run_prompt
        if m.get("lora"):
            run_prompt += f"<lora:{m['lora'][0]}:{m['lora'][1]:g}>"
        args = [exe, "-M", "vid_gen", "--diffusion-model", files["diffusion"], "--t5xxl", files["t5xxl"],
                "--vae", files["vae"], "-p", run_prompt, "-n", negative or "", "--cfg-scale", f"{m['cfg']:g}",
                "--steps", str(m["steps"]), "--sampling-method", m["sampler"], "-W", str(w), "-H", str(h),
                "--video-frames", str(frames), "--fps", str(m["fps"]), "-s", str(seed),
                # auto-fit keeps what fits on the GPU and leaves 1.5 GB for the work itself; never --offload-to-cpu
                # (see the measurements above). The full VAE tiles itself when it runs out of memory.
                "--diffusion-fa", "--max-vram", "-1.5", "--mmap", "-o", out]
        if m.get("lora"):
            args += ["--lora-model-dir", os.path.dirname(_lora_path(key))]
        if fast_decode:
            args += ["--tae", files["tae"]]
        if ref_image:
            args += ["-i", ref_image]
        with open(log_path, "wb") as log:
            _proc = subprocess.Popen(args, cwd=os.path.dirname(exe), stdout=log, stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            _job.update(stage="loading", text=f"Loading {m['name']}")
            started = time.time()
            while _proc.poll() is None:
                if stop():
                    _proc.kill()
                    _proc.wait(10)
                    raise VideoError("Stopped. Nothing was saved.", "stopped")
                time.sleep(0.7)
                p = parse_progress(_read(log_path))
                spent = time.time() - started
                left = estimate_left(p, w, h, frames, m["steps"], fast_decode, spent)
                if p["stage"] == "decoding":
                    text = (f"Turning it into frames, part {p['tile']} of {p['tiles']}" if p["tiles"]
                            else "Turning it into frames")
                    if not fast_decode and p["retries"] >= 3:
                        text += " - the GPU is full, so it works in small pieces (slower)"
                else:
                    text = {"loading": f"Loading {m['name']}", "encoding": "Reading the prompt",
                            "drawing": f"Drawing, step {p['step']} of {p['steps'] or m['steps']}",
                            "saving": "Saving the video"}[p["stage"]]
                # The bar is time spent over time spent + time left, so it keeps moving while an estimate grows.
                _job.update(stage=p["stage"], text=text, step=p["step"], steps=p["steps"] or m["steps"],
                            tile=p["tile"], tiles=p["tiles"], retries=p["retries"],
                            progress=round(min(0.99, spent / (spent + left)), 3), left=round(left))
        if _proc.returncode != 0 or not os.path.exists(out) or os.path.getsize(out) < 1000:
            tail = _read(log_path)[-600:]
            if re.search(r"out of memory|cudaMalloc failed|failed to allocate|workspace capacity", tail, re.I):
                raise VideoError("It ran out of GPU memory - try a smaller size or a shorter clip, or close a game "
                                 "or video.", "oom")
            raise VideoError(f"The engine stopped without a video (see {log_path}).", "crashed")
        seconds = round(time.time() - t0, 1)
        _typical[key] = seconds * 49 / frames
        learn_rates(_read(log_path), w, h, frames, m["steps"], fast_decode)
        meta = {"id": vid, "prompt": prompt, "negative": negative or "", "model": m["name"], "key": key, "seed": seed,
                "width": w, "height": h, "frames": frames, "fps": m["fps"], "duration": round(frames / m["fps"], 2),
                "seconds": seconds, "ref_id": ref_id, "best": not fast_decode, "fav": False, "created": time.time()}
        with open(os.path.join(VIDEOS_DIR, vid + ".json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        _last_error = _last_code = ""
        return {**meta, "url": f"/api/videos/{vid}.webm"}
    except VideoError as e:
        _last_error, _last_code = str(e), e.code
        if os.path.exists(out):
            try:
                os.remove(out)                     # our own unfinished file, never a user file
            except OSError:
                pass
        raise
    finally:
        _proc = None
        _job = {}
        _lock.release()


def _read(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read(4_000_000).decode("utf-8", errors="replace")
    except OSError:
        return ""


def status() -> dict:
    return {"root": _cfg()["video_root"], "engine": bool(cli_exe()), "running": bool(_job),
            "job": {k: v for k, v in _job.items()}, "last_error": _last_error, "last_code": _last_code,
            "count": count(),
            "models": [{"key": k, "name": m["name"], "license": m["license"], "about": m["about"], "installed": installed(k),
                        "seconds": round(_typical.get(k, 0))} for k, m in MODELS.items()],
            "sizes": {k: f"{w}x{h}" for k, (w, h) in SIZES.items()}, "lengths": LENGTHS, "estimates": estimates()}


# ---- the gallery ------------------------------------------------------------------------------------------
def _trash_dir() -> str:
    return os.path.join(VIDEOS_DIR, "trash")


def count() -> int:
    try:
        return sum(1 for n in os.listdir(VIDEOS_DIR) if n.endswith(".webm"))
    except OSError:
        return 0


def _read_meta(folder: str, vid: str) -> dict | None:
    try:
        with open(os.path.join(folder, vid + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def find(vid: str) -> tuple[dict, str] | None:
    if not _ID.fullmatch(str(vid or "")):
        return None
    for folder in (VIDEOS_DIR, _trash_dir()):
        meta = _read_meta(folder, vid)
        if meta and os.path.exists(os.path.join(folder, vid + ".webm")):
            return meta, folder
    return None


def recent(limit: int = 120, q: str = "", fav: bool = False, trashed: bool = False) -> list:
    folder = _trash_dir() if trashed else VIDEOS_DIR
    words = [w for w in str(q or "").lower().split() if w]
    try:
        names = sorted((n for n in os.listdir(folder) if n.endswith(".json")), reverse=True)
    except OSError:
        return []
    out = []
    for n in names:
        meta = _read_meta(folder, n[:-5])
        if not meta or "id" not in meta or (fav and not meta.get("fav")):
            continue
        if words and not all(w in str(meta.get("prompt", "")).lower() for w in words):
            continue
        out.append({**meta, "url": f"/api/videos/{'trash/' if trashed else ''}{meta['id']}.webm", "trashed": trashed,
                    "ref_url": _ref_url(meta.get("ref_id"))})
        if len(out) >= limit:
            break
    return out


def _ref_url(iid: str | None) -> str | None:
    """The picture a clip started from - in Images or its trash; None once it was deleted for good."""
    got = image_gen.find(iid) if iid else None
    if not got:
        return None
    return f"/api/images/{'trash/' if got[1] != image_gen.IMAGES_DIR else ''}{iid}.png"


def set_favorite(vid: str, on: bool) -> dict:
    got = find(vid)
    if not got:
        raise VideoError("No such video.", "missing")
    meta, folder = got
    meta["fav"] = bool(on)
    with open(os.path.join(folder, vid + ".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    return meta


def _move_pair(vid: str, src: str, dst: str) -> None:
    os.makedirs(dst, exist_ok=True)
    for ext in (".webm", ".json"):
        os.replace(os.path.join(src, vid + ext), os.path.join(dst, vid + ext))


def trash(vid: str) -> None:
    got = find(vid)
    if not got or got[1] != VIDEOS_DIR:
        raise VideoError("No such video.", "missing")
    _move_pair(vid, VIDEOS_DIR, _trash_dir())


def restore(vid: str) -> None:
    got = find(vid)
    if not got or got[1] != _trash_dir():
        raise VideoError("That video isn't in the trash.", "missing")
    _move_pair(vid, _trash_dir(), VIDEOS_DIR)


def purge(vid: str) -> int:
    """Delete one clip for good - only from the trash, on the user's click. Returns the bytes freed."""
    got = find(vid)
    if not got or got[1] != _trash_dir():
        raise VideoError("Only a video in the trash can be deleted for good.", "missing")
    freed = 0
    for ext in (".webm", ".json"):
        path = os.path.join(_trash_dir(), vid + ext)
        try:
            freed += os.path.getsize(path)
            os.remove(path)
        except FileNotFoundError:
            pass
    return freed


def empty_trash() -> dict:
    count_, freed = 0, 0
    try:
        names = [n[:-5] for n in os.listdir(_trash_dir()) if n.endswith(".json")]
    except OSError:
        names = []
    for vid in names:
        if _ID.fullmatch(vid):
            try:
                freed += purge(vid)
                count_ += 1
            except (VideoError, OSError):
                pass
    return {"count": count_, "freed": freed}


def disk_free() -> int | None:
    try:
        return shutil.disk_usage(VIDEOS_DIR if os.path.isdir(VIDEOS_DIR) else ROOT).free
    except OSError:
        return None
