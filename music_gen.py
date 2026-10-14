"""
music_gen.py
============
Local music: ACE-Step 1.5 (MIT) - songs with or without lyrics, instrumentals, 10 s to 10 min - from its own API
server (acestep-api), run on this PC. Nothing is sent anywhere.

Install (once, by hand or by Claude): the ACE-Step-1.5 source in settings music_root (default
G:\\ClankerLocalCpp\\Music\\ACE-Step-1.5) with its uv environment (.venv). Its models (the 2B turbo DiT and the 0.6B
"5Hz" language model that plans the song - the 8 GB tier of its GPU guide) download into that folder on first use.

The server starts with the first song and stops after music_keep_minutes idle (it holds a few GB of VRAM). Before
it starts, the image engine and the chat model let go of the GPU, as for images and videos.

Made songs (FLAC - MP3 export needs ffmpeg, which isn't installed): data/music/<id>.flac with <id>.json (prompt, lyrics, duration, bpm, key, seconds). Trash:
data/music/trash/; deleted for good only from the trash, on the user's click.
"""
from __future__ import annotations

import paths
import json
import os
import re
import secrets
import subprocess
import threading
import time

import httpx

import image_gen

ROOT = os.path.dirname(os.path.abspath(__file__))
MUSIC_DIR = os.path.join(ROOT, "data", "music")
DEFAULT_ROOT = os.path.join(paths.feature_root("Music"), "ACE-Step-1.5")
PORT = int(os.environ.get("ACESTEP_PORT_FOR_APP", "8011"))
BASE = f"http://127.0.0.1:{PORT}"
LM_MODEL = "acestep-5Hz-lm-0.6B"
DEFAULT_SETTINGS = {"music_root": DEFAULT_ROOT, "music_keep_minutes": 5}
LANGUAGES = ("en", "ja", "zh", "ko", "es", "fr", "de", "ru", "he")

_PATH_RE = re.compile(r"[A-Za-z]:\\[^<>\"|?*]{0,200}")
_ID = re.compile(r"\d{8}-\d{6}-[0-9a-f]{6}")
_lock = threading.Lock()
_proc: subprocess.Popen | None = None
_job: dict = {}
_last_used = 0.0
_last_error = ""
_stop_requested = threading.Event()
_idle_thread: threading.Thread | None = None
_typical = 60.0                       # seconds for a song, replaced by each real one
_settings = lambda: {}                # noqa: E731


class MusicError(Exception):
    """code: empty, filter, not_installed, crashed, timeout, failed, stopped, busy, missing."""

    def __init__(self, message: str, code: str = "failed"):
        super().__init__(message)
        self.code = code


def configure(settings_fn) -> None:
    global _settings
    _settings = settings_fn


def clean_settings(s: dict) -> dict:
    root = str(s.get("music_root") or DEFAULT_ROOT).strip()
    try:
        keep = max(0, min(120, int(s.get("music_keep_minutes", 5))))
    except (TypeError, ValueError):
        keep = 5
    return {"music_root": root if _PATH_RE.fullmatch(root) else DEFAULT_ROOT, "music_keep_minutes": keep}


def _cfg() -> dict:
    return clean_settings(_settings() or {})


def _python() -> str:
    return os.path.join(_cfg()["music_root"], ".venv", "Scripts", "python.exe")


def installed() -> bool:
    return os.path.exists(_python()) and os.path.isdir(os.path.join(_cfg()["music_root"], "acestep"))


def _alive() -> bool:
    return _proc is not None and _proc.poll() is None


def _healthy() -> bool:
    try:
        return httpx.get(f"{BASE}/health", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


def stop() -> None:
    """Stop the music server (frees its VRAM). It starts again with the next song."""
    global _proc
    if _proc is not None:
        try:
            _proc.terminate()
            _proc.wait(timeout=15)
        except Exception:
            try:
                _proc.kill()
            except Exception:
                pass
    _proc = None


def _start(progress) -> None:
    global _proc
    if _alive() and _healthy():
        return
    stop()
    root = _cfg()["music_root"]
    env = {**os.environ, "ACESTEP_API_HOST": "127.0.0.1", "ACESTEP_API_PORT": str(PORT),
           "ACESTEP_INIT_LLM": "true", "ACESTEP_LM_MODEL_PATH": LM_MODEL, "ACESTEP_LM_BACKEND": "pt",
           "ACESTEP_DOWNLOAD_SOURCE": "huggingface", "CHECK_UPDATE": "false", "PYTHONIOENCODING": "utf-8"}
    os.makedirs(MUSIC_DIR, exist_ok=True)
    log = open(os.path.join(MUSIC_DIR, "server.log"), "ab")
    _proc = subprocess.Popen([_python(), "-m", "acestep.api_server"], cwd=root, env=env, stdout=log,
                             stderr=subprocess.STDOUT, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    deadline = time.time() + 1800          # the first start downloads its models (several GB)
    while time.time() < deadline:
        if _stop_requested.is_set():
            stop()
            raise MusicError("Stopped.", "stopped")
        if not _alive():
            raise MusicError(f"The music engine stopped while starting (see {MUSIC_DIR}\\server.log).", "crashed")
        if _healthy():
            return
        progress("Starting the music engine (the first time it downloads its models)")
        time.sleep(2)
    stop()
    raise MusicError("The music engine didn't start in 30 minutes.", "timeout")


def _idle_watch() -> None:
    while True:
        time.sleep(30)
        keep = _cfg()["music_keep_minutes"] * 60
        if _alive() and not _job and time.time() - _last_used > keep:
            with _lock:
                if not _job:
                    stop()


def request_stop() -> bool:
    if not _job:
        return False
    _stop_requested.set()
    return True


def _free_gpu() -> None:
    image_gen.stop()
    if image_gen._cfg().get("image_free_gpu", True):
        import llama_local
        llama_local.unload_all()


def generate(prompt: str, lyrics: str = "", duration: float = 60, instrumental: bool = False, seed: int | None = None,
             language: str = "en", bpm: int | None = None, wait_while=None) -> dict:
    """Make one song. Returns its metadata with "url". Raises MusicError."""
    global _job, _last_used, _last_error, _typical, _idle_thread
    prompt, lyrics = str(prompt or "").strip(), str(lyrics or "").strip()
    if not prompt:
        raise MusicError("Describe the music - genre, mood, instruments.", "empty")
    refused = image_gen._check(f"{prompt}\n{lyrics}")
    if refused:
        raise MusicError(*refused)
    if not installed():
        raise MusicError(f"ACE-Step isn't installed in {_cfg()['music_root']}.", "not_installed")
    if not _lock.acquire(blocking=False):
        raise MusicError("A song is already being made - wait for it or stop it.", "busy")
    _stop_requested.clear()
    duration = max(10.0, min(600.0, float(duration or 60)))
    t0 = time.time()
    _job = {"stage": "starting", "text": "Starting", "progress": 0.0, "duration": duration, "started": t0}

    def progress(text, frac=None):
        _job.update(text=text, **({"progress": frac} if frac is not None else {}))
    try:
        while wait_while and wait_while():
            if _stop_requested.is_set():
                raise MusicError("Stopped before it started.", "stopped")
            progress("Waiting for the GPU - the chat model is busy")
            time.sleep(1)
        progress("Making room on the GPU")
        _free_gpu()
        _start(progress)
        if _idle_thread is None or not _idle_thread.is_alive():
            _idle_thread = threading.Thread(target=_idle_watch, name="music-idle", daemon=True)
            _idle_thread.start()
        body = {"prompt": prompt, "lyrics": "[Instrumental]" if instrumental or not lyrics else lyrics,
                "thinking": True, "vocal_language": language if language in LANGUAGES else "en",
                "audio_format": "flac", "audio_duration": duration, "batch_size": 1, "inference_steps": 8,
                "lm_backend": "pt", "lm_model_path": LM_MODEL,
                "use_random_seed": seed is None, "seed": int(seed) if seed is not None else -1}
        if bpm:
            body["bpm"] = max(30, min(300, int(bpm)))
        r = httpx.post(f"{BASE}/release_task", json=body, timeout=60).json()
        task = (r.get("data") or {}).get("task_id")
        if not task:
            raise MusicError(f"The music engine refused the request: {r.get('error') or r}", "failed")
        _job.update(stage="making")
        started = time.time()
        while True:
            if _stop_requested.is_set():
                stop()                                   # it can't cancel a running task; closing it does
                raise MusicError("Stopped. Nothing was saved.", "stopped")
            time.sleep(1.5)
            spent = time.time() - started
            est = _typical * max(0.5, duration / 60)
            progress(f"Making the song, about {max(1, round(est - spent))} s left" if spent < est
                     else "Making the song, almost done", round(min(0.97, spent / (spent + max(5, est - spent))), 3))
            q = httpx.post(f"{BASE}/query_result", json={"task_id_list": [task]}, timeout=30).json()
            item = ((q.get("data") or [{}])[0]) or {}
            if item.get("status") == 2:
                raise MusicError(f"The music engine failed: {item.get('result') or 'no reason given'}", "failed")
            if item.get("status") == 1:
                results = json.loads(item.get("result") or "[]")
                break
        if not results or not results[0].get("file"):
            raise MusicError("The music engine finished without a song.", "failed")
        res = results[0]
        audio = httpx.get(BASE + res["file"], timeout=120)
        audio.raise_for_status()
        mid = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
        os.makedirs(MUSIC_DIR, exist_ok=True)
        with open(os.path.join(MUSIC_DIR, mid + ".flac"), "wb") as f:
            f.write(audio.content)
        metas = res.get("metas") or {}
        seconds = round(time.time() - t0, 1)
        _typical = (time.time() - started) * 60 / duration
        meta = {"id": mid, "prompt": prompt, "lyrics": lyrics if not instrumental else "", "instrumental": instrumental
                or not lyrics, "language": body["vocal_language"], "duration": metas.get("duration") or duration,
                "bpm": metas.get("bpm"), "key": metas.get("keyscale"), "seed": res.get("seed_value"),
                "model": f"ACE-Step 1.5 ({res.get('dit_model') or 'turbo'})", "seconds": seconds, "fav": False,
                "created": time.time()}
        with open(os.path.join(MUSIC_DIR, mid + ".json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=1)
        _last_error = ""
        return {**meta, "url": f"/api/music/{mid}.flac"}
    except MusicError as e:
        _last_error = str(e)
        raise
    except (httpx.HTTPError, ValueError) as e:
        _last_error = f"The music engine didn't answer: {e}"
        raise MusicError(_last_error, "failed")
    finally:
        _last_used = time.time()
        _job = {}
        _lock.release()


def status() -> dict:
    return {"installed": installed(), "root": _cfg()["music_root"], "running": _alive(), "job": dict(_job),
            "last_error": _last_error, "count": count(), "estimate_per_minute": round(_typical)}


# ---- the gallery (the same shape as Images and Videos) ------------------------------------------------------
def _trash_dir() -> str:
    return os.path.join(MUSIC_DIR, "trash")


def count() -> int:
    try:
        return sum(1 for n in os.listdir(MUSIC_DIR) if n.endswith(".flac"))
    except OSError:
        return 0


def _read_meta(folder: str, mid: str) -> dict | None:
    try:
        with open(os.path.join(folder, mid + ".json"), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def find(mid: str) -> tuple[dict, str] | None:
    if not _ID.fullmatch(str(mid or "")):
        return None
    for folder in (MUSIC_DIR, _trash_dir()):
        meta = _read_meta(folder, mid)
        if meta and os.path.exists(os.path.join(folder, mid + ".flac")):
            return meta, folder
    return None


def recent(limit: int = 200, q: str = "", fav: bool = False, trashed: bool = False) -> list:
    folder = _trash_dir() if trashed else MUSIC_DIR
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
        hay = f"{meta.get('prompt', '')} {meta.get('lyrics', '')}".lower()
        if words and not all(w in hay for w in words):
            continue
        out.append({**meta, "url": f"/api/music/{'trash/' if trashed else ''}{meta['id']}.flac", "trashed": trashed})
        if len(out) >= limit:
            break
    return out


def set_favorite(mid: str, on: bool) -> dict:
    got = find(mid)
    if not got:
        raise MusicError("No such song.", "missing")
    meta, folder = got
    meta["fav"] = bool(on)
    with open(os.path.join(folder, mid + ".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    return meta


def _move_pair(mid: str, src: str, dst: str) -> None:
    os.makedirs(dst, exist_ok=True)
    for ext in (".flac", ".json"):
        os.replace(os.path.join(src, mid + ext), os.path.join(dst, mid + ext))


def trash(mid: str) -> None:
    got = find(mid)
    if not got or got[1] != MUSIC_DIR:
        raise MusicError("No such song.", "missing")
    _move_pair(mid, MUSIC_DIR, _trash_dir())


def restore(mid: str) -> None:
    got = find(mid)
    if not got or got[1] != _trash_dir():
        raise MusicError("That song isn't in the trash.", "missing")
    _move_pair(mid, _trash_dir(), MUSIC_DIR)


def purge(mid: str) -> int:
    """Delete one song for good - only from the trash, on the user's click."""
    got = find(mid)
    if not got or got[1] != _trash_dir():
        raise MusicError("Only a song in the trash can be deleted for good.", "missing")
    freed = 0
    for ext in (".flac", ".json"):
        path = os.path.join(_trash_dir(), mid + ext)
        try:
            freed += os.path.getsize(path)
            os.remove(path)
        except FileNotFoundError:
            pass
    return freed


def empty_trash() -> dict:
    n = freed = 0
    try:
        names = [x[:-5] for x in os.listdir(_trash_dir()) if x.endswith(".json")]
    except OSError:
        names = []
    for mid in names:
        if _ID.fullmatch(mid):
            try:
                freed += purge(mid)
                n += 1
            except (MusicError, OSError):
                pass
    return {"count": n, "freed": freed}
