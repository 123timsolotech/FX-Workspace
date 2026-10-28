# Changelog

What changed in each version of FX Workspace, newest first. Each version links to its full release notes. Installers are on the [Releases](https://github.com/fuxion64-ctrl/FX-Workspace/releases) page.

Parts marked **Beta** work, but are still being finished.

## 1.4.3 - 2026-10-28

[Full notes](docs/releases/1.4.3.md)

- **Orbs, a new thinking mark:** three orbs that fly into a new pattern for each kind of work - thinking, reading, searching, a tool, writing, drawing, waiting - dressed for each appearance. Legacy keeps the old marks.
- **Every appearance checked**, screen by screen in light and dark: contrast, focus rings and cut-off text fixed everywhere. The Claude, ChatGPT, Grok and Gemini looks are out of Beta.
- **A welcome for new installs:** a chat model that suits this PC in one click, a look, and what stays on this PC.
- **What's new** shows once after an update.
- **Settings search goes to the setting itself;** Connections (connectors, MCP servers, channels) and Web search and network are one page each.
- **Notifications held back** by Focus assist or a full-screen game are explained, with the way to change it.
- **Steadier:** the hidden approval window no longer takes the keyboard focus at startup; repeated background model calls fold into one line.

## 1.4.2 - 2026-10-28

[Full notes](docs/releases/1.4.2.md)

- **See what each chat costs:** a token breakdown behind the context meter, tool groups off per chat, status dots, and a monitor for background model calls.
- **Keeps working with the window closed:** close to tray, done notifications, start with Windows, and the model unloads when hidden and idle.
- **A built-in mini window** with a chat strip replaces the Edge overlay; the assistant can move it when you ask.
- **Use it from your phone** over Tailscale or your home network, with a password.
- **Discord and Slack (Beta) and a webhook,** with approvals in the channel.
- **A Soul file, memory that replaces old facts** and linked memory notes; Work and Assistant chats.
- **Sandboxed chats** and Windows Sandbox runs for risky tasks.
- **Labs:** an MoE cache speed test and the NCM story writer (Beta).
- **Safer:** prompt-injection defences, fair answers on politics, and a Relaxed limits switch for uncensored models.
- **Update now** in Settings › About, checksum-checked. Updates remove old packages and the old engine build, and pip keeps no cache.
- **Setup runs inside the wizard,** with no console window, and `run.bat` opens the app instead of Edge.
- **Stop keeps the half-written reply** instead of erasing it.

## 1.4.1 - 2026-10-27

[Full notes](docs/releases/1.4.1.md)

- **Replies start in about a second, not 20-40.** The app was rebuilding its tool list and scanning every process on the PC before each reply.
- **The model loads while you type** (Beta), so the first reply doesn't wait for a load from disk.
- **Stop works every time,** including while a model is loading. A reply ends within a tenth of a second.
- **The System panel reads correctly:** no more impossible GPU memory numbers, and Engine names your local model.
- **Every icon has its own hover animation** (92 of them).
- **Steadier local models and safer files,** from an outside code audit: a shared model lock, `models.ini` written safely, checked downloads, and protected files kept out of search.
- No longer a beta as a whole. Fx Labs and the AI-inspired appearances stay Beta.

## 1.4.0 - 2026-10-21

[Full notes](docs/releases/1.4.0.md)

- **A new interface:** one Customize window, a Home that shows what's waiting, new fonts. The old layout stays as Legacy.
- **It learns how you work:** a profile you approve, memory topics, Agent Skills it can write and load, and search across past chats.
- **Your files, mail and apps:** a knowledge base over your folders, Mail over IMAP/SMTP, and ten one-click connectors.
- **New places:** Notes and tasks, Docs with suggested edits and versions, a blind Compare with a leaderboard, and Find models on Hugging Face.
- **A new Labs** (Beta) with a home page, a clearer step rail and a blind check before a fine-tune is used.
- **Speed you can see:** tokens per second on each reply, a speed chart, and a Diagnose session offer.
- **Four AI-inspired appearances** (Beta), and thinking motion that differs per task.

## 1.3.0 - 2026-10-16

[Full notes](docs/releases/1.3.0.md)

- **A desktop app:** FX Workspace opens in its own window with a tray icon.
- **A setup wizard:** a per-user install that updates in place and keeps your data. The app checks GitHub for updates.
- **A lighter, faster UI:** theme switches 10x faster, and Settings › Models opens instantly.

## Before 1.3.0

These were published as source only, without an installer.

- **2026-10-16 - Fx Labs beta:**
  - fine-tune Qwen 3.5 chat models (0.8B-4B, QLoRA), and port a fine-tune into Workspace as a normal model;
  - a redesigned Labs, with networks you can configure and trace.
- **2026-10-16 - Fx Labs phase 1:**
  - a second mode for training a small GPT or a picture classifier from scratch, with live charts;
  - picture viewer zoom, and fixes to the built-in browser.
- **2026-10-15 - Music studio:**
  - a waveform timeline where you change a chosen part;
  - steadier video (Wan 2.2 Steady motion);
  - chats stay usable while a model loads.
- **2026-10-14 - First public release.**
