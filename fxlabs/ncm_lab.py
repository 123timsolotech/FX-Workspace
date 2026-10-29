"""
ncm_lab.py
==========
Labs > Research models > NCM story writer (1.4.2, Beta). HazyResearch's Next-Concept Modeling (MIT) writes text
coarse to fine: a rough draft of the whole story first, then sharper versions, each level all at once instead of one
word at a time. This page shows each level as it lands, and compares the speed with your local chat model.

  Setup      on your click: the code (github.com/HazyResearch/ncm, a zip) and the TinyStories checkpoint
             (huggingface.co/hazyresearch/ncm-checkpoints, tinystories/ - about 578 MB) go to E:\\FXWorkspace\\ncm
             (G: is nearly full); the few Python packages the Labs engine lacks are added to it.
  Writing    labs_ncm_worker.py in the Labs Python, on the GPU (it waits while a Labs run trains).
  Honest     the TinyStories model is unconditional: it writes a story from scratch, and a "story start" can't steer
             it. Training your own NCM isn't possible - the repo publishes no training code.
"""
from __future__ import annotations

import io
import json
import os
import subprocess
import threading
import time
import zipfile

ROOT = os.environ.get("FX_NCM_DIR") or r"E:\FXWorkspace\ncm"
REPO_ZIP = "https://github.com/HazyResearch/ncm/archive/refs/heads/main.zip"
HF_TREE = "https://huggingface.co/api/models/hazyresearch/ncm-checkpoints/tree/main/tinystories"
HF_FILE = "https://huggingface.co/hazyresearch/ncm-checkpoints/resolve/main/tinystories/{name}"
NEEDS = ("omegaconf", "hydra-core", "regex", "transformers>=4.51")
WORKER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "workers", "labs_ncm_worker.py")
state = {"setup": None, "progress": "", "error": None, "writing": False, "levels": [], "result": None, "compare": None}
_lock = threading.Lock()


def repo_dir() -> str:
    return os.path.join(ROOT, "ncm-main")


def ckpt_dir() -> str:
    return os.path.join(ROOT, "artifacts", "tinystories")


def status() -> dict:
    import labs
    ready_code = os.path.isfile(os.path.join(repo_dir(), "models", "next_concept.py"))
    marker = os.path.join(ckpt_dir(), ".complete")
    return {"root": ROOT, "drive_ok": os.path.isdir(os.path.splitdrive(ROOT)[0] + "\\"), "code": ready_code,
            "checkpoint": os.path.isfile(marker), "labs": bool(labs.engine().get("installed")),
            "ready": ready_code and os.path.isfile(marker), **{k: state[k] for k in state}}


def _client():
    import netlayer
    return netlayer.client("models", timeout=60.0, follow_redirects=True)


def _download(url: str, dest: str, label: str) -> None:
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    part = dest + ".part"
    with _client() as c, c.stream("GET", url) as r:
        r.raise_for_status()
        total, done = int(r.headers.get("content-length") or 0), 0
        with open(part, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                done += len(chunk)
                state["progress"] = f"{label}: {done / 1e6:,.0f}" + (f" of {total / 1e6:,.0f} MB" if total else " MB")
    os.replace(part, dest)


def _setup():
    import labs
    try:
        if not labs.engine().get("installed"):
            raise RuntimeError("Set up the Labs engine first (Labs > Setup).")
        if not os.path.isfile(os.path.join(repo_dir(), "models", "next_concept.py")):
            state["progress"] = "The NCM code"
            with _client() as c:
                r = c.get(REPO_ZIP)
                r.raise_for_status()
            os.makedirs(ROOT, exist_ok=True)
            zipfile.ZipFile(io.BytesIO(r.content)).extractall(ROOT)
        marker = os.path.join(ckpt_dir(), ".complete")
        if not os.path.isfile(marker):
            state["progress"] = "Listing the checkpoint"
            with _client() as c:
                r = c.get(HF_TREE)
                r.raise_for_status()
                files = [f["path"].split("/", 1)[1] for f in r.json() if f.get("type") == "file"]
            for name in files:
                dest = os.path.join(ckpt_dir(), *name.split("/"))
                if not os.path.isfile(dest):
                    _download(HF_FILE.format(name=name), dest, name)
            open(marker, "w").close()
        state["progress"] = "Python packages for NCM"
        r = subprocess.run([labs.python(), "-m", "pip", "install", "--no-cache-dir", "-q", *NEEDS], capture_output=True,
                           text=True, env=labs._pip_env(), timeout=1800,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if r.returncode:
            raise RuntimeError("Installing the packages failed: " + (r.stderr or r.stdout).strip()[-300:])
        state.update(error=None, progress="Ready")
    except Exception as e:
        state.update(error=str(e)[:300], progress="")
    finally:
        state["setup"] = None


def setup() -> dict:
    with _lock:
        if state["setup"]:
            return {"ok": False, "message": "Already setting up."}
        state.update(setup="running", error=None, progress="Starting")
    threading.Thread(target=_setup, daemon=True, name="labs-ncm-setup").start()
    return {"ok": True, "message": f"Downloading about 600 MB to {ROOT} and adding NCM's packages to Labs."}


def write(seed: int | None = None) -> dict:
    """One story, level by level (on a thread; the page polls status())."""
    import labs
    if not status()["ready"]:
        return {"ok": False, "message": "Set it up first."}
    if labs.gpu_busy():
        return {"ok": False, "message": "A Labs run is training on the GPU - try when it has finished."}
    with _lock:
        if state["writing"]:
            return {"ok": False, "message": "Already writing."}
        state.update(writing=True, levels=[], result=None, error=None)
    seed = int(seed if seed is not None else time.time()) % 100000

    def work():
        try:
            labs._free_gpu()
            proc = subprocess.Popen([labs.python(), "-u", WORKER, repo_dir(), ckpt_dir(), str(seed)],
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                    errors="replace", env=labs._worker_env(),
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for line in proc.stdout:
                if not line.startswith("{"):
                    continue
                msg = json.loads(line)
                if "level" in msg:
                    state["levels"].append(msg)
                elif "loaded" in msg:
                    state["progress"] = f"Model loaded in {msg['loaded']} s"
                elif msg.get("ok") is False:
                    state["error"] = msg.get("message")
                elif msg.get("ok"):
                    state["result"] = {"seconds": msg["seconds"], "seed": seed,
                                       "words": len((state["levels"][-1]["text"] if state["levels"] else "").split())}
            proc.wait(timeout=600)
            if proc.returncode and not state["error"]:
                state["error"] = (proc.stderr.read() or "It stopped.").strip().splitlines()[-1][:300]
        except Exception as e:
            state["error"] = str(e)[:300]
        finally:
            state["writing"] = False
    threading.Thread(target=work, daemon=True, name="labs-ncm").start()
    return {"ok": True}


def compare_local() -> dict:
    """The same job for the local chat model - a short children's story - timed to the full text."""
    import core.state as shared
    import llama_local
    import netlayer
    model = shared.app_state.get("local_engaged") or shared.app_state.get("local_model")
    if not model:
        return {"ok": False, "message": "No local chat model is chosen."}
    t0 = time.time()
    with netlayer.local_client(timeout=600) as http:                     # llama-server on this PC only
        r = http.post(f"{llama_local.LLAMA_SERVER_BASE.rstrip('/')}/chat/completions",
                      json={"model": model, "max_tokens": 200, "temperature": 0.8, "stream": False,
                            "messages": [{"role": "user", "content": "Write a very short story for a three-year-old (about 100 words)."}]})
    r.raise_for_status()
    d = r.json()
    t = d.get("timings") or {}
    text = d["choices"][0]["message"].get("content") or ""
    state["compare"] = {"model": model, "seconds": round(time.time() - t0, 2), "words": len(text.split()),
                        "writing_tps": t.get("predicted_per_second"), "text": text[:1200]}
    return {"ok": True, **state["compare"]}
