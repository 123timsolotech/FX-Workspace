"""
update_cleanup.py
=================
Tidies an installed copy after setup or an update (install.ps1 runs it with the app's own Python), so updates
don't pile up leftovers (a request from 123timsolotech, 1.4.2):

  packages  Python packages an older version needed and this one doesn't - only those: the packages the previous
            requirements.txt brought in (directly or as a dependency) that nothing still wanted needs. Packages
            a feature installed on its own (voice, connectors) and everything they depend on are kept. Then the
            current requirements are remembered in .venv/fx-requirements.txt for the next update.
  tidy      __pycache__ folders of the app's own code (Python rebuilds them; old ones keep compiled copies of
            removed files), and models.ini backups beyond the newest three.

Prints one line per change and a total; never touches data\\, local\\ (models, engines), keys or settings.
Run: .venv\\Scripts\\python.exe update_cleanup.py packages|tidy
"""
from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT = os.path.join(ROOT, ".venv", "fx-requirements.txt")
ALWAYS_KEEP = {"pip", "setuptools", "wheel"}
SKIP_DIRS = {".venv", "data", "local", "desktop-app", "node_modules", ".git"}
KEEP_INI_BACKUPS = 3


def canon(name: str) -> str:
    return re.sub(r"[-_.]+", "-", str(name or "")).lower().strip()


def read_requirements(path: str) -> dict:
    """{canonical name: set of extras} from a requirements file ("" lines, comments and options skipped)."""
    out: dict = {}
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in lines:
        line = line.split("#", 1)[0].strip()
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(\[([^\]]*)\])?", line)
        if not line or line.startswith("-") or not m:
            continue
        extras = {canon(e) for e in (m.group(3) or "").split(",") if e.strip()}
        out.setdefault(canon(m.group(1)), set()).update(extras)
    return out


def _requirement(spec: str):
    try:
        from packaging.requirements import Requirement
    except ImportError:                                    # pip carries its own copy
        from pip._vendor.packaging.requirements import Requirement
    return Requirement(spec)


def dependencies(dist, extras: set) -> dict:
    """What one installed distribution needs on this PC: {name: extras}, with markers evaluated."""
    out: dict = {}
    for spec in dist.requires or []:
        try:
            req = _requirement(spec)
        except Exception:
            continue
        if req.marker is not None:
            envs = [{"extra": e} for e in (extras or {""})]
            try:
                if not any(req.marker.evaluate(env) for env in envs):
                    continue
            except Exception:
                continue
        out.setdefault(canon(req.name), set()).update(canon(e) for e in req.extras)
    return out


def closure(roots: dict, installed: dict) -> set:
    """Every installed distribution the roots need, themselves included."""
    seen: dict = {}
    todo = [(n, set(x)) for n, x in roots.items()]
    while todo:
        name, extras = todo.pop()
        if name not in installed:
            continue
        new = extras - seen.get(name, set()) if name in seen else extras
        if name in seen and not new:
            continue
        seen.setdefault(name, set()).update(extras)
        for dep, dep_extras in dependencies(installed[name], extras).items():
            todo.append((dep, dep_extras))
    return set(seen)


def installed_distributions() -> dict:
    from importlib import metadata
    out = {}
    for d in metadata.distributions():
        name = canon(d.metadata.get("Name") or "")
        if name:
            out[name] = d
    return out


def unneeded(old: dict, new: dict, installed: dict) -> list:
    """Packages to remove: brought in by the old requirements, needed by nothing kept."""
    before = closure(old, installed)
    roots = {**{n: set() for n in installed if n not in before}, **new, **{n: set() for n in ALWAYS_KEEP}}
    keep = closure(roots, installed)
    return sorted(before - keep - ALWAYS_KEEP)


def packages(dry_run: bool = False) -> list:
    new = read_requirements(os.path.join(ROOT, "requirements.txt"))
    old = read_requirements(SNAPSHOT)
    gone = unneeded(old, new, installed_distributions()) if old else []
    if gone and not dry_run:
        r = subprocess.run([sys.executable, "-m", "pip", "uninstall", "-y", "--disable-pip-version-check", *gone])
        if r.returncode != 0:
            print("Some old packages could not be removed - they stay, and nothing else changes.")
            return []
    for name in gone:
        print(f"Removed {name} (no longer used)")
    if not dry_run and new:
        shutil.copyfile(os.path.join(ROOT, "requirements.txt"), SNAPSHOT)
    return gone


def _size(path: str) -> int:
    total = 0
    for base, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(base, f))
            except OSError:
                pass
    return total


def tidy(root: str = ROOT) -> int:
    """Returns the bytes freed."""
    freed = 0
    for base, dirs, _files in os.walk(root):
        if base == root:
            dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for d in [d for d in dirs if d == "__pycache__"]:
            path = os.path.join(base, d)
            size = _size(path)
            shutil.rmtree(path, ignore_errors=True)
            if not os.path.exists(path):
                freed += size
            dirs.remove(d)
    backups = sorted(glob.glob(os.path.join(root, "models.ini.bak*")), key=os.path.getmtime, reverse=True)
    for path in backups[KEEP_INI_BACKUPS:]:
        try:
            size = os.path.getsize(path)
            os.remove(path)
            freed += size
            print(f"Removed an old backup: {os.path.basename(path)}")
        except OSError:
            pass
    return freed


if __name__ == "__main__":
    what = sys.argv[1] if len(sys.argv) > 1 else ""
    if what == "packages":
        gone = packages()
        print(f"Old packages removed: {len(gone)}" if gone else "No old packages to remove.")
    elif what == "tidy":
        print(f"Tidied up: {tidy() / 1048576:.0f} MB freed.")
    else:
        print(__doc__)
        sys.exit(2)
