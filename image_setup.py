"""
image_setup.py
==============
Downloads the local image engine and models (image_gen.py) and verifies each file's SHA-256 against the
checksum its source publishes (GitHub release asset digests, Hugging Face LFS hashes) - a file that doesn't
match is deleted and reported, never used. Resumes partial downloads. Run once:

    .venv\\Scripts\\python.exe image_setup.py [target folder]      (default local\\Images)

Sources and checksums were read from the publishers on 2026-10-01 (see FILES).
"""
from __future__ import annotations

import core.modpath  # noqa: F401  (the module folders on sys.path)
import paths
import hashlib
import json
import os
import shutil
import sys
import time
import zipfile

import httpx

HF = "https://huggingface.co/{repo}/resolve/main/{path}"
# (local path, url, size bytes (approx, for progress), sha256, source note)
FILES = [
    ("engine/sd-master-3f8527a-bin-win-cuda12-x64.zip",
     "https://github.com/leejet/stable-diffusion.cpp/releases/download/master-929-3f8527a/sd-master-3f8527a-bin-win-cuda12-x64.zip",
     322_300_000, "217d6dead9abd3f827fc338268555cc179234e7e6330ef21ecb1c985e19d2dc7", "leejet/stable-diffusion.cpp master-929 (MIT)"),
    ("models/z_image_turbo-Q6_K.gguf", HF.format(repo="leejet/Z-Image-Turbo-GGUF", path="z_image_turbo-Q6_K.gguf"),
     5_019_000_000, "319f627beac8059b7546f36a7b4d5097b7f4ee6a1fc37585d0f75ca1d12d01af", "Z-Image-Turbo (Apache-2.0)"),
    ("models/flux-2-klein-4b-Q8_0.gguf", HF.format(repo="leejet/FLUX.2-klein-4B-GGUF", path="flux-2-klein-4b-Q8_0.gguf"),
     4_101_000_000, "0bba6951258ec8f92d51114a8fa13e66828297bfff58a738f52729b3ef66fa28", "FLUX.2 klein 4B (Apache-2.0)"),
    ("models/Qwen3-4B-Q4_K_M.gguf", HF.format(repo="unsloth/Qwen3-4B-GGUF", path="Qwen3-4B-Q4_K_M.gguf"),
     2_382_000_000, "f6f851777709861056efcdad3af01da38b31223a3ba26e61a4f8bf3a2195813a", "Qwen3-4B text encoder (Apache-2.0)"),
    ("models/z_image_ae.safetensors", HF.format(repo="Comfy-Org/z_image_turbo", path="split_files/vae/ae.safetensors"),
     320_000_000, "afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38", "Z-Image VAE (Apache-2.0)"),
    ("models/flux2-vae.safetensors", HF.format(repo="Comfy-Org/flux2-klein", path="split_files/vae/flux2-vae.safetensors"),
     321_000_000, "868fe7b343cc8f3a19dbcfcafbc3d5f888802be3f89bd81b65b3621a066ce8f3", "FLUX.2 VAE (Apache-2.0)"),
    ("models/Krea-2-Turbo-Q4_K_M.gguf", HF.format(repo="realrebelai/KREA-2_GGUFs", path="TURBO/Krea-2-Turbo-Q4_K_M.gguf"),
     6_883_000_000, "273a98be1afe317bc7228403b6434647eaf866cebe6aff1980c401b950473807", "Krea 2 Turbo, community GGUF (Krea 2 Community License)"),
    ("models/Qwen3-VL-4B-Instruct-Q4_K_M.gguf", HF.format(repo="unsloth/Qwen3-VL-4B-Instruct-GGUF", path="Qwen3-VL-4B-Instruct-Q4_K_M.gguf"),
     2_382_000_000, "d4dcd426bfba75752a312b266b80fec8136fbaca13c62d93b7ac41fa67f0492b", "Qwen3-VL-4B text encoder for Krea 2 (Apache-2.0)"),
    ("models/krea2_vae.safetensors", HF.format(repo="Comfy-Org/Krea-2", path="vae/qwen_image_vae.safetensors"),
     242_000_000, "a70580f0213e67967ee9c95f05bb400e8fb08307e017a924bf3441223e023d1f", "Krea 2 VAE"),
    ("models/KREA-2-LICENSE.pdf", HF.format(repo="Comfy-Org/Krea-2", path="LICENSE.pdf"),
     200_000, "b82a2805162bde714a4eb27b9063c4fc3345d08a30be055134a6160e5430ba74", "Krea 2 Community License"),
]
# The engine's CUDA 12 runtime: copied from llama.cpp's folder instead of downloading it again (537 MB).
CUDA_DLLS = ("cudart64_12.dll", "cublas64_12.dll", "cublasLt64_12.dll")
CUDA_FROM = (*paths.prism_dirs(), *paths.llama_dirs())


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch(root: str, rel: str, url: str, size: int, digest: str, note: str, log) -> bool:
    dest = os.path.join(root, rel)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.exists(dest):
        if sha256(dest) == digest:
            log(f"OK (already here, verified) {rel}")
            return True
        log(f"{rel}: checksum mismatch on the existing file - downloading again")
        os.remove(dest)
    part = dest + ".part"
    have = os.path.getsize(part) if os.path.exists(part) else 0
    headers = {"User-Agent": "AgenticWorkspace-image-setup/1.0"}
    if have:
        headers["Range"] = f"bytes={have}-"
    t0, last = time.time(), 0
    with httpx.stream("GET", url, headers=headers, follow_redirects=True, timeout=httpx.Timeout(60, read=300)) as r:
        if r.status_code == 200 and have:
            have = 0                                        # the server ignored the range: start over
        elif r.status_code not in (200, 206):
            log(f"FAILED {rel}: HTTP {r.status_code}")
            return False
        with open(part, "ab" if have else "wb") as f:
            done = have
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                done += len(chunk)
                if time.time() - last > 15:
                    last = time.time()
                    rate = (done - have) / max(1e-6, last - t0) / 1e6
                    log(f"... {rel}: {done / 1e6:,.0f} / ~{size / 1e6:,.0f} MB ({rate:.1f} MB/s)")
    got = sha256(part)
    if got != digest:
        os.remove(part)
        log(f"FAILED {rel}: SHA-256 {got} does not match the published {digest} - deleted")
        return False
    os.replace(part, dest)
    log(f"OK (verified SHA-256) {rel} - {note}")
    return True


def install_engine(root: str, log) -> bool:
    z = os.path.join(root, FILES[0][0])
    engine = os.path.join(root, "engine")
    with zipfile.ZipFile(z) as f:
        names = f.namelist()
        f.extractall(engine)
    exe = [n for n in names if n.lower().endswith(("sd-server.exe", "sd-cli.exe", "sd.exe"))]
    log(f"Engine unpacked: {', '.join(exe) or 'no executables found!'}")
    for dll in CUDA_DLLS:
        src = next((os.path.join(d, dll) for d in CUDA_FROM if os.path.exists(os.path.join(d, dll))), None)
        if not src:
            log(f"MISSING {dll}: not found in {CUDA_FROM} - the CUDA runtime zip is needed")
            return False
        dst = os.path.join(os.path.dirname(os.path.join(engine, exe[0])) if exe else engine, dll)
        if not os.path.exists(dst):
            shutil.copy2(src, dst)
        log(f"Copied {dll} from {os.path.dirname(src)}")
    return bool(exe)


def main(root: str) -> int:
    logf = open(os.path.join(root, "setup.log"), "a", encoding="utf-8")

    def log(msg):
        line = f"{time.strftime('%H:%M:%S')} {msg}"
        print(line, flush=True)
        logf.write(line + "\n")
        logf.flush()

    ok = True
    for rel, url, size, digest, note in FILES:
        for attempt in range(3):
            try:
                if fetch(root, rel, url, size, digest, note, log):
                    break
            except Exception as e:                           # a dropped connection: resume from the .part file
                log(f"retry {rel}: {type(e).__name__}: {e}")
                time.sleep(5)
        else:
            ok = False
    ok = install_engine(root, log) and ok
    manifest = {rel: {"sha256": digest, "source": url, "note": note} for rel, url, _s, digest, note in FILES}
    with open(os.path.join(root, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    log("ALL DONE - every file verified" if ok else "DONE WITH ERRORS - see above")
    return 0 if ok else 1


if __name__ == "__main__":
    target = sys.argv[1] if len(sys.argv) > 1 else paths.feature_root("Images")
    os.makedirs(target, exist_ok=True)
    sys.exit(main(target))
