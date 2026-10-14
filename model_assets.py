"""
model_assets.py
===============
Files a local model needs beside its weights, fetched when you ask (D50). Today: the vision projector (an
"mmproj" file) that lets a model see pictures.

A known model (KNOWN: matched by its weights' file name) gets its own projector - the publisher's file, with its
size and SHA-256. It downloads through the network layer (the route and kill switch in Settings > Network) into
the model's own folder as a .part file, is checked against both, and only then gets its "mmproj =" line in
models.ini - added beside the model's own "model =" line, the rest of the file untouched (a backup of the file as
it was is kept). A missing projector would stop the model loading, so the line never comes first.

llama-server reads models.ini when it starts: the model sees pictures after the next start of the app.
"""
from __future__ import annotations

import hashlib
import ntpath
import os
import threading
import time

import httpx

import llama_local
import netlayer

# The model's weights file name (lower case) starts with the key.
KNOWN = {
    "ornith-1.5-9b": {
        "file": "mmproj-Ornith-1.5-9B-f16.gguf",
        "url": "https://huggingface.co/bartowski/Ornith-1.5-9B-GGUF/resolve/main/mmproj-Ornith-1.5-9B-f16.gguf",
        "size": 918165728,
        "sha256": "946850fd82bd43b856d878267bc9db809f0aaa37c81bf11918ecad1de464625e",
        "about": "Ornith 1.5 9B's vision projector (F16 - bartowski's conversion of ornith-ai/Ornith-1.5-9B)",
    },
}

_lock = threading.Lock()
_state: dict = {"state": "idle", "model": "", "done": 0, "total": 0, "error": ""}


def _p(path: str):
    """Path functions for this path: models.ini holds Windows paths (and tests, POSIX ones)."""
    return ntpath if "\\" in str(path or "") else os.path


def known_for(model_path: str) -> dict | None:
    name = _p(model_path).basename(str(model_path or "")).lower()
    return next((dict(v, key=k) for k, v in KNOWN.items() if name.startswith(k)), None)


def offers() -> list:
    """The models in models.ini that could see pictures with a projector they don't have yet."""
    out = []
    try:
        configs = llama_local.list_model_configs()
    except Exception:
        return out
    for c in configs:
        k = known_for(c.get("model_path"))
        if not k or c.get("mmproj_path"):
            continue
        pm = _p(c["model_path"])
        dest = pm.join(pm.dirname(c["model_path"]), k["file"])
        out.append({"model": c["name"], "key": k["key"], "file": k["file"], "mb": round(k["size"] / 1e6), "dest": dest, "about": k["about"],
                    "on_disk": os.path.exists(dest) and os.path.getsize(dest) == k["size"]})
    return out


def status() -> dict:
    with _lock:
        return dict(_state)


def start(model: str) -> dict:
    """Download the projector for `model` in the background; status() follows it."""
    offer = next((o for o in offers() if o["model"] == model), None)
    if not offer:
        return {"ok": False, "message": f"No vision file is known for {model}, or it already has one."}
    with _lock:
        if _state["state"] == "running":
            return {"ok": False, "message": f"Already downloading the vision file for {_state['model']}."}
        k = KNOWN[offer["key"]]
        _state.update(state="running", model=model, done=0, total=k["size"], error="", started=time.time())
    threading.Thread(target=_run, args=(model, offer["dest"], k), name="model-asset", daemon=True).start()
    return {"ok": True, **status()}


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(model: str, dest: str, k: dict) -> None:
    part = dest + ".part"
    try:
        if not (os.path.exists(dest) and os.path.getsize(dest) == k["size"] and _sha256(dest) == k["sha256"]):
            h = hashlib.sha256()
            with netlayer.client("other", timeout=httpx.Timeout(60.0, connect=20.0), follow_redirects=True, private=True) as c:
                with c.stream("GET", k["url"]) as r:
                    if r.status_code >= 400:
                        raise RuntimeError(f"{k['url'].split('/')[2]} answered {r.status_code}")
                    with open(part, "wb") as f:
                        for chunk in r.iter_bytes(1 << 16):
                            f.write(chunk)
                            h.update(chunk)
                            with _lock:
                                _state["done"] += len(chunk)
            if os.path.getsize(part) != k["size"] or h.hexdigest() != k["sha256"]:
                raise RuntimeError("the download doesn't match the published file (size or SHA-256) - nothing was changed")
            os.replace(part, dest)
        add_mmproj_line(model, dest)
        with _lock:
            _state.update(state="done", done=k["size"])
    except Exception as e:                                  # network, disk, refused route: say it, change nothing
        with _lock:
            _state.update(state="failed", error=str(e)[:300])
    finally:
        if os.path.exists(part):
            try:
                os.remove(part)                              # our own unfinished download, never a user file
            except OSError:
                pass


def add_mmproj_line(model: str, path: str) -> None:
    """`mmproj = path` after the model's own `model =` line in models.ini; the rest of the file is kept as it is.
    The file as it was is saved beside it (models.ini.bak-<time>)."""
    ini = llama_local._ini_path()
    raw = open(ini, "rb").read().decode("utf-8")
    nl = "\r\n" if "\r\n" in raw else "\n"
    lines = raw.split(nl)
    start = next((i for i, l in enumerate(lines) if l.strip() == f"[{model}]"), None)
    if start is None:
        raise RuntimeError(f"{model} isn't in models.ini any more")
    end = next((i for i in range(start + 1, len(lines)) if lines[i].strip().startswith("[")), len(lines))
    body = lines[start + 1:end]
    if any(l.split("=")[0].strip() == "mmproj" for l in body if "=" in l):
        return
    at = next((start + 1 + i for i, l in enumerate(body) if l.split("=")[0].strip() == "model"), start)
    lines.insert(at + 1, f"mmproj = {path}")
    with open(ini + time.strftime(".bak-%Y%m%d-%H%M%S"), "wb") as f:
        f.write(raw.encode("utf-8"))
    with open(ini, "wb") as f:
        f.write(nl.join(lines).encode("utf-8"))
    llama_local._ini_cache.update(key=None, configs=None)
