"""
video_audio.py
==============
"Add sound" for a clip in Videos: sound effects and ambience that match what is on screen, an optional spoken
line, and optional music from Music - mixed (effects and music duck under the voice) and put into a new copy of the
clip. The silent original stays.

  effects   MMAudio large_44k_v2 (hkchengrex/MMAudio; code MIT, weights CC-BY-NC 4.0 - fine for personal use, not
            for commercial use). It watches the clip and makes sound for exactly its length.
  voice     Chatterbox (Resemble AI, MIT; inaudible Perth watermark): English, or 22 more languages (Hebrew,
            Japanese, Russian ...) with its multilingual model; optional voice to imitate.
  mixing    numpy + PyAV in workers/av_worker.py (no ffmpeg install).

Each runs as its own process in its own environment under G:\\ClankerLocalCpp\\Audio (mm-venv, tts-venv) - their
PyTorch versions differ, and none of it loads into the app. One job at a time; the GPU is cleared first (image
engine, music engine, chat model).
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

import image_gen
import video_gen

ROOT = os.path.dirname(os.path.abspath(__file__))
AUDIO_ROOT = paths.feature_root("Audio")
WORKERS = os.path.join(ROOT, "workers")
LANGUAGES = ("en", "he", "ja", "ru", "es", "fr", "de", "ko", "zh", "it", "pt", "ar")

_lock = threading.Lock()
_job: dict = {}
_proc: subprocess.Popen | None = None
_stop = threading.Event()
_last_error = ""
STAGE_TEXT = {"voice:loading": "Loading the voice model", "voice:speaking": "Speaking the line",
              "loading": "Loading the sound model", "watching": "Watching the clip", "sound": "Making the sound",
              "mixing": "Mixing", "saving": "Saving the clip with sound"}


class SoundError(Exception):
    def __init__(self, message: str, code: str = "failed"):
        super().__init__(message)
        self.code = code


def _py(env: str) -> str:
    return os.path.join(AUDIO_ROOT, env, "Scripts", "python.exe")


def installed() -> dict:
    return {"effects": os.path.exists(_py("mm-venv")) and os.path.isdir(os.path.join(AUDIO_ROOT, "MMAudio")),
            "voice": os.path.exists(_py("tts-venv"))}


def _free_gpu() -> None:
    image_gen.stop()
    try:
        import music_gen
        music_gen.stop()
    except Exception:
        pass
    if image_gen._cfg().get("image_free_gpu", True):
        import llama_local
        llama_local.unload_all()


def _run(args: list, cwd: str, prefix: str, log_path: str) -> str:
    """Run one worker, following its STAGE lines. Returns its DONE value."""
    global _proc
    env = {**os.environ, "HF_HOME": os.path.join(AUDIO_ROOT, "hf"), "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    with open(log_path, "ab") as log:
        _proc = subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=log,
                                 creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        done = None
        for raw in iter(_proc.stdout.readline, b""):
            line = raw.decode("utf-8", "replace").strip()
            log.write(raw)
            if _stop.is_set():
                _proc.kill()
                raise SoundError("Stopped. Nothing was saved.", "stopped")
            if line.startswith("STAGE "):
                key = prefix + line[6:]
                _job.update(stage=key, text=STAGE_TEXT.get(key, line[6:]))
            elif line.startswith("DONE "):
                done = line[5:]
        _proc.wait()
    if _proc.returncode != 0 or done is None:
        raise SoundError(f"The {'voice' if prefix else 'sound'} step failed (see {log_path}).", "crashed")
    return done


def add_sound(vid: str, prompt: str = "", dialogue: str = "", language: str = "en", voice_start: float = 0.3,
              emotion: float = 0.5, music_id: str | None = None, voice_file: str | None = None, seed: int | None = None,
              wait_while=None) -> dict:
    """Make a copy of clip `vid` with sound. Returns the new clip's metadata (with "url")."""
    global _job, _last_error
    got = video_gen.find(vid)
    if not got or got[1] != video_gen.VIDEOS_DIR:
        raise SoundError("That video isn't in Videos.", "missing")
    meta, folder = got
    for text in (prompt, dialogue):
        refused = image_gen._check(text or "")
        if refused:
            raise SoundError(*refused)
    have = installed()
    if not have["effects"]:
        raise SoundError(f"The sound model (MMAudio) isn't installed in {AUDIO_ROOT}.", "not_installed")
    if dialogue.strip() and not have["voice"]:
        raise SoundError(f"The voice model (Chatterbox) isn't installed in {AUDIO_ROOT}.", "not_installed")
    music_path = None
    if music_id:
        import music_gen
        m = music_gen.find(music_id)
        if not m:
            raise SoundError("That song is no longer in Music.", "missing")
        music_path = os.path.join(m[1], music_id + ".mp3")
    if not _lock.acquire(blocking=False):
        raise SoundError("Sound is already being added to a clip - wait for it or stop it.", "busy")
    _stop.clear()
    t0 = time.time()
    _job = {"id": vid, "stage": "starting", "text": "Starting", "started": t0, "voice": bool(dialogue.strip())}
    new_id = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    work = os.path.join(video_gen.VIDEOS_DIR, "work")
    os.makedirs(work, exist_ok=True)
    log_path = os.path.join(work, "sound.log")
    out = os.path.join(video_gen.VIDEOS_DIR, new_id + ".webm")
    voice_wav = os.path.join(work, new_id + "-voice.wav")
    try:
        while wait_while and wait_while():
            if _stop.is_set():
                raise SoundError("Stopped before it started.", "stopped")
            _job.update(text="Waiting for the GPU - the chat model is busy")
            time.sleep(1)
        _job.update(text="Making room on the GPU")
        _free_gpu()
        if dialogue.strip():
            tts_job = os.path.join(work, new_id + "-tts.json")
            with open(tts_job, "w", encoding="utf-8") as f:
                json.dump({"text": dialogue.strip()[:600], "out": voice_wav,
                           "language": language if language in LANGUAGES else "en", "emotion": emotion,
                           "voice": voice_file}, f)
            _run([_py("tts-venv"), os.path.join(WORKERS, "tts_worker.py"), tts_job], WORKERS, "voice:", log_path)
        av_job = os.path.join(work, new_id + "-av.json")
        with open(av_job, "w", encoding="utf-8") as f:
            json.dump({"video": os.path.join(folder, vid + ".webm"), "out": out,
                       "prompt": prompt.strip() or meta.get("prompt", ""), "seed": seed if seed is not None else 42,
                       "voice": voice_wav if dialogue.strip() else None, "voice_start": max(0.0, float(voice_start or 0)),
                       "music": music_path, "steps": 25, "variant": "large_44k_v2"}, f)
        _run([_py("mm-venv"), os.path.join(WORKERS, "av_worker.py"), av_job], os.path.join(AUDIO_ROOT, "MMAudio"), "",
             log_path)
        if not os.path.exists(out) or os.path.getsize(out) < 1000:
            raise SoundError("The sound step finished without a video.", "failed")
        new = {**{k: v for k, v in meta.items() if k not in ("id", "fav", "created")}, "id": new_id, "fav": False,
               "created": time.time(), "sound": {"prompt": prompt.strip(), "dialogue": dialogue.strip(),
                                                 "language": language, "music": music_id,
                                                 "seconds": round(time.time() - t0, 1)}, "from": vid}
        with open(os.path.join(video_gen.VIDEOS_DIR, new_id + ".json"), "w", encoding="utf-8") as f:
            json.dump(new, f, ensure_ascii=False, indent=1)
        _last_error = ""
        return {**new, "url": f"/api/videos/{new_id}.webm"}
    except SoundError as e:
        _last_error = str(e)
        if os.path.exists(out):
            try:
                os.remove(out)                      # our own unfinished file
            except OSError:
                pass
        raise
    finally:
        for p in (voice_wav,):
            try:
                os.remove(p)
            except OSError:
                pass
        _job = {}
        _lock.release()


def request_stop() -> bool:
    if not _job:
        return False
    _stop.set()
    if _proc is not None and _proc.poll() is None:
        _proc.kill()
    return True


def status() -> dict:
    return {"installed": installed(), "job": dict(_job), "last_error": _last_error}
