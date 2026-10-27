# FX Workspace 1.4.1

A fix-up release on top of 1.4.0: replies start far sooner, Stop always works, and the System panel reads correctly. It includes an outside code audit (Astra, 2026-10-26), reviewed and tested on Windows before release.

FX Workspace is no longer a beta as a whole. Only the newest parts below are still marked **Beta**: they work, but they're still being finished.

**Still beta:**
- **Fx Labs** - training and fine-tuning your own models (marked Beta in the logo menu).
- **AI-inspired appearances** - the Claude, ChatGPT, Grok and Gemini styles (marked Beta in Settings › Appearance). Some details are still off.
- **The model loads while you type** - new in 1.4.1.

**Updating:** run `FX-Workspace-Setup-1.4.1.exe` over 1.4.0. It closes the app, keeps your chats, settings, keys and models, and updates only the app's own files.

## Replies start in about a second, not 20-40

1.4.0 spent most of a reply's wait inside the app, before the model was even asked. A short "hi" took 20-40 s on a model that answers in under one. Measured live on an RTX 3060 Ti, same model and messages:

| Message | 1.4.0 | 1.4.1 |
|---|---|---|
| "hi" (model loads first) | 39.8 s | 17.3 s |
| "what is 2+2?" | 32.3 s | 1.6 s |
| "thanks" | 20.9 s | 0.9 s |

- **Building the tool list re-read every skill and rescanned every connected MCP tool once per tool** (116 times) for each model call and each refresh of the context meter. Now it's worked out once.
- **Finding the running llama-server read the command line of every process on the PC** (3-8 s with ~450 processes). The Labs page and every reply asked for it. It now asks which process listens on the router's port, which takes a few ms.
- **The page's status refreshes no longer starve replies.** Idle, the server used 0.7 of a CPU core; that's mostly gone. MCP tool descriptions, model file sizes and the nvidia-smi location are remembered instead of being looked up again on every refresh.
- **The model loads while you type** (beta). In Local mode, the first keystroke in the message box starts loading your chosen model if it isn't loaded yet. A 3 GB model takes ~20 s to load from a hard drive, and that now overlaps your typing.

## Stop works every time

- **Stop during "Checking the message" was ignored.** That step often waits for the model to load, and the turn then ran to the end. The whole turn can now be stopped: it ends within a tenth of a second, during a load or while the reply is being written.
- **A reply waiting for the model** (behind a load or another call) can be stopped or paused too.
- **A model that stalls before its first word** no longer holds Stop for up to 30 seconds.

## System panel

- **GPU memory no longer shows numbers like 17179865097.2 / 8.0 GB.** nvidia-smi sometimes reports a wrapped value; such a reading is now dropped and the last good one stays on screen.
- **Engine names your local model in Local mode,** instead of a cloud model (gemini-3.6-flash) before the first local reply.

## Every icon animates

Before, 48 icons had their own hover animation and 44 only nudged up 1.5 px. Now all 92 have one:
- The bell rings and the chart's bars grow.
- The gauge needle sweeps and the keys press.
- The corners of Expand push out and the link's halves pull apart.
- The sun's rays turn.

Reduced-motion settings still turn them off.

## Speed numbers you can trust

- **The live counter now says char/s, not tok/s.** It used to count streamed messages, and the server bundles several pieces of text into one message, so the number reflected delivery batching rather than the model's speed.
- **The tok/s on a finished reply now comes from llama-server's own timing** when it reports it, otherwise from the streamed output interval. Helper calls without timing no longer drag the average down.
- **Less background work per request:** GPU memory isn't re-measured on every reply when the same model is already loaded.

## Steadier local models

- **One shared reservation for the local model.** Replies, helper calls, model loads, unloads and engine switches no longer step on each other. A Settings change that would restart llama-server waits for the next start instead of cutting off a reply in progress.
- **Changing one resource setting no longer resets the others.** Updating just the context size keeps your GPU layers, batch and thread overrides.
- **models.ini is written atomically under one lock,** so a scan, a download and Settings can't corrupt it at the same time.
- **Model downloads:** files already on disk are checked against the published SHA-256, resumed downloads check the byte range, and repos with identically named files no longer share a folder.

## Safer files and chats

- **Knowledge search skips protected files** (keys, settings, mail config) and links that point outside the chosen folder. Re-index to remove any protected entries an older index still holds.
- **Indexing:** same-size edits are picked up, a cancelled scan keeps the existing index, and folder filters match whole folders only.
- **Zip extraction** checks real paths, so a link inside the destination can't redirect files elsewhere.
- **An attachment named meta.json** no longer overwrites the upload's own metadata.
- **Chat saves** can't be overwritten by a rename, pin or move happening at the same moment, and long chats are trimmed at whole turns, so a tool result is never separated from its call.
- **Timed-out read-only tools aren't retried automatically** while the first attempt may still be running.

## Kept from 1.4.0

The audit suggested capping chat history at each effort level's budget (about 6K tokens for Quick). 1.4.1 keeps 1.4.0's behaviour instead: history is limited only by the context you set for the model in Settings › Models.

## Known issues

- The first message after the app starts still waits for the model to load from disk, unless it loaded while you typed. The load time depends on the drive: an SSD is several times faster than a hard drive.
- Hugging Face repos with very many files may still list incompletely (no pagination yet).

Full technical detail: `docs/AUDIT-2026-10-26.md` and `docs/PERFORMANCE-FIXES-2026-10-26.md`.
