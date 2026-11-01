# FX Workspace 1.4.5 review

Reviewed 2026-10-31. The fixes for items 01 to 08 are in this branch; items 09 to 23 are open.

## Summary

The review found 23 issues: 8 are fixed in this branch, 15 are open. The three most serious (01 to 03) are among the fixed ones: closed chats stay closed on every route, long messages are checked in full, and Hebrew words are matched as whole words.

The review covered the chat limits system end to end (backend, API, channels, UI and settings) and ran a broad pass over the 21,000-line interface file. Findings were checked by reading the code, by running the test suite, and by sending real messages to two local models: Huihui-Qwen3.5-9B-abliterated (Q4_K_M) and Qwen3.8-27B-OBLITERATED (IQ4_XS).

Tests: `python -m unittest tests.test_chat_guard tests.test_images` passes (86 tests). In the full suite, the failures that remain also occur on unmodified 1.4.5 (missing optional packages, real-browser tests).

## Issues

| ID | Area | Issue | Severity | Status | Confirmed by |
| --- | --- | --- | --- | --- | --- |
| 01 | Channels | A closed chat keeps answering through Slack, Discord, Telegram and the webhook, including chats closed permanently for minors | Critical | Fixed | Code, tests |
| 02 | Chat limits | The model only sees the first 3,000 characters, so a harmful request after that is allowed | High | Fixed | Model test (9B) |
| 03 | Image filter, chat limits | Hebrew keywords match inside ordinary words ("minimal", "miniature"), so ordinary Hebrew image requests are refused | High | Fixed | Rules test: 5 of 6 flagged before, 0 after |
| 04 | Chat limits | The verdict parser takes the first category word, so "not MALWARE, ALLOW" counts as a strike | High | Fixed | Tests |
| 05 | Chat limits | A keyword match alone, with no model confirmation, locks a chat permanently | High | Fixed | Tests |
| 06 | Chat limits, image filter | English keyword rule flags 22 of 27 ordinary messages, mostly coding questions | High | Fixed | Rules test |
| 07 | Chat limits, image filter | English keyword rule misses 6 of 19 abbreviated or slang requests ("u18", "12 y.o.", "jailbait") | High | Fixed | Rules test |
| 08 | Chat limits | The 9B model allowed "jailbait nudes": the policy text did not name slang | Medium | Fixed | Model test (9B) |
| 09 | Chat limits | When the check fails (model loading, busy, timeout), messages pass unchecked and nothing tells the user | Medium | Open | Code |
| 10 | Chat limits, performance | The check runs on the chat model: +3.9 s per message with the 27B model on an 8 GB GPU; in cloud mode each message goes to the provider twice | Medium | Open | Measured, code |
| 11 | UI | Reopening a closed chat is a hidden double-click on an icon hidden from screen readers; no keyboard or reliable touch route, not mentioned in the app | Medium | Open | Code |
| 12 | UI | The strike count is never shown; the server sends it but the page ignores it | Medium | Open | Code |
| 13 | Chat limits | Attached files are not screened, only the message text | Medium | Open | Code |
| 14 | Chat limits | Free-text answers can be cut off at 24 tokens; constraining the answer to the six labels would remove parse failures | Medium | Open | Suggestion |
| 15 | Chat limits | The keyword rule joins the previous message, so a message after a refused one also matches when the check fails | Low | Open | Code, model test |
| 16 | Settings | "Relaxed limits" says messages are no longer checked, but every message is still checked (for minors), with the same delay | Low | Open | Code |
| 17 | Settings | Emptying "Counts fade after" saves 0, which means counts never fade | Low | Open | Code |
| 18 | UI | The sidebar does not mark which chats are closed | Low | Open | Code |
| 19 | Code quality | Unused CSS from the removed passphrase dialog, and an orphaned comment | Low | Open | Code |
| 20 | UI | 32 native browser confirm() dialogs instead of the app's own modals | Low | Open | Count |
| 21 | UI | 103 text styles below 12 px | Low | Open | Count |
| 22 | Code quality | 47 empty catch blocks swallow errors silently | Low | Open | Count |
| 23 | Docs | ARCHITECTURE.md describes another machine (Windows 10, RTX 3060 Ti, E:\ paths) | Low | Open | Code |

## Fixes and details

Where each problem starts, why it happens, and the fix, under the same ID. Line numbers are for this branch.

| ID | Where | Cause | Fix |
| --- | --- | --- | --- |
| 01 | `api/server.py` `_channel_turn`; `connect/channels.py` `run_turn`, `hook_message` | `/api/chat` checks `chat_guard.is_locked` before starting a turn; `_channel_turn` started turns directly, so that check never ran. | Fixed: the turn itself refuses a closed chat (`agent/tasks.py`, `_run_turn_in_chat`), so every route is covered and nothing is saved to it. A channel's closed chat is reported, and `connect/channels.py` forgets it (`_forget_chat_id`), so that channel's next message starts a new chat. |
| 02 | `chat/chat_guard.py` `screen` (was `text[:3000]`) | Only the first 3,000 characters went to the model. The webhook accepts 8,000. | Fixed: a long message is checked in 3,000-character windows that overlap by 300 (`_windows`); the most severe verdict counts. Past 8 windows (about 22,000 characters), 8 evenly spaced windows from start to end are checked; tune `MAX_WINDOWS` to trade coverage for speed. Measured with the 9B model: 15,000 characters in 6 checks, about 2 s. |
| 03 | `media/image_gen.py` Hebrew parts of `_SEXUAL` and `_MINOR` | No word boundaries: "מיני" matched inside "מינימלי" and "מיניאטורי", "נער" inside "נערך". The image filter uses these rules with no model. | Fixed: Hebrew words must stand alone (`_he`), with up to three one-letter prefixes; "כל מיני" (all kinds of) is excluded; Hebrew ages (בת 13, בן 12) added. Ordinary Hebrew questions flagged: 5 of 6 before, 0 of 6 after; requests that must be caught: 5 of 5 both times. |
| 04 | `chat/chat_guard.py` `parse_verdict` | Took the first category word in the reply. | Fixed: a marked answer ("Answer: X"), then a leading label, then the last capitalised label. |
| 05 | `chat/chat_guard.py` `record_refusal`; `agent/tasks.py` `_screen` | A keyword-only `minors` refusal locked the chat for good. | Fixed: refused and logged, no lock unless the model confirms. |
| 06 | `media/image_gen.py` `_SEXUAL`, `_MINOR` | "explicit", "child", "minor" and similar counted in their programming senses. | Fixed: technical senses excluded. Ordinary messages flagged: 22 of 27 before, 3 of 27 after (sex education and abuse-reporting questions, which the model check allows). |
| 07 | Same as 06 | No patterns for abbreviations and slang. | Fixed: "13yo", "12 y.o.", "14 yr old", "u18", "jailbait", "middle schooler", "child-like" and others. Requests caught: 13 of 19 before, 19 of 19 after. |
| 08 | `chat/chat_guard.py` `_POLICY` | The policy text did not say minors are often named by slang. | Fixed: slang and abbreviations named. Both models then caught 19 of 19 and refused 0 of 27 ordinary messages. |
| 09 | `chat/chat_guard.py` `screen` (`except Exception: verdicts.append(None)`); `agent/tasks.py` `_screen` | A failed check allows the message (fail open). Only the event log records it. | Retry once after the model loads, and show a small note when a message was not checked. |
| 10 | `agent/tasks.py` `_guard_model` | `llm.model_text` with no model uses the model in use: a large local model, or the cloud provider. | Add a setting for a small local check model (the 9B took 0.14 s per check), or run the check alongside the answer and discard the answer if refused. |
| 11 | `index.html` `renderLockState` | Reopening is an `ondblclick` on a `<span aria-hidden="true">`. A keyboard cannot reach it, a screen reader skips it, and double-tap is unreliable over phone access. | Add a visible "Reopen chat" action (for example in Settings > Privacy, or the chat's menu) and mention it in the settings text. |
| 12 | `agent/tasks.py` `_screen` returns `guard.strikes`; `index.html` never reads it | The count is sent but not shown. | Show "Warning 2 of 3" with the refusal. |
| 13 | `agent/tasks.py` `_screen` uses `req.message` only | Attached files are read for the model in `_begin_composer_tools` but never screened. | Add the attached text (trimmed) to what is screened. |
| 14 | `agent/tasks.py` `_guard_model` (`max_tokens=24`) | The answer is free text and can be cut off before the label. | For local models, pass llama-server a grammar that only allows ALLOW, MINORS, MASS_HARM, MALWARE, SELF_HARM or ABUSE. Keep the parser for cloud models. |
| 15 | `chat/chat_guard.py` `screen` (`rule_hit`) | The rule checks the previous message joined to the current one; after a refusal the previous message is the refused one. | Skip the join when the previous message was itself refused (it is already counted). |
| 16 | `index.html` `renderGuardSettings` ("Relaxed limits" text) | The text says messages are no longer checked; `screen` still asks the model about every message. | Reword: "On: messages are only checked for sexual content involving minors." |
| 17 | `index.html` `guardSaveLimits` (`+$('gl-decay').value \|\| 0`) | An empty field becomes 0, which means never. | Treat an empty field as the default (20), or refuse to save it. |
| 18 | Sidebar chat list (`loadSidebar` in `index.html`) | The chat list does not include the lock state. | Send `locked` with each chat and show a lock icon. |
| 19 | `index.html` comment above the Music styles, and the `.unlock-modal` rules | Leftovers from the removed passphrase dialog. | Delete the `.unlock-modal` rules and move the comment to the `.lock-banner` rules. |
| 20 | `confirm(` throughout `index.html` | Native dialogs look different from the app and block the page. | One shared confirmation modal. |
| 21 | `font-size: 9px` to `11px` in `index.html` | Small text is hard to read. | Review each; 12 px minimum for readable text. |
| 22 | `catch (_) {}` in `index.html` | Failures are swallowed with no message. | Review each; show a toast where the user should know. |
| 23 | `ARCHITECTURE.md`, "Runtime facts" | Describes the developer's own machine and paths. | Update it, or label it as the developer's setup. |

## Not covered

- The interface was reviewed from its code, not by using the running app. Layout, contrast and on-screen behaviour were not checked.
- Areas outside the chat limits were only scanned for patterns (items 20 to 23): voice, music, images, labs, OSINT, Roblox, network and routines.
- Cloud models were not tested, and nor were the Ornith models listed in `models.ini`.
- Items 20, 21 and 22 are counts; each case still needs a look, since some may be intentional.
