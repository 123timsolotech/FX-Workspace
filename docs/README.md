# Documentation

| Folder or file | What's in it |
|---|---|
| [`../CHANGELOG.md`](../CHANGELOG.md) | What changed in each version, newest first |
| [`releases/`](releases/) | Full release notes per version (`1.4.1.md`, ...), pasted into each GitHub release |
| [`FEATURES.md`](FEATURES.md) | Everything the app does, feature by feature |
| [`DESKTOP.md`](DESKTOP.md) | The desktop app and installer: how they work and how to build a release |
| [`audits/`](audits/) | Code and performance audits, and what was fixed from them |
| [`plans/`](plans/) | Plans for upcoming work, such as the self-contained installer |
| [`../ARCHITECTURE.md`](../ARCHITECTURE.md) | How the code is organized |

## Making a release

1. Raise the number in `VERSION`.
2. Add `docs/releases/<version>.md` and a short entry at the top of `CHANGELOG.md`.
3. Run `installer\build.ps1`.
4. Commit and push.
5. On GitHub, create a release:
   - **Tag:** `v<version>`
   - **Title:** `FX Workspace <version>`
   - **Description:** the release notes
   - **Attachment:** `installer\output\FX-Workspace-Setup-<version>.exe`
