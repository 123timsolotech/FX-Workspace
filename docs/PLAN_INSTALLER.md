# Plan: a finished Windows app and a full setup wizard

Written 2026-10-27, after 1.4.1. Goal: one signed `FX-Workspace-Setup-<ver>.exe`. It installs a self-contained app: no Python, pip or PowerShell window during setup, user data kept apart from the program, and a wizard that sets up a PC end to end. Built in phases; each phase stops for review before the next.

## Where it stands (1.4.1)

| | Today | Problem |
|---|---|---|
| Installer | Inno Setup 6, 93 MB, per-user, no admin | Good base: setup types, components, models-folder page, update detection, `.fx-manifest` cleanup |
| Python | `install.ps1` finds Python 3.11/3.12 or installs it with winget, then pip builds a ~495 MB `.venv` | Needs internet. A changed package or a winget failure breaks setup on that PC. A console window opens during setup |
| App folder | 235 loose files: code, `test_*.py`, `fixtures/`, `docs/`, `models.ini.bak*`, leftovers like `snake_game.html` | Looks unfinished, and the uninstaller and updates have to tiptoe around it |
| User data | `data\`, `settings.json`, `keys.local.bat`, `models.ini` inside the program folder | Updates must skip them file by file; the app can't live in Program Files. 45 modules (~208 references) assume `data\` sits next to the code |
| Signing | none | SmartScreen says "Unknown publisher" / "Windows protected your PC" |
| Updates | the app checks GitHub, the user downloads and runs the next installer | Works, but takes several manual steps |

## Target layout

```
%LOCALAPPDATA%\Programs\FX Workspace\        program - replaced whole on update
    FX Workspace.exe                         the desktop app (Electron)
    runtime\                                 Python 3.11 + every package, prebuilt, hash-locked
    app\                                     only the files the app runs (compiled .pyc + the page and its assets)
    unins000.exe
%LOCALAPPDATA%\FX Workspace\                 user data - never touched by updates or a plain uninstall
    chats\ images\ videos\ music\ voice\ ...  (what data\ holds now)
    settings.json  models.ini  keys (encrypted with Windows DPAPI)
    logs\  backups\
<drive chosen in setup>\FX Workspace Models\ engines and models (llama.cpp builds, GGUFs, image models)
```

Data goes in Local, not Roaming, AppData: it reaches many GB (voice alone is ~3 GB here), and Roaming is for small, synced settings.

## Phase 1 - User data apart from the program

1. **One source of paths.** Every module asks a single module for paths: `paths.data_dir()`, `paths.settings_file()`, `paths.models_ini()`, `paths.keys_store()`. The 45 modules that build `data\...` themselves move to it. A test fails the build if a module builds a data path on its own.
2. **Move on first start, safely.** When the old layout is found, copy (never move first) `data\`, `settings.json`, `models.ini` and `keys.local.bat` to the new folder. Check file counts and sizes, then switch over. The old copy is kept until the user confirms in Settings ("Remove the old copy - 4.2 GB").
3. **Keys.** `keys.local.bat` becomes `keys.json` encrypted with Windows DPAPI, readable only by this Windows user. The `.bat` is still read once, for the move.
4. **Settings › Storage.** Shows where data, models and logs live, how big each is, and "Open folder" / "Move…" (move models to another drive).
5. **Exit check:** the full test suite, plus a live run on a copy of this PC's real data (237 chats, images, voice), then a rollback test.

## Phase 2 - A self-contained app

1. **Bundled Python.** At build time, take the official python.org "embeddable" 3.11 package and install the exact packages from a hash-locked requirements file into `runtime\`. Nothing is downloaded or compiled on the user's PC. The `.venv` and `install.ps1`'s Python part go.
2. **Runtime files only.** The build ships an allowlist: the app's modules (compiled to `.pyc`), `index.html`, `static/` and fonts. Tests, fixtures, docs, backups and scratch files stay in the repo. A build check refuses to package any `test_*`, `*.bak` or top-level `.html` that isn't the app's page.
3. **Heavy extras stay optional:**
   - Labs (PyTorch, about 2.5 GB) and the image, voice and music engines download on demand from Settings, with progress, as today.
   - The installer itself is then about 150-250 MB.
4. **Launcher.** The desktop app starts `runtime\python.exe` directly, so there's no venv shim and nothing depends on PATH.
5. **Exit check:** install on a clean Windows Sandbox with no Python at all. The first reply works offline (the engine and model on disk).

## Phase 3 - The full setup wizard

All native wizard pages, with progress bars in the wizard (Inno Setup 6's download page, which checks SHA-256). No console windows.

1. **Welcome.** What FX Workspace is, what will be set up, and that it runs locally. An "Update 1.4.1 → 1.5.0" variant shows the release notes instead.
2. **License.**
3. **This PC check.** Read before any choice:
   - Windows version, GPU and its VRAM, RAM, and free space on every drive.
   - Each line is marked OK, Limited (for example "4 GB VRAM: models up to ~4B") or Problem.
   - It warns about a nearly full system drive. This PC's C: has ~4 GB free.
4. **Setup type.**
   - *Recommended:* picked from the check.
   - *Custom:* every page below.
   - *Minimal:* the app only, for cloud models or models you already have.
5. **Locations.** Program, data and models folders, each with the drive's free space and how much this setup needs there. Models default to the roomiest drive.
6. **Models you already have.** Scans the chosen folders and common places (for example `E:\ClankerLocalCpp`) for `.gguf` files. Lists them with size, then a tick per model to add it to the app, so no download is needed.
7. **Chat model.** If none is chosen or found, it suggests one that fits the measured VRAM, with size, VRAM need and expected speed (small, balanced, larger).
8. **Engine.** The llama.cpp build for this GPU (CUDA, Vulkan or CPU) with a one-line reason. Advanced users can pick another or add PrismML's build.
9. **Optional features,** each with download size and disk need: images, voice, music, Labs (training). All can be added later from Settings.
10. **Shortcuts and start-up.** Desktop shortcut, Start menu, start with Windows, and open after setup.
11. **Ready.** A summary: total download, disk use per drive, and what will happen.
12. **Installing.**
    - One progress bar per step, each download resumable and SHA-256 checked.
    - A failed step offers Retry, Skip (where safe) or Save log. Nothing half-installed is left registered.
13. **Finish.** Launch, plus "Show me around" (a short first-run tour in the app) and a link to the release notes.

**Running setup again or "Modify" in Windows Settings** opens a maintenance wizard:
- **Add/remove features.**
- **Repair:** re-checks the program files' hashes and restores damaged ones.
- **Move models.**

**Uninstall** asks separately:
- Remove my chats and settings? (off by default)
- Remove downloaded models and engines? (off by default, with the size shown)

Models it didn't download are never touched.

**Exit check:**
- Each type, run in Windows Sandbox.
- A PC with no NVIDIA GPU.
- An update from 1.4.1 with real data.
- Repair after deleting files, and uninstall with each choice.

## Phase 4 - Signed and self-updating

1. **Code signing.** Sign the installer, the app `.exe` and the uninstaller with an Authenticode certificate, so Windows shows "Fuxion" as the publisher. Options to decide:
   - SignPath (free for open-source projects, needs approval).
   - Certum's open-source certificate (about €30-60 a year).
   - Microsoft Trusted Signing (about $10 a month).
   - A standard OV certificate (about $200-400 a year).

   SmartScreen still warms up over the first downloads, but the "Unknown publisher" warning goes.
2. **In-app updates.** The update card downloads the new installer in the background and verifies its signature and SHA-256. On "Restart to update" it closes the app, updates silently and reopens. Data is backed up first (`backups\`), and a failed update rolls back.
3. **Release build on GitHub Actions.** A tag builds, tests, signs and drafts the release with the installer and its SHA-256 attached. A person still reviews and publishes it.

## Order, size and risks

- **Order:** Phase 1 → 2 → 3 → 4. Phases 1 and 2 are the foundation; the wizard's pages depend on them.
- **Biggest risk: the data move (Phase 1).** It touches every feature. That's why it copies first, verifies, and keeps the old copy.
- **Installer size:**
  - Online installer: ~200 MB, downloads models.
  - Offline installer, also built: includes the engine and a starter model, ~3 GB, for PCs without internet.
- **Not in scope:** Microsoft Store/MSIX packaging, Linux and macOS installers (the pull request with Linux/macOS notes is separate), and Labs' PyTorch inside the installer.

## Decisions to make before Phase 1

1. **Data folder:** `%LOCALAPPDATA%\FX Workspace` (recommended), or let setup choose any folder.
2. **Keys:** move them to DPAPI-encrypted storage (recommended), or keep the `.bat`.
3. **Signing:** which of the four options, or none for now.
4. **Installers:** online only, or online plus offline.
