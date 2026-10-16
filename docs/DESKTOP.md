# The desktop app and the installer

## Desktop app (`desktop/`, Electron)

FX Workspace runs in its own window - Electron's bundled Chromium, so no Edge or other installed browser is
involved. `desktop/main.js`:

- starts the app (`launcher.pyw`, with `FX_SHELL=electron`) unless a server already answers on 127.0.0.1:8000,
  and shows a loading screen until it does;
- shows the page in one window (size and position remembered), with a tray icon; a second start focuses it;
- opens links to other sites in your normal browser; the app's own pop-ups stay inside;
- allows the microphone (voice mode), notifications and the clipboard - for the app's own page only;
- keeps the browser shortcuts: F5 / Ctrl+R reload, F11 full screen, Ctrl +/- zoom, Ctrl+Shift+I developer tools.

Closing the window closes everything: the launcher watches the desktop app's process (`FX_PARENT_PID`) and, a
second after it is gone, stops the server and llama-server cleanly. (It is started detached on purpose - on
Windows a normal child would be killed with Electron before it could stop llama-server.)

Not moved yet: the Roblox Studio overlay (HUD) still opens as a small Edge app window, because it is styled and
positioned over Studio through Windows window handles.

Develop: `cd desktop`, `npm install`, `npm start` (uses the repo's `.venv`). Package: `npm run pack:win` ->
`desktop/dist/FX Workspace-win32-x64/FX Workspace.exe`.

## Installer (`installer/`, Inno Setup)

`installer\build.ps1` packages the desktop app, stages only the files git publishes (never `data\`, keys,
`.venv`, `local\` or files the assistant created in your chats) and compiles `installer\FXWorkspace.iss` into
`installer\output\FX-Workspace-Setup-<version>.exe`. Needs Inno Setup 6 (`winget install JRSoftware.InnoSetup`).

The wizard: welcome, licence, install folder (per user, no admin: `%LOCALAPPDATA%\Programs\FX Workspace`), where
models go (sets `AGENTIC_HOME` when it isn't the default), what to download (model engine, starter model, image
models), shortcuts. It then runs `install.ps1 -Yes` in a window that shows progress. The uninstaller asks
whether to also delete your chats and settings (with your API keys), and separately the downloaded models.

Tested on this PC: a silent install (app only) took ~4 minutes, the installed app started in 20 s and closed
cleanly, and the uninstaller removed the program, shortcut and Apps entry.

## Updating (for users)

Download the newer `FX-Workspace-Setup-<version>.exe` and run it - that's all. It finds the installed copy and
updates it in place:

- asks nothing it already knows (folder, models folder, what was installed, shortcuts) - it shows a summary
  "Update FX Workspace 1.3.0 -> 1.4.0" with what stays as it is, then Install;
- closes FX Workspace first if it's running (gracefully, so the model server stops too);
- replaces the app's own files and removes the ones the new version no longer has - only files on the list the
  last install wrote (`.fx-manifest`), so nothing of yours is ever deleted;
- runs the setup again, which updates only the Python packages the new version needs;
- keeps exactly as they are: chats, pictures, videos, music, memory and settings (`data\`), API keys
  (`keys.local.bat`), the model list (`models.ini`), models and the model engine (`local\` or your models
  folder), the Python environment (`.venv`) and any other file in the folder.

Running an older installer over a newer install asks first. Settings > About > **Check for updates** asks GitHub
for the newest release (only when pressed) and links its installer.

Tested on this PC: 1.3.0 installed with a chat, custom keys, an edited models.ini and a personal file, the app
running; the 1.3.1 installer closed the app, updated in 22 s, kept all of those and the same `.venv`, removed the
file the new version dropped, and the updated app started and reported 1.3.1.

## Releasing an update (for the maintainer)

1. Raise the number in `VERSION` (e.g. `1.4.0`).
2. Run `installer\build.ps1` -> `installer\output\FX-Workspace-Setup-1.4.0.exe`.
3. Commit and push, then publish a GitHub Release tagged `v1.4.0` with that .exe attached (its name must contain
   "Setup" and end in .exe - that's what Check for updates links).

## Linux

- The desktop app is cross-platform: `npm run pack:linux` builds it; it looks for `.venv/bin/python`.
- The launcher works on Linux too (file lock instead of a Windows mutex, `llama-server` without `.exe`, no
  console-hiding), and the Windows-only packages are marked `sys_platform == "win32"` in requirements.txt.
- What doesn't work there: everything that drives Windows - the desktop tools (apps, windows, UI automation),
  PowerShell commands, media keys, the Studio overlay, and `install.ps1` (set up by hand: `python3 -m venv .venv`,
  `.venv/bin/pip install -r requirements.txt`, and a llama.cpp Linux build in `local/llama.cpp`). Chat, web
  search, images, music, video and Labs are the parts expected to work.
- **Not tested on Linux yet** - there was no Linux machine or WSL to run it on.
