# Agentic Workspace — Architecture & work log

Resume point for any session: read **Progress** first, then **Decisions**.

## Runtime facts (verified 2026-09-23)
- Host: Windows 10, Python 3.11 venv in `.venv`, FastAPI app `main:app` on 127.0.0.1:8000 (`run.bat`).
- Local models: llama-server **build 10621** (`E:\ClankerLocalCpp\llama-server.exe`), router mode
  (`--models-preset models.ini --models-max 1 --jinja`), port 8090 (`llama_local.LLAMA_SERVER_BASE`).
  Since 2026-09-24 `run.bat` prefers PrismML's llama.cpp fork in `E:\ClankerLocalCpp\prism\` when it is
  installed (needed for Ternary Bonsai 2), and no longer passes a global `--ctx-size` (D29).
  GPU: RTX 3060 Ti, 8 GB, driver 610.74.
- Tests: `.venv\Scripts\python.exe -m unittest <every test_*.py module> smoke_test` (no pytest).
  JS syntax: extract the last `<script>` of `index.html` and `node --check` it.
- `index.html` is the source; it is copied to `static/index.html` at startup (keep both in sync).
- Packages available without downloads: httpx, requests, websockets, pywinauto, comtypes, openai, litellm.
  Not available: git, playwright, mcp, bs4.

## Baseline (before this work)
- 2026-09-23: **365 tests, all passing**, 0 pre-existing failures (`test_* + smoke_test`).
- 2026-09-23 (resume of Phase 3.5, Linux cloud container, no Windows APIs): **520 tests: 510 pass,
  4 skipped, 6 fail** — all six need Windows and pass on the Windows host (520/520 recorded there
  after 3.4): `test_folder_rules` junction (runs `cmd /c mklink /J`), case-insensitive path and
  backslash-path replay (NTFS semantics), `test_architecture` eviction (Windows model paths in the
  temp models.ini), `test_productivity_tools` focus-before-screenshot (Windows window API), and
  `test_streaming` stop-before-first-token (closing a socket from another thread interrupts a blocked
  read on Windows, not on Linux). Container runs are judged as "no new failures beyond these six".

## Module responsibilities
| Module | Responsibility |
|---|---|
| `main.py` | Entrypoint (`uvicorn main:app`) — since Phase 2 everything else is in the packages below |
| `api/server.py` | FastAPI app, middleware, every route, static UI |
| `agent/` | `prompts`, `parsing` (tool-call parsing), `models` (local/cloud calls, streaming glue, protocol detection), `loop` (agent loop), `tasks` (turns, desktop routes, task queues, orchestrator instance) |
| `tools/` | `schemas` (tool schemas, per-turn authority, internal tools), `files` (undo journal, atomic write, secrets), `dispatch` (`execute_tool`), `runtime` (registry, executor, guarded/internal execution), `approvals` (views, broker, gate_batch) |
| `core/`, `events.py`, `routing/handoff.py` | config/settings, shared state, awareness glue; audit log + UI event stream; model registry + handoff |
| `orchestrator.py` | Run pipeline: prepare → plan → execute → review → bounded revisions; TaskState |
| `effort.py` | Effort levels as data, capability fallback, per-run budget (Phase 1.8) |
| `turns.py` / `run_control.py` | One background thread per turn; pause/resume/stop/deadline |
| `tool_registry.py` / `tool_executor.py` | Typed tool metadata (risk, retry, timeout, undoable) / validated, classified execution with retries, cooldowns, budget, undo journal |
| `permissions.py` | auto / ask / deny per tool (mode, category defaults, per-tool overrides) |
| `approval_view.py`, `approval_broker.py`, `approval.py` | What an approval shows; web card vs desktop popup; the Tk popup |
| `undo_store.py` | Journaled file changes, rollback, undo, retention (`data/undo/`) |
| `model_caps.py`, `llama_local.py`, `model_router.py` | Per-model detected capabilities; llama-server discovery/config; model registry + handoff |
| `llm_stream.py`, `stream_hub.py`, `llm_compat.py` | Streaming completions + UI relay; litellm/openai wrapper (cloud, tool-free calls) |
| `shell_lint.py`, `patch_edit.py` | POSIX-construct detection; multi-hunk patch engine |
| `replay_harness.py`, `recorder.py` | Replay recorded/reconstructed model replies through the real pipeline; record live runs |
| `intent_router.py`, `desktop_router.py`, `generic_actions.py`, `elements.py`, `desktop_input.py`, `vision.py`, `window_state.py` | Deterministic desktop routes, UI Automation, input, screenshots, non-disruptive inspection |
| `context.py`, `history.py`, `model_output.py` | Task board, action ledger, live machine state; history fitting; reasoning split |
| `fs_tools.py`, `sys_tools.py`, `web_tools.py`, `productivity_tools.py`, `media_session.py`, `messaging.py` | Tool implementations |
| `awareness.py`, `win_signals.py`, `system_metrics.py` | Proactive detectors, window/idle signals, CPU/RAM/VRAM |
| `roblox_bridge.py`, `roblox_places.py`, `roblox_plugin/` | Studio long-poll bridge, place discovery, Lua plugin |
| `app_registry.py` | Installed-app index, clipboard helpers |

## Request flow (current; module names after the Phase 2 split)
```
UI POST /api/chat (api/server.py) ─► TurnManager (turns.py, background thread) ─► AgentOrchestrator.run (orchestrator.py)
   analyze → choose_effort → PromptPreparer → Planner ─► set_context (task brief + saved memory)
   ─► execute = chat_with_agent (agent/tasks.py)
      ├─ deterministic routes: intent_router → desktop_router ─► approvals.gate_batch → approval
      │     → runtime.execute_internal("desktop_route") → ToolExecutor (same spec/journal/events)
      └─ run_agent_loop (agent/loop.py)
           llm.call_model → local (llm_stream, SSE) / cloud (llm_compat → litellm / openai client)
           parse_response (native tool_calls | schema | text recovery) → _invalid_call_reason
           permissions.explain(tool, settings, tainted, args) → auto | ask | deny
               ask → approvals.ask_batch_approval_os → approval_broker → web card | Tk popup
           runtime.execute_tool_guarded → ToolExecutor.execute (validation, budget, retries, cooldown,
               isolation, undo journal) → dispatch.execute_tool → result framed as untrusted data
   → Reviewer (deterministic / model / cross) → bounded revisions → result
UI: GET /api/stream (SSE: drafts, events) + polls GET /api/state; long-polls /api/turn/{id}.
```
(The flow as first found — everything in main.py — is kept in the Phase 2 notes, D14.)

## Failure points found (orientation)
1. Model replies are not streamed: truncation/runaway is only seen after the whole reply (`call_local`).
2. Tool-call constraint support is assumed, never detected (`call_local` always sends `tools`).
3. No undo: `write_file`, `edit_file`, `move_path`, `delete_path` change files irreversibly (`execute_tool`).
4. Approval arguments truncated to 300 chars in the Tk popup (`approval._format_args`); two wordings.
5. `execute_command` runs through `cmd.exe` (`_run_cancellable(shell=True)`); POSIX commands fail late.
6. `edit_file` is single find/replace, exact match only.
7. Run limits are scattered (`MAX_*` constants, `EffortProfile`, executor budget); no token/time usage tracking.
8. `main.py` is 3,944 lines holding tools, loop, API, routing, settings.
9. No persisted run logs, so there are no raw recordings of past failures (replay fixtures are reconstructed).
10. (found 3.5) `data/awareness.json` — the user's learned app habits — was committed to the repository
    (untracked in the 3.5 commit; it remains in earlier history, which may not be rewritten).
11. (found 3.5; → 3.7) Roblox: `roblox_read_script` results are cut to 6,000 characters with no
    notice, and the only write tool replaces the whole script — a model can silently delete the unseen rest.

## Phase plan
1. Reliability core — replay harness → tool-call constraints → undo trail → readable approvals →
   PowerShell tool + bash-ism lint → patch editing → streaming; execution discipline + effort runtime.
2. Split `main.py` (behaviour-preserving).
3. Capabilities — proactive observer, Roblox hardening + adapter interface, web/browser, MCP,
   projects + persistent chats, memory, per-folder permissions, routines.
4. Workspace UI — activity panel, sliders, audit fixes, theming, states, (ambient idle).
5. Benchmarks + routing (if time allows).

## Progress
- [x] Orientation: baseline recorded, flow mapped
- [x] Phase 1.1 replay harness + fixtures (`replay_harness.py`, `fixtures/replay/`, `test_replay.py`);
      loop detection rebuilt (per-call signature + machine-change epoch) — `loop_repetition` fixture
- [x] Phase 1.2 constrained tool calls (`model_caps.py`, `_complete_with_protocol`, `test_model_caps.py`)
- [x] Phase 1.3 undo trail (`undo_store.py`, executor journal hook, `fs_tools` journals, `/api/undo*`,
      Activity "Undo" + "Undo last change", `test_undo.py`)
- [x] Phase 1.4 readable approvals (`approval_view.py` shared view + wording, popup/card rebuilt,
      Enter guard, Stop closes the popup, `test_approvals.py`); safety: `open_url` web-only,
      `get_env_var` hides secrets
- [x] Phase 1.5 PowerShell tool + POSIX lint (`run_powershell`, `shell_lint.py`, pre-approval check in
      `_invalid_call_reason`, prompts updated, `test_shell_lint.py`, bashisms fixture; Snake fixture passes)
- [x] Phase 1.6 patch editing (`patch_edit.py`, `apply_patch` tool, dry-run diff in approvals,
      all-or-nothing + journaled, `test_patch_edit.py`)
- [x] Phase 1.7 streaming (`llm_stream.py`, `stream_hub.py`, `GET /api/stream`, chat draft + live
      Activity row in `index.html`; simulated reveal removed; verified against the real llama-server:
      tool-call fragments, reasoning channel, Stop in 0.8 s; `test_streaming.py`, fragmented/runaway fixtures)
- [x] Phase 1.8 execution discipline + effort runtime (`effort.py`: 6 levels, capability fallback,
      per-model overrides, RunBudget, safe-boundary change, `/api/effort*`; `recorder.py`;
      `test_effort.py`, step-budget + mid-run-change fixtures)
- [x] Phase 1 review (reviewer subagent, 2026-09-23) — 3 high + 11 medium findings fixed (see
      "Phase 1 review fixes"); 463/463 tests. Carried forward: desktop_router side channel (→ Phase 2),
      MockMessagingProvider in shipped code (→ Phase 2), content-is-data wrapping (→ Phase 3),
      undo history beyond the current run in the UI (→ Phase 4).
- [x] Phase 2 split main.py (see D14): main.py 4,500 → 83 lines (entrypoint); 16 modules; tests retargeted;
      464/464; real startup verified (`uvicorn main:app`: 85 tools, no errors). Carried items done:
      desktop routes through the one tool path (`desktop_route` internal tool), mock messaging provider
      moved to `testing_fakes.py`.
- [x] Phase 2 review (reviewer subagent, 2026-09-24): split verified (every definition moved once,
      identical bodies, identical route table/OpenAPI/middleware, no scope/import/path bugs). Fixed its
      findings: `desktop_route` behaviour restored (route result/message and exceptions pass through,
      no model budget, not counted as a model change → no extra reviewer model call), shared
      `runtime._finish` instead of duplicated bookkeeping, Activity label "Open <app>", unused
      `messaging.py` imports removed, stale `main.py` references fixed, D14 corrected;
      `test_desktop_route.py` covers the real path.
- [x] Phase 3 capabilities
  - [x] 3.1 content is data (D15): `untrusted.py`; every non-bookkeeping tool result and the window-title
        observation framed; embedded instructions → `untrusted` event (Activity + chat steps) and a
        request-wide taint: nothing but local reads auto-runs afterwards, approval cards carry the warning
        (web card + desktop popup); rule in both system prompts; `test_untrusted.py` + replay fixture
        `injected_instructions_in_file`; 475/475
  - [x] 3.2 per-folder permissions (D16): `folder_rules` setting (default empty, old settings load
        unchanged), `permissions.explain/decide(..., args=)`, `POST /api/permissions/folders`,
        Settings > Permissions > Folders; `test_folder_rules.py` (incl. junction escape, loop replay:
        no card + still journaled); 488/488
  - [x] 3.3 memory (D17): `memory.py` store (`data/memory.json`), per-request selection sized by effort,
        brief block + `memory` Activity event, `/api/memory*`, Settings > Memory, `save_memory` tool
        (asks by default, journaled/undoable), `memory` turn mode ("remember …" offers only
        save_memory); `test_memory.py`; 506/506. UI edits moved to the served root `index.html`
        (they had gone to the `static/` copy, which the server overwrites at start).
  - [x] 3.4 Prompt Prep (D18): `prompt_prep.py` (Off / Clarify / Expand / Plan first; session > project >
        model > setting; migration from `prompt_enhancement`), `PrepGate` card (web card, Ctrl+Enter / Esc,
        Stop ends the wait, the wait doesn't use the time limit), one-question rule in the loop,
        `brief_sent` Activity row with the exact brief, `/api/prep*`, quick menu + tooltip;
        `test_prompt_prep.py`; 520/520
  - [x] 3.5 projects + persistent chats (D20): `projects.py`, `chat_store.py`, `agent/sessions.py` wired in —
        every turn saved (`tasks._run_turn`), last chat/project reopened at startup, effort-sized project
        context in the brief (`tasks._project_brief`, Activity `context` row, file text framed + taints),
        `/api/chats*`, `/api/projects*`, `/api/search`, sidebar Chats/Projects tabs + search + project
        editor; `test_projects.py` (18); 538 run, only the 6 Windows-only failures. UI checked in Chromium
        (Terminal, Light, Eclipse); restart → same chat reopened, verified against the real server.
  - [x] 3.6 routines (D21): `routines.py` store (`data/routines.json`), `agent/routine_runner.py` (every step
        through permissions.explain → approval card → guarded executor; desktop routes through
        `run_desktop_intent`), "Save as routine" offered under a reply that took actions, sidebar Routines tab
        (one-click Run, editor: rename / drop steps / schedule), `/api/routines*`; `schedule_task` gained
        `repeat` (daily / weekdays / weekly) and writes reminders to a `.ps1` (bug fix, see D21);
        `test_routines.py` (18); 556 run, only the 6 Windows-only failures
  - [x] 3.7 Roblox hardening + app-adapter interface (D22): `app_registry.AppAdapter` (+ registry, generic
        `/api/apps`, `/api/apps/{id}/check`); `roblox_adapter.py`; bridge protocol v3 (sessions per plugin copy,
        command deadlines, Stop, late-result drop, focus routing, health ping); plugin v3 (raw source,
        compare-and-set writes, place tree, bounded playtests, stack traces, Output backfill, error retry);
        new tools `roblox_get_tree`, `roblox_edit_script`, `roblox_run_playtest`; script edits in the undo trail
        (`undo_store` "external" ops); approval diffs + changed-after-approval check for scripts; data-loss fix
        (reads say what they didn't show); UI health/check; `testing_fakes.FakeStudio`; `test_roblox.py` (24,
        incl. the end-to-end replay); 580 run, only the 6 Windows-only failures. Plugin not run in Studio here
        (Limitations); Luau syntax-checked with luaparse.
  - [x] 3.8 web + browser (D23): `web_search` (DuckDuckGo HTML, keyless; or the user's SearXNG), `web_read`
        (readable text + numbered links, windows that name what they skipped, public addresses only, redirects
        re-checked), `browser.py` (the assistant's own Edge/Chrome over DevTools with a separate profile:
        `browser_open/read/click/fill`); fills and irreversible clicks always ask (`permissions.ALWAYS_ASK`,
        `ALWAYS_ASK_WHEN`); passwords/payment/ID fields and card/SSN-like values refused; approval card shows the
        control and page; search/look-up requests are read turns; prompts ask for [title](url) citations;
        `new_browser_tab` refuses non-web URLs (fix); Settings > Agent > Web; `test_web_browser.py` (18, incl. the
        real Chromium path); 598 run, only the 6 Windows-only failures
  - [x] 3.9 MCP client + tab (D24): `mcp_client.py` (JSON-RPC over stdio and streamable HTTP; initialize,
        tools/list with paging, tools/call, resources, list_changed, cancel on Stop/timeout, server log),
        `data/mcp_servers.json` (secrets write-only), MCP tools registered as `mcp__<server>__<tool>` in the one
        registry (`runtime.sync_mcp_tools`) and offered in action turns, destructive tier (ask), "Not undoable",
        untrusted framing, `mcp_read_resource`; `/api/mcp*`; Settings > MCP (status, tools, resources, last
        error, log, connect/disconnect/retry/edit/remove); `test_mcp.py` (13, fake stdio server + HTTP mock);
        611 run, only the 6 Windows-only failures
  - [x] 3.10 proactive observer (D25): off really stops sampling and drops what was held; quiet hours hold
        suggestions; "Don't suggest this again" mutes that exact suggestion (persisted; Settings lists and
        unmutes); dismissals/snoozes persist; feedback spaces suggestions out instead of silently muting;
        clipboard/habits can't be forced on; habits never save window-title fragments; every suggestion has a
        one-line `reason`; SIGNALS catalogue (Settings > "What is read"); new detectors repeated_actions,
        idle_after_error, project_opened, studio_project, routine_time (+ routine run history); activity summary
        for local models only; accept "project"/"routine" kinds hand off to the normal UI paths;
        `test_observer.py` (17); the suite now provably leaves `data/` untouched (runner check; harness sandboxes
        the session file); 628 run, only the 6 Windows-only failures
  - [x] 3.11 exit tests + Phase 3 review (reviewer subagent, 2026-09-24): `test_no_bypass.py` checks every
        registered tool against every non-permission setting, undo honesty, taint, always-ask rules and a full
        "everything turned up" replay. The review found 2 high, 9 medium and 6 low gaps - all fixed (D26), each
        with a test; 658 run, only the 6 Windows-only failures, `data/` untouched.
- [x] Phase 4 workspace UI (D27)
  - [x] 4.1 run history + links: every finished turn saved (`run_log.py`, `data/runs/`, newest 300 / 30 days,
        `tasks._save_run`), `/api/runs?q=`, `/api/runs/{id}`; tool events carry `plan_step`, `why` (the model's
        justification), `thought_id` (the note/reasoning of the same reply) or `routine_step`; thought events keep
        `reasoning_full` (only when the model emitted reasoning); `/api/state.usage` = the running run against its
        effort limits; `/api/effort.unsupported` says why a level isn't available; `test_run_history.py`,
        `test_run_log.py`
  - [x] 4.2 Activity panel: live usage meters (steps / tool calls / tokens / time), "Why?" on each step (plan step,
        stated reason, its note + "Show its reasoning"), "Raw reasoning" section, Past runs (search, open a run in
        the feed, "Back to live"; a live run never pulls you out), Undo history drawer (every journaled change with
        its own Undo, retention stated, "not undoable" kinds named)
  - [x] 4.3 Effort slider (role=slider, 6 levels, arrow/Page/Home/End, drag; levels the model can't run hatched,
        skipped and explained; fallback and source shown; limits of the level; "Only for this session"; a
        project's level is overridden for the session only - and said so) + a lighter Prompt Prep slider; ⚡ button
        shows the applied level with pips in the effort color
  - [x] 4.4 effort as theme tokens (`--effort-*` from each theme's colors, `data-effort`); reduced motion covers
        the new motion; audit fixes: text contrast ≥ 4.5:1 in every look × theme (`--muted`, new `--accent-text`;
        placeholder, hints, log times moved off `--faint`, which is ≥ 3:1 for non-text), ⓘ hit area 24 px, "Show
        activity" → "Show run steps in chat", General folded into Appearance, `[hidden]` always hides (fixed
        "Undo last change" showing with nothing to undo), suggestions never move keyboard focus, error states for
        the model list and the permissions tool list
  - [x] 4.5 walkthrough `test_walkthrough.py`: new project (High) → Roblox task → approval card (diff, "Can be
        undone") → effort raised to Max mid-run through the API → run found in past runs → undo → restart →
        project and chat reopen; UI checked in Chromium (Light, Dark/Terminal): past runs, Why?, raw reasoning,
        usage, undo drawer, sliders by keyboard, no failed API calls; 670 run, only the 6 Windows-only failures
  - [x] 4.6 Phase 4 review (reviewer subagent, 2026-09-24): 2 high, 4 medium, 10 low - all fixed, with tests in
        `test_run_history.py` (ReviewFixTests): project snippets never read secret files and mask secret values
        (a key file's content used to reach the brief - also to cloud models); saved runs keep no brief text and
        no memory text and mask every string (`secret_mask.py`, the one rule, shared with the loop); clearing the
        session override works (null accepted); the effort answer follows the UI's mode and pinned model; a past
        run's Undo shows what the undo trail says now; runs can be deleted (per run, all, with their chat) and the
        Privacy tab says what is kept; text colors are fitted at runtime to 4.5:1 for any theme and accent
        (`fitTextColors`: accent, warn, danger); ⓘ target a 24 px square; honest labels (note vs reasoning, raw
        reasoning, mid-run effort change); the reason and plan step are searchable; the drawer refreshes when a
        turn ends; undo drawer paging; error states for the model list, messaging and effort; no sideways scroll
        at phone width; 671 run, only the 6 Windows-only failures
- [x] Phase 5 benchmarks + routing (D28)
  - [x] 5.1 per-model benchmark: `benchmark.py` (6 tool-call cases against real schemas, latency, tokens/s, usable
        context by needle probes at 50% / 90% of the window, capped at 32k tokens) run by
        `agent/benchmark_runner.py` through the agent's own request path (`_complete_with_protocol`, streaming
        client, `_unwrap_schema_reply`, `parse_response`); results in `data/benchmarks.json` and merged into
        `model_caps`; `/api/benchmarks` (GET / POST / cancel); Settings > Models: per-model results, each case,
        Run / Run again / Cancel, progress; Activity events `benchmark_started/done/failed`
  - [x] 5.2 routing on measurements: registry entries carry validity, latency, speed and usable context; strengths
        only from measurements (the name-based `_strengths` is gone); a local model with < 50% valid tool calls is
        not a handoff target; usable context wins over the configured window; `request_model_handoff` lists the
        models it can target (enum) with what was measured; effort levels read the measured validity
        (`effort.supported_levels`); `test_benchmark.py` (12); 683 run, only the 6 Windows-only failures
  - [x] 5.3 Phase 5 review (reviewer subagent with a fake router-mode llama-server, 2026-09-24): 1 high, 10 medium,
        4 low - all fixed: usable context probed inside the window `/props` reports (not the ini value), with the
        server's real prompt token count, never past the window, a refused over-long probe retried smaller, a
        cap-limited result stored as a lower bound (`usable_context_min`) that never overrides the window, equal
        sizes probed once; cases use the agent's own schemas, system prompt, tool list and reply budget plus the
        turn's cut-off retry, and cut-off replies are reported apart (validity over finished replies, None when
        none finished); Cancel reaches the request in flight (`_local_complete(cancel=)`) and nothing is kept
        after it; loading for a benchmark is not a model switch and leaves the cloud label alone; the tool list
        is sized for the pinned model; busy messages name the benchmark, a scheduled routine due meanwhile is
        recorded as skipped, a suggestion is not consumed while busy; measurements are replaced as a set
        (`model_caps.set_measurements`), results from an older build are marked and unused; Windows-safe writes;
        the handoff enum is the router's own target list (mode, reliability, VRAM, safe ids); vision only from a
        projector (models.ini, the router's command line or `/props`), cloud facts shown as configured, the
        name-based reasoning guess gone, automatic picks pass over unreliable models; tokens/s from llama-server's
        own timings; the Models tab updates only the benchmark block while polling (edits, focus and open lists
        stay), resumes polling after a reload, shows load errors, disables loading during a run, announces
        progress in a stable live region, and a benchmark starts its own Activity feed. Checked end to end against
        the fake server (an always-reasoning model now scores 5/6 instead of 0/6; a 13-tokens-per-sentence model's
        over-long probe is retried and recorded at its real 27,920 tokens). `test_benchmark.py` (20); 691 run, only
        the 6 Windows-only failures

## Decisions
- **D1 — No git (superseded by D19).** git was not installed on the Windows host, so Phases 1–3.4 were
  recorded in the **Change log** below instead of commits.
- **D19 — Git from Phase 3.5 on.** Work continues in a clone of the GitHub repository (branch
  `claude/festive-maxwell-pjrvb6`), one commit per coherent step. The brief's "never push" was written
  for a local-only repo; this clone lives in a disposable cloud container, so each commit is pushed to
  that one feature branch (never `main`, no new remotes, no history rewrites) — otherwise the work
  would be lost with the container. The repo had no ignore file: `.gitignore` now covers
  `keys.local.bat` (it held only the placeholders run.bat writes; it is untracked now and run.bat
  recreates it when missing), `.env*`, key/cert files, `.venv/`, `__pycache__/` (97 committed `.pyc`
  files untracked) and `data/` (runtime data; the three files committed earlier stay tracked).
  Commands in this container run on Linux; code and the commands the assistant runs stay Windows-only
  (non-negotiable 6), and Windows-only tests are judged against the Windows baseline above.
- **D2 — File names.** The brief's `claude_awareness.py`, `claude_win_signals.py`,
  `claude_roblox_bridge.py`, `claude_roblox_places.py`, `keys_local.bat`, `_gitignore` are
  `awareness.py`, `win_signals.py`, `roblox_bridge.py`, `roblox_places.py`, `keys.local.bat`, `.gitignore`.
- **D3 — Replay fixtures are reconstructed.** No run logs are persisted anywhere, so the Snake-game
  fixture is rebuilt from the user's two approval screenshots plus the known code path, and marked
  `"provenance": "reconstructed"` with its evidence. A recorder (off by default) now writes real
  recordings for future fixtures.

- **D4 — Tool-call constraints.** llama-server b10621 started with `--jinja` already applies a grammar
  to tool calls (the template's native format, or its generic JSON format). The app therefore sends
  `tools` and *detects* which case applies from `/props?model=` (`chat_template_caps`) — only for a
  model that is already loaded, so detection never triggers a model load. Only if the server refuses
  `tools` does the app constrain the whole reply itself (`response_format` json_schema: one tool call
  or an answer); if that is refused too it parses leniently. A refusal observed at runtime outranks
  `/props`; a new server build resets a model's record. Stored in `data/model_caps.json`.
- **D5 — Loop detection.** An identical call (same tool, same args minus justification) is not re-run
  while nothing on the machine has changed since it ran (a successful mutating tool bumps an epoch).
  Reads of changing state (screen, windows, processes) may repeat 3×. Three replies in a row that are
  only repeats end the run with a closing summary.

- **D6 — Undo is a journal, not a snapshot.** The ToolExecutor opens a `Change` for every tool whose
  registry spec is `undoable`; the tool records what it is about to do (`backup_file`, `trash`,
  `record_move`, `created_dir`, `created_tree`). Deletes and overwritten destinations are *moved* into
  the backup area (no copy cost, fully recoverable). Success → commit (with fingerprints of the result
  so a later undo detects "changed since" and refuses unless forced); failure/cancel/exception →
  immediate rollback, so nothing is half-applied. One journaled tool at a time (lock), undo takes the
  same lock. **Backups:** `data/undo/<change_id>/`, manifest `data/undo/manifest.json`.
  **Retention:** newest 300 changes, 14 days, 2 GB — oldest pruned first (permanent, like emptying a
  recycle bin). No setting can turn the journal off.
- **D7 — Approvals.** `approval_view.describe()` is the single source for what an approval shows:
  friendly name, every argument in full (the card/popup scroll, nothing is cut; `force` is shown),
  a preview from the real file (unified diff / what a delete removes / what an overwrite replaces),
  and "Can be undone" / "Not undoable" from the registry. One wording: "Allow" / "Deny",
  "Enter allows · Esc denies". Enter is ignored for 700 ms after a card/popup appears (a keystroke
  already in flight must not approve); Enter presses the focused button. Stop now closes the desktop
  popup (it used to wait up to 10 minutes), and a dead GUI thread is detected.

- **D8 — Windows commands.** `run_powershell` (Windows PowerShell 5.1, `-NoProfile -NonInteractive
  -EncodedCommand`, UTF-8 output, 5–300 s timeout, Stop kills the process tree) is the preferred shell;
  `execute_command` (cmd.exe) stays for compatibility. `shell_lint.lint()` runs in the pre-approval
  validation: POSIX-only constructs (&&/|| on PS 5.1, rm -rf, printf, export, heredocs, touch, ls -la,
  mkdir -p, grep/sed/awk, which, sudo, /dev/null, /tmp paths, VAR=x cmd, shebangs) never reach
  approval; the model gets a Windows-correct hint. Quoted text is ignored, so it does not false-positive
  on strings. Both shells are "Not undoable"; the intent guard now covers all three shells.
- **D9 — Patch editing.** `apply_patch` hunks are `{find, replace, line?, before?, after?}` (search/replace
  hunks are what local models produce most reliably; a unified-diff input format was not added).
  Matching: exact → whitespace-insensitive whole lines → fuzzy (difflib ≥ 0.86, must beat the runner-up
  by 0.03). Ambiguity is an error with instructions, never a guess. CRLF preserved; lost indentation is
  restored line by line. `edit_file` is kept unchanged for compatibility.

- **D10 — Streaming.** Local calls stream over SSE (`llm_stream.complete`, httpx, injectable transport).
  Fragments are relayed through `stream_hub` → `GET /api/stream` (SSE, merged fragments, 15 s
  keep-alive) → the chat draft (answer text only) and the Activity live row (reasoning *only* when the
  model emits a reasoning channel or inline `<think>`, and the tool call being written). Early stops:
  Stop closes the HTTP stream (llama-server aborts generation); a tool call > 60,000 chars ("runaway")
  or output that becomes periodic (period 4–64, ≥ 3 distinct chars, 384 chars) ("repetition") is cut
  off and answered as an unreadable call. The old after-the-fact word reveal was removed; new words
  fade in as they actually arrive (arrival time per word + negative animation delay). Cloud calls are
  not streamed (litellm path unchanged) and simply appear.
- **D11 — Effort levels.** Ids `low normal high extra max ultra`; labels Quick / Balanced / Thorough /
  Reviewed / Cross-checked / Workflow. The brief names the levels Low/Normal/High/Extra/Max/UltraCode;
  the user earlier asked that no level be labelled "Normal", so ids follow the brief and labels stay
  use-case names (the brief's name is shown as the level id). Stored values low/normal/high/max load
  unchanged. Resolution: session override > project > per-model (`effort_by_model`) > setting; then
  auto-escalation (normal→high for complex actions); then capability fallback to the nearest lower
  supported level (context window, tool protocol, measured validity once Phase 5 exists) with an
  Activity event. Per-model `effort_overrides` can tune numeric limits only (clamped). A change during a
  run is queued and applied between steps (`_apply_effort_change`): limits, executor tool budget and
  the time deadline move; usage so far is kept. Run budget: steps, tokens, time, tool calls — reaching
  the step or token budget ends the run with a summary (`budget_reached`). Revisions share the run's
  budget (they used to get fresh iterations).
- **D12 — Real context windows.** Verified on b10621: the router's global `--ctx-size 65536` overrides a
  preset's `ctx-size` (models.ini says 250000 for two models; they run with 65536). `/v1/models` reports
  every model's real command line, so `llama_local.model_context_limit` now trusts that first.
- **D13 — Recorder.** `record_model_io` (default off) writes each local orchestrated run to
  `data/recordings/*.json` in the fixture format (`provenance: recorded`, raw replies incl. cut-off
  ones and retries, approval answers). The Settings toggle arrives with Phase 4.
- **Bug fixes logged (Phase 1):** `orchestrator.run` converted `max_revisions` with `int()` after
  `run_control.begin()` outside the try, so a bad stored value wedged the run controller → validated
  before `begin`. `open_url` (runs without approval) passed `file://`/custom schemes to `os.startfile`
  → web URLs only. `get_env_var` (runs without approval) returned secrets → values of secret-looking
  names hidden. Desktop approval popup hid `force` and truncated args → full view. Replay tests reached
  the live llama-server when it was running → network sealed in the harness.

- **Phase 1 review fixes (2026-09-24).**
  - *Undo:* the change just committed is never pruned (a single change larger than 2 GB used to
    destroy its own backup); sizes recorded per change; appends never move the original; copy/move
    journal a partial destination so a half-done copy is rolled back; `screenshot_to_file` always names
    its file so it is really undoable. D6 correction: a delete on another drive than `data/undo` is a
    copy + remove (not free).
  - *Replay isolation:* replays used the real undo store and wrote 22 test records to
    `data/undo/manifest.json` (renamed to `manifest.replay-leftovers.json`; deleting it was refused as
    irreversible). The harness now uses a sandbox store; a test guards the real one.
  - *Side channels:* `open_application`'s last resort ran any unmatched text through `cmd /c`
    (auto-approved) → now only a bare program name found on PATH, started without a shell.
    `run_sequence` refuses if any step's tool is set to Deny. `download_file` runs inline, checks Stop
    and a 300 s limit between chunks (no rollback racing a writer thread).
  - *Approvals:* preview capped at 200k chars (arguments stay complete), binary/huge files described,
    CRLF-aware `edit_file` + preview; a file changed after its preview was approved is not written.
    D7 correction: Enter presses the focused button; focus on the card text does nothing.
  - *Lint:* verified on PowerShell 5.1 — `mkdir -p` and `rm -r` work, `rm -rf` / `rm -f` fail; heredoc
    and /tmp rules now ignore quoted text (D8 correction).
  - *Patch:* trailing-newline finds no longer add a blank line; mixed-ending files stay as they are
    (D9 correction); bad `line` hints are ignored.
  - *Streaming:* Stop closes the connection even before the first token (watcher thread); unnamed
    calls and string error chunks are surfaced; loop detection is conservative (4,096 chars of one
    unit with ≥ 6 distinct chars incl. ≥ 3 letters, checked every 256 chars — arrays, tile maps and
    tables are not loops); SSE ids for resume. Transient connection errors before any output are
    retried twice with backoff.
  - *Budget:* the token budget counts generated tokens + prompt tokens new since the previous step
    (the repeated ~9k-token system prompt/tool list is served from cache) — D11 correction.
  - *Other:* `read_file` refuses secret files (keys*.bat, .env, keys/certs); unknown tool names are
    answered instead of dropped; an empty final reply is reported honestly; the executor's budget and
    failure memory reset at run end; recorder keeps tool results, permission settings and Stop points
    (D13 correction); fixture metadata no longer names models that are not evidenced (`unrecorded`),
    and the runaway fixture is labelled illustrative.

- **D14 — The split (Phase 2).** Done by a script (kept out of the repo), not by hand: every top-level
  statement of main.py moved, in its original order and with the comments above it, to one module;
  every reference to a name that now lives in another module was rewritten `alias.name` with
  scope-aware analysis (parameters/locals/comprehension variables untouched) and checked against the
  exact source position. Layout vs the brief's suggestion: `core/{config,state,observer}.py`,
  `events.py`, `agent/{prompts,parsing,models,loop,tasks}.py`, `tools/{schemas,files,dispatch,
  runtime,approvals}.py`, `routing/handoff.py`, `api/server.py`. `effort.py`, `llm_stream.py`,
  `stream_hub.py` stayed top-level modules (already standalone and imported by tests by those names;
  moving them into `agent/` would only rename imports). Cross-module calls go through the module
  (`llm.call_model()`), so a function is patched where it is defined — except values captured at
  import time, which need their holder patched too: `runtime.tool_exec` holds `files.undo` and
  `run_control.is_stopped` (the replay harness patches `files.undo` *and* `runtime.tool_exec.undo`),
  `handoff.router` holds `_load_model`/`emit_activity`, `approvals.broker` holds `approval.ask`.
  The import cycle spans `agent.loop`, `agent.tasks`, `routing.handoff`, `tools.approvals`,
  `tools.dispatch`, `tools.runtime`; it is safe because each imports cleanly first and
  `agent/tasks.py` builds the orchestrator with call-time lookups only. Every module gained
  `from __future__ import annotations` (harmless); import-time side effects happen once each but in
  a different order (FastAPI app last; awareness engine before the undo store/`approval.start()`).
  Aliases: config, shared (state), observer, event_log, prompts, parsing, llm (models), agent_loop,
  tasks, schemas, files, dispatch, runtime, approvals, handoff, server.
  Functional changes (each a Phase-1-review item): the mock messaging provider moved to test-only
  `testing_fakes.py`; deterministic desktop routes run as the internal tool `desktop_route` through
  the registry/executor/event stream (they already used the approval path). The Phase 2 review found
  that this first version also changed behaviour; corrected so the route's caller sees exactly what
  it saw before — the route's own result dict (so the caller's fallback messages apply), and an
  exception from the route still propagates as "Turn failed" instead of being reported as success;
  internal calls don't use the model's tool budget and are marked `internal` in the executor
  history, so the reviewer at Balanced effort stays deterministic after a route (no extra model
  call). Kept on purpose: a Stop/Pause checkpoint before each route (Stop works between steps), and
  an Activity row "Open <app>" per route. `test_task_flow.py` reads function bodies from the new
  files with stand-ins; the real `execute_internal` + `run_desktop_intent` path is covered by
  `test_desktop_route.py`.

- **D15 — Content is data (non-negotiable 8).** Three layers, because detection alone is a heuristic:
  (1) *framing* — every tool result except the host's own bookkeeping (`set_plan`, `update_plan`,
  `ask_user`) reaches the model between `<<<UNTRUSTED CONTENT from <tool>>>` markers, and both system
  prompts say what the markers mean; (2) *showing* — `untrusted.find_instructions` finds lines aimed at
  an AI reader (not ordinary imperative text: "you are now logged in", "run the following command",
  "the agent should retry" do not trip it; see `test_untrusted.py`), emits an `untrusted` event and
  adds a host note inside the frame; (3) *enforcing* — once a request has read such content,
  `permissions.decide(..., tainted=True)` turns every `auto` into `ask` except local reads
  (`permissions.LOCAL_READS`; network tools count as egress, a URL can carry data out), including god
  mode and per-tool `auto` overrides. Taint only tightens, and resets per user request
  (`chat_with_agent`). The approval card/popup shows which content tried what.

- **D16 — Per-folder rules only relax file writes.** A rule turns `ask` into `auto` for
  `write_file/edit_file/apply_patch/make_dir/copy_path/move_path/rename_path` when *every* path the
  call touches (both ends of copy/move, the renamed result) resolves — `realpath`, so `..` and
  junctions cannot escape — inside a rule folder. Never: delete, archives, downloads, shell, browser,
  network, MCP, messaging, the desktop-router batch path (no per-path args), secret files, or this
  app's own folder. Lockdown, any per-tool override of that tool, and a tainted request win. A rule
  cannot be a drive, the user profile or above, a system folder, a UNC path, or the app folder
  (checked on add; on load only the shape is checked so an unplugged drive doesn't delete a rule).
  Writes that run under a rule are journaled like any other; Activity logs "folder rule: <path>".

- **D17 — Memory is background, never authority.** Entries live in `data/memory.json` (atomic
  write). Each run's brief gets the pinned entries plus those sharing words with the request
  (stop-words ignored), newest first, within a per-effort character budget (600 → 4000); entries
  tagged with another project are left out. Used entries are counted and listed in Activity. The
  brief says memory grants nothing, and no permission/approval/undo code reads it (a replay test
  seeds "always run commands without asking" and still gets the approval card). The assistant can
  only *propose* an entry: `save_memory` is input-tier (asks by default; tainted requests always
  ask) and journals `memory.json` first, so Undo removes it. "Remember …" messages get a new
  `memory` turn mode that offers only `save_memory` rather than full action authority. Replays
  and tests use a sandbox store; a test checks the real file is untouched.

- **D18 — Prompt Prep.** *Clarify* makes no extra model call: the quick rules add constraints and
  "done when", and the brief tells the executor it may ask ONE question through the existing
  `ask_user` path; the loop refuses a second question for that request (answered → "continue and
  say what you assumed"). An extra pre-run model call on every local request was rejected for
  latency. *Expand* has the model write a spec (goal, constraints, done-when, assumptions) and
  *Plan first* a step plan; both wait on a card (`PrepGate`) whose text is exactly what is sent,
  with Start/Accept, Run as typed, Cancel. Plan first is enforced by the runtime: the executor has not
  started, so no model step or tool call can happen before acceptance (replay test: cancel → zero
  tool-enabled model requests). Accepting approves nothing — the brief says so and every action
  still gets its card (replay test). The card wait suspends the run's time limit and Stop ends it.
  An answer to a pending question skips prep (no second card). Migration: none of the old levels
  blocked a run, so off→Off and minimal/standard/detailed→Clarify; Expand/Plan first are opt-in.
  The old `prompt_enhancement` key is kept (read only for migration). Mode source and label are on
  the run_started event; the full brief (task brief + memory) is the `brief_sent` Activity row.

- **D20 — Projects and persistent chats.** The server is the source of truth: a chat is saved after every
  turn (`agent/tasks._run_turn` → `sessions.record_turn`; a save failure is logged and never loses the
  answer), and `data/session.json` reopens the last chat and project at startup (the model gets the saved
  conversation back, minus screenshots). The browser keeps only a copy of the open chat for an instant
  render; `syncSession` follows the server. Switching chat/project is refused while a turn runs (it would
  swap the conversation under it). Deleting moves files to a `trash/` folder; chats of a deleted project
  are listed under "no project", never hidden. *Context assembly* (`projects.build_context`, called from
  `tasks._project_brief` for every run): project instructions (≤ ⅓ of the budget), notes (≤ ⅕), the folder's
  file index (tree, languages, README), up to three windows of lines matching the request's words, and a
  one-line summary of the project's three latest other chats — never whole files or everything. Budget in
  characters = the effort profile's `context_tokens` / 4, capped at 16,000 (Quick 1,500 → Cross-checked
  16,000; a per-model override of `context_tokens` moves it). A full Windows path in a request gets the same
  index on demand, cached in `data/index/` (5-minute TTL). README and file text are framed as untrusted
  data; instructions found in them are shown (`untrusted` row) and taint the request (`brief_untrusted` seeds
  `untrusted_seen`, which `chat_with_agent` used to reset). An Activity `context` row lists what was
  included and the budget. Non-negotiable 7: a project supplies effort/Prompt Prep defaults and context only
  (replay tests: "approve everything" instructions + Cross-checked effort still get the write card; a
  project file's injected text makes a normally auto `open_url` ask).

- **D21 — Routines repeat calls, not prompts.** A routine is the ordered list of tool calls from a finished,
  successful run that changed something (model-called tools and deterministic desktop routes; reads, plan
  bookkeeping, questions, `save_memory` and screen-coordinate clicks are dropped — they only informed the
  model or depend on what was on screen). A re-run needs no model and does the same thing again; the user
  can rename a routine and drop steps, never add or edit one, so a routine only ever repeats calls that
  already ran. Re-runs (clicked or scheduled) are turns on the TurnManager — one at a time, Stop/Pause
  between steps — and each step goes through `permissions.explain` and, when that says ask, the same approval
  card with "Routine 'X' · step n of m"; Deny is honoured; a declined or failed step stops the rest (done
  steps stay done; each file change is its own undo entry). The executor's duplicate guard (meant to stop a
  model redoing work within 120 s) is cleared at the start of a routine run — the user asked again.
  *Scheduling* uses the existing `schedule_task` tool through its normal approval card: Task Scheduler runs
  `powershell … Invoke-RestMethod -Method Post http://127.0.0.1:8000/api/routines/<id>/run?trigger=schedule`
  (port from `AGENTIC_PORT`, default 8000 as in run.bat). The app must be running; a scheduled run that
  finds another task running is recorded as "skipped" and shown as a note. A local process could POST the
  same URL — it gains nothing a click would not: every step still asks. A scheduled routine can't be
  deleted until its schedule is removed (no orphaned Task Scheduler entries).
  *Bug fixes in `schedule_task`* (touched for routines): a reminder's PowerShell one-liner was longer than the
  261 characters schtasks accepts for `/TR`, and a `"` in the message broke its quoting — reminders now run
  a script written to `data/scheduled/<name>.ps1` with `-File` (removed on cancel); a command over 261
  characters is refused with a hint instead of failing inside schtasks. Unverified on Windows here (no
  schtasks in the container; covered with a mocked `_run`). Not changed: `/sd` for one-off tasks is written
  as MM/DD/YYYY, which schtasks may read in the system locale's date order — repeating schedules don't pass
  `/sd` at all (Limitations).

- **D22 — One adapter interface for creative apps; Roblox first.** `app_registry.AppAdapter` is what an app
  integration implements: `status`, `health_check`, `call(tool, args, journal, cancelled)`, and for things
  that live inside the app `preview` (approval diff + fingerprint), `fingerprint` and `restore` (undo).
  Adapters supply app knowledge only — their tools are declared like any other (schemas, registry policy,
  permission tier) and `tools/dispatch.py` hands a call to `adapter_for_tool(name)` inside the one path
  (permissions → approval card → ToolExecutor with its undo journal and events). Blender and Figma have no
  adapter, so no tool, status row or button exists for them; how to add one is in "Next steps".
  *Undo for artifacts inside an app:* `undo_store` gained an `external` op — the old content is saved in the
  change's backup folder and the adapter restores it. Commit records the fingerprint the tool meant to
  write; undo refuses (unless forced) if the artifact changed since, and a *rollback* of a failed call
  restores only when the write actually landed — so a refused compare-and-set never overwrites the user's
  edit. References carry the place (`<placeId>|<name>::<path>`); undo in another place is refused.
  *Roblox:* script edits (`roblox_edit_script` — the file patch engine on the script text,
  `roblox_set_script_source`, `roblox_create_script`) are undoable from Activity and written compare-and-set
  (the plugin refuses if the script changed since it was read). The approval card shows the real diff from
  the script as it is in Studio; a change made after approval is caught before running. Creating/deleting
  instances and setting properties stay Studio Ctrl+Z steps; their card says "Not undoable here - Ctrl+Z in
  Studio reverts it". Playtests are input tier ("asks"), "Not undoable" (scripts in a test can reach the
  network/DataStores). *Data-loss fix:* `roblox_read_script` returned at most 6,000 characters with no notice
  and the only write replaced the whole script; reads are now windows of ≤ 400 lines / 16,000 characters
  that name the lines not shown and how to read on, and partial edits go through `roblox_edit_script`.
  *Bridge v3:* one session per plugin copy (instance id, role, place); commands go to one edit session — the
  place in the foreground Studio window (title from the observer) else the most recently active — through
  its own queue; each command carries its caller's deadline and is dropped once the caller stops waiting or
  the user presses Stop (a timed-out change can no longer run 40 s later); late results are discarded; the
  playtest flag comes from live play-test sessions; `health_check` pings. A v2 plugin still works (legacy
  session per role) but v3-only tools say "update the plugin". Roblox action tools are offered to the model
  only while Studio is connected (`roblox_status` always, to explain how to connect). A project's folder is
  searched for place files, and the brief notes when Studio has a different place open than the project's.
  *Playtests:* the edit plugin calls `StudioTestService:ExecutePlayModeAsync/ExecuteRunModeAsync` with
  `{agentic=true, seconds}`; the play-test server copy reads `GetTestArgs()` and calls `EndTest` after the
  time, flushing its errors (with `ScriptContext.Error` stack traces) first; the host collects errors pushed
  meanwhile. Without StudioTestService, Run mode falls back to `RunService:Run/Stop`.

- **D23 — Web reading is server-side and public-only; interaction happens in the assistant's own browser.**
  *Search:* no key needed — DuckDuckGo's HTML endpoint (POST, parsed with `html.parser`, redirect links
  unwrapped, deduplicated), or a SearXNG instance the user sets (Settings > Agent > Web). Results tell the model
  to read before relying and to cite `[title](url)`; the prompts say the same. *Reading:* `web_read` fetches
  with httpx (3 MB cap, redirects followed by hand), refuses anything but http(s) and any host that resolves to
  this PC or a private/link-local/reserved address — on every redirect hop — so a page or an injected line
  cannot make the assistant read local services (this app's API, a router). Text windows of 10,000 characters
  name what was not shown. Non-text types are refused with a pointer to `download_file`. *Browser:* a second,
  visible Edge/Chrome window started with `--remote-debugging-port=9333 --user-data-dir=data/browser-profile`
  and driven over CDP with the `websockets` package already installed (Playwright is not available on the
  host). The separate profile holds none of the user's passwords, cookies or cards; the user signs in there
  when needed. `browser_read` numbers the visible controls (`data-agentic-id`), and `browser_click` /
  `browser_fill` act on those numbers — trusted mouse events and `Input.insertText`, so frameworks see real
  input; numbers from an old read fail with "read again". *Rules in code, not prompts:* `browser_fill` is
  `ALWAYS_ASK` (no override or god mode makes it automatic; Deny still wins); a click on a control that submits a
  form or says buy/pay/order/send/delete/publish/subscribe/… is always asked via `ALWAYS_ASK_WHEN` (other
  clicks are input tier — ask by default, overridable); password/`cc-*`/one-time-code fields, labels for card,
  CVV, IBAN, SSN, passport, national ID, driver's licence, tax ID…, and values that pass the Luhn check or look
  like an SSN are refused before the browser is touched, with "ask the user to type it". Clicks and fills are
  "Not undoable"; the card shows the page, the control and the typed value. `browser_read` counts as a local read
  (sends nothing); `web_search`, `web_read` and `browser_open` are network egress and ask once a request is
  tainted. `browser_open` may open localhost (web development), unlike `web_read`. The app closes only the
  browser process it started, on shutdown. "Search/look up/on the web/https://…" requests are now read turns
  (they got no tools before).

- **D24 — MCP tools are ordinary tools.** The `mcp` package isn't installed on the host, so `mcp_client.py` is
  a small client for the needed subset (protocol 2025-03-26: initialize, tools/list with cursors, tools/call,
  resources/list/read, notifications/tools/list_changed, notifications/cancelled; server-to-client requests are
  answered "not supported"). stdio servers start through `shutil.which` (finds `npx.cmd` on Windows) without a
  console window; their stderr is kept (last 60 lines) as the server log; a crash turns the server to "error"
  with its last words and fails pending calls at once. HTTP servers: POST per message, JSON or SSE answers,
  `Mcp-Session-Id` kept. Each tool of a connected server is registered in the one registry as
  `mcp__<server>__<tool>` (its own input schema, validated like any tool), re-synced on connect / disconnect /
  list_changed, and offered to the model in action turns only (capped at 60 per server); parsing accepts those
  names (`schemas.known_tool`). Permission: destructive tier → asks by default; a per-tool rule in Permissions
  (the MCP tools are listed there) is the only way to auto-run one; folder rules never apply; tainted requests
  ask. Nothing a server says — descriptions, `readOnlyHint` annotations — changes a permission. Calls run
  isolated with a 130 s limit, are never retried, and Stop sends `notifications/cancelled`. Results are text
  (images/audio noted, not shown) framed as untrusted content. Config lives in `data/mcp_servers.json`
  (git-ignored); environment values and headers are never returned by the API or logged — the UI shows only
  their names, and an edit that leaves a value empty keeps the saved one. Adding a server starts its program:
  the form says so. Only the user adds servers (no tool can).

- **D25 — The observer only suggests, reads only while on, and says why.** *Off means off:* with
  `proactive_enabled` false `tick()` returns before sampling anything (no foreground title, idle time, Studio
  focus hint or habit learning) and in-memory observations (titles, sessions, per-app minutes, scratch) are
  dropped. *Quiet hours* now hold every suggestion, not just notifications. *"Don't suggest this again"*
  mutes the suggestion's `mute_key` — e.g. `habit:Discord`, `not_responding:<app>`, `low_disk:C` — not the
  whole detector (the detector switch in Settings does that), is persisted in `data/awareness.json`
  (`muted`), listed in Settings with Unmute; dismissal blocks and snoozes persist too. Old detector-wide mutes
  (weight 0.0 in existing files) still hold. *Feedback no longer mutes silently:* admission ignores the weight;
  a dismissed kind's cooldown is divided by its weight (floor 0.25 → at most 4× rarer) and Settings shows
  "less often" with Reset. *Opt-ins stay opt-in:* a detector override can't turn the clipboard or habit learning
  on. *No title fragments on disk:* habits are keyed by known app name or the .exe name. *Every suggestion has a
  `reason`* — one line naming the concrete signal ("Studio's Output shows 3 nil-index errors in
  ServerScriptService.Main"). *Signals:* `awareness.SIGNALS` lists everything that can be read, what it is used
  for and what is kept; each detector declares its signals; Settings > Proactive help shows the list. New
  detectors: `repeated_actions` (the same 3–4-app cycle three times in 15 min → offer to open/arrange them),
  `idle_after_error` (2+ min without input after a Studio Output error or an error window → "Stuck on that
  error?"), `project_opened` (Explorer/editor showing a project's folder → open the project), `studio_project`
  (Studio opens a project's place → switch project), `routine_time` (a routine run at this time ±30 min on ≥ 2
  earlier days and not today → run it; routines now keep their last 30 run times). Accepting a project/routine
  suggestion only returns it to the UI, which uses the normal activate/run paths (approvals apply). *Local only:*
  the observer's one-line activity summary goes to local models only — cloud models get none (the LIVE MACHINE
  STATE window list, which the desktop agent needs, is unchanged). Behaviour change noted in
  `test_awareness.py` (the forever-mute test now checks the precise mute).

- **D26 — Phase 3 review fixes.** What changed, by finding:
  *Batch decisions (high).* The loop decided every call of a batch before any ran, so
  `[browser_open, browser_read, browser_click(7)]` could click the NEW page's element 7 on the strength of
  the OLD page's. Each auto-run call is now decided again right before it runs (an earlier call can change
  the page or taint the request); a click on an element that isn't in the last page read always asks; and
  right before a click or fill the live control is re-read - refused if its label, kind or form changed or
  something covers it (`browser.AgentBrowser._live`).
  *Secrets (high).* `read_file`'s secret rule now covers the app's own `settings.json`, `keys.local.bat`,
  `data/mcp_servers.json` and `data/browser-profile/`; content searches skip secret files (they were an
  oracle). MCP server processes get an allow-listed environment plus their own env - never the app's API keys.
  *Local API boundary.* Writes and reads under `/api/` must come from this exact origin (Sec-Fetch-Site /
  Origin; a page on another localhost port is "same-site" and refused); non-browser clients (the routine
  trigger) send neither header and pass. `/roblox/*` answers only clients without browser headers (Studio's
  HttpService). The assistant's browser never opens or drives this app's pages or the model server.
  Opening an MCP resource from the tab is the `mcp_read_resource` tool through the one path (it was a GET
  side channel). Editing a server to a new command/URL drops its saved secrets.
  *Other.* Observer keys are persisted as hashes (they held window titles; old stores are migrated) and
  muted entries keep no titles; suggestion prompts reference Studio errors instead of pasting their text;
  content markers inside framed content are neutralised; turning proactive help off clears suggestions.
  `roblox_set_property("Source")` and scripts via `roblox_create_instance` are refused (adapter + plugin).
  Web reads connect only to the address that was checked (`web_tools._PublicOnlyBackend`, DNS rebinding).
  MCP: Stop reaches HTTP servers (the POST runs on a worker thread and `notifications/cancelled` is sent),
  disconnect fails waiting calls at once, one connect at a time, credentials redacted in args/URLs,
  instruction-like tool descriptions withheld, truncated tool names get a hash suffix, at most 8 MCP tools
  in the small local tool list, the registry is locked. Routines keep the taint of the run they came from,
  never replay browser clicks/fills, and end with `routine_finished` (not "verified"). Windows: store files
  are replaced with a short retry on `PermissionError` (`atomic_io.py`); npx-started MCP servers are ended
  with `taskkill /T`. Tests: the harness sandboxes `data/recordings`, the schedule tests `data/scheduled`.

- **D27 — The workspace shows the record, not a story.** The Activity panel renders runtime events only: a
  step's "Why?" is assembled from what the runtime stored with it (the active plan step, the model's own
  `justification` argument, the id of the note/reasoning event from the same reply) - nothing is inferred, and a
  step without them has no "Why?". Raw reasoning appears only when the model emitted reasoning (its reasoning
  channel or `<think>`), capped at 20,000 characters, labelled as the model's text. Past runs are the same
  events saved at the end of the turn (slimmed to the fields the feed renders); opening one never replaces the
  live view silently (a banner, "Back to live"). The effort slider shows the level that will actually run:
  requested vs applied (fallback reason), where it came from, and what this model can't run; when a project
  sets the level, a slider change becomes a session override and the note says the project still says
  otherwise - it never silently does nothing. No setting here touches permissions (non-negotiable 7).

- **D28 — Benchmarks describe what the agent will get.** The benchmark sends its cases through the same
  constrained-call path, streaming client and parser as a real turn (thinking off, temperature 0), so a model
  whose calls the agent can't use scores low even if it "knows" the answer. It never executes a call - the
  tools are real schemas, the calls are only read. It runs only when the user starts it (it loads the model and
  holds the turn lock; a chat meanwhile is told the app is busy), one at a time, cancellable. Results are tied
  to the llama-server build. Routing and effort read them from `model_caps`; nothing reads model names for
  capability any more (cloud models, which are not benchmarked, list no strengths).

- **D29 — Ternary Bonsai 2 27B (PTQ1_0) needs PrismML's llama.cpp fork.** The file
  `E:\ClankerLocalCpp\Clankers\Ternary-Bonsai-2-27B-PTQ1_0.gguf` (5.95 GB, 1.75 bits/weight) stores tensor
  types that upstream llama.cpp does not have, with Hadamard-rotated weights; stock build 10621 cannot load
  it. It runs on the `prism` branch of github.com/PrismML-Eng/llama.cpp (Windows releases:
  `llama-prism-b<build>-<hash>-bin-win-cuda-12.4-x64.zip`; newest checked: prism-b10735-842b188). The fork
  tracks upstream, so it also runs every other model, and router mode is kept - one server, `--models-max 1`,
  no second VRAM owner. `run.bat` uses `E:\ClankerLocalCpp\prism\llama-server.exe` when present, else the
  stock build (rename the folder to go back).
  *Context:* the router's global `--ctx-size 65536` overrode every preset (D12), and Bonsai needs a smaller
  window on 8 GB, so the flag is gone; `[*] ctx-size = 65536` in models.ini is the default instead. Verified
  on b10621 by reading `/v1/models` (no model loaded): with the flag every model gets 65536, without it a
  preset's own value applies. To keep every existing model exactly as it ran, their `ctx-size` lines
  (250000 / 32768 / 16384, never in effect) now say 65536; a smaller value set in Settings > Resources
  will now actually apply. Bonsai: `ctx-size 16384`, `flash-attn on`, q8_0 K/V cache, `batch-size 512`
  (weights 5.95 GB + ~0.5 GB KV at 16K; PrismML lists ~16K usable context for PTQ1_0 on 8 GB).
  *Behaviour:* the model thinks by default; the app already sends `chat_template_kwargs.enable_thinking`
  per effort level, which its template honours, and its own low temperature. No mmproj is installed, so it
  is text-only. *Errors:* the router reports each model's executable (`llama_local.router_status()["exe"]`);
  a failed load of a PTQ1_0/PQ2_0 model on a non-PrismML server says so and where to install the fork
  (`llama_local.load_hint`, used by chat turns, force-load, handoff and benchmark). `test_bonsai.py`.
  *Installed and verified 2026-09-24:* `llama-prism-b10735-842b188-bin-win-cuda-12.4-x64.zip` from the
  official release (built by GitHub Actions), SHA-256 `a6fe7fe4…07f5dd` matched GitHub's published digest,
  archive checked for unsafe paths, extracted to `E:\ClankerLocalCpp\prism\` (stock build untouched); the zip
  ships no CUDA runtime, so the existing CUDA 12.4 `cudart64_12` / `cublas64_12` / `cublasLt64_12` DLLs were
  copied beside it. On a spare port with the real models.ini: Bonsai loads fully on the GPU (7.8 of 8 GB in
  use, ctx 16384), ~28 tok/s warm, native tool calls correct with thinking on and off; Llama-3.2-3B,
  Qwen-3.5-4B and Gemma-4-Vision-Q4 load and answer on the fork; one model resident at a time; the app reads
  the server as PrismML (`is_prism_server()` True, no hint). Found meanwhile, not changed: models.ini lists
  two files that are no longer on disk (`Qwen3-8B-Q5_K_M.gguf`, `Llama-3.2-11B-Vision-Instruct.Q4_K_M.gguf`).
  Benchmarked through the app (D28) on the fork: 5/6 valid tool calls (the patch case read the script instead
  of editing it), 0 cut off, ~26 tok/s, median case 13 s; stored in `data/benchmarks.json`.

- **D30 — Finishing on the Windows host (2026-09-24).** The deferred items that needed Windows, done here:
  *Ambient idle screen* (index.html `Idle`, Settings > Appearance "Idle screen": Off / 5 / 10 / 30 min, default
  10): after that long without input, and only while the live state is idle - never over a running task, an
  approval or prep card, an error, Settings or a hidden tab - the workspace fades to a calm screen with the time
  and the same live facts the sidebar shows (local model, Roblox Studio, waiting suggestions, current chat); the
  theme's ambient background keeps reporting status behind it. Any input wakes it; the waking key/click is
  swallowed so it cannot type or press anything. Reduced motion: no breathing, no fade. `test_ambient_idle.py`.
  *Scheduling:* `schtasks /sd` is read in the locale's short-date order, so `%m/%d/%Y` was wrong for
  day-first locales; `productivity_tools.schtasks_date` now follows `sShortDate` (this host: en-US `M/d/yyyy`,
  unchanged output). *Web:* `web_search` (DuckDuckGo) and `web_read` checked against the live network.
  *Launcher:* a desktop shortcut "Local AI" runs `run.bat` in this folder.

- **D31 — Context window meter and compression.** Beside the message box, a ring shows how full the next
  request's context is (`agent/context_window.status`): instructions + tools (fixed, ~10K tokens with the full
  catalogue), this chat's messages and the summary, against the limit that actually applies - the model's real
  window minus the reply reserve, or the effort level's `context_tokens` when smaller (the same numbers
  `_fit_for_local` / `trim_conversation` use). Its popover shows the breakdown, why that limit, the automatic
  threshold (Off / 80 / 90 / 95 / 97 %, default 97, also in Settings > Agent) and "Compress now".
  *Compression* (`context_compress.py`, pure): only whole turns before the last `MIN_KEPT_TURNS` (2) real
  requests are replaced, so the task in progress and its tool call/result pairs are never touched; the model
  writes the summary (`model_json`, thinking off, input capped to half the window) folding in any earlier
  summary; if no model answers, a rules digest (each request, its reply, the tools used) is used. The summary is
  stored in `app_state["context_summary"]`, saved with the chat (`context_summary` in the chat file), cleared on
  a new chat, and sent beside the system prompt - not as a message (templates that require user/assistant
  alternation would reject it; the prefix changes only on compression, so llama.cpp's prompt cache holds) -
  labelled as a record, not instructions, and framed as untrusted if it contains injected instructions.
  *Triggers:* `maybe_compress()` runs before every model call (mid-run too) at the threshold; the button
  (`POST /api/context/compress`) refuses while a task runs. `history.fit` still drops the oldest turns as the
  last resort (e.g. compression off, or one task that alone outgrows the window). Each compression is an
  Activity row (`context_compressed`: auto or manual, model or rules, before → after %). Checked live on the
  PrismML router with Llama-3.2-3B: a model-written summary, saved and reopened with the chat; the button keeps
  its "Compressing…" state through poll redraws. `test_context_compress.py`.

- **D32 — Readability pass, Dusk, real sliders, "what was added".** *Readability:* every text style under
  12px was raised (35 rules; only glyph-sized badges, the ⓘ, key hints and severity tags stay at 10.5–11px);
  popover headings/descriptions, tooltips, settings group titles and row descriptions went up a step; long
  descriptions (effort, Prompt Prep, routing, looks, themes, Agent settings) were rewritten to one short line.
  *Sliders:* Effort and Prompt Prep are real sliders - a thumb that follows the pointer and settles on the
  nearest level this model can run, the level name shown once in the header (not six 10px labels), ends
  "Faster/Smarter" and "As typed/Planned". `--lvp` (0..1) drives the look: the dithered fill gets denser and
  brighter and the thumb's glow grows toward the max; at the max level a shimmer runs through the fill and the
  thumb pulses (reduced motion keeps the glow, drops the motion). Keyboard and unavailable-level rules are
  unchanged. *Dusk:* Dawn after sunset - shares every Eclipse/Dawn layout and motion rule
  (`:is(eclipse,dawn,dusk)`), own twilight palette (umber surfaces, apricot/rose horizon), body text 13:1 on
  its ground; `test_dusk_is_warm_dark_and_readable`. *What was added:* `TaskState.additions()` is the single
  source of what preparation/planning add to a request; `brief()` is built from it (text unchanged), and
  `brief_sent` carries it structured (`prepared`), shown under the user's message as a pill that opens to each
  added part (green +), saved with the chat (`p` on the user entry). *Fixes found in the pass:* Qwen3-style
  XML tool calls printed as text (`<tool_call><function=…><parameter=…>`), which Ternary Bonsai 2 produced in
  a real chat and the UI showed as the reply, are now recovered and run (`parsing._xml_tool_calls`, also
  when cut off); such replies already saved show as "wrote a tool call as text, so it wasn't run"; menus stay
  attached to their button when the window is resized; models whose file is missing say so and can't be
  loaded (`file_found` in `/api/local-models/config`). `test_prompt_prep.PreparedViewTests`,
  `test_tool_calls` XML cases.
- **D33 — Tool calls: the turn policy withheld the tools.** From real chats (`data/runs`, `data/chats`):
  "in Test1 make a simple inventory GUI", "New experience" and "Up to you" were classified as mode `none`,
  so no tools were offered; Bonsai then invented tool names and printed them as text, or its calls were
  refused as "not a request". *Fix in `intent_router.classify_turn`:* leading fillers ("ok,", "hey") are
  dropped; the action verb may follow a request phrase ("can you", "I'd like to", "let's") and/or a scope
  ("in Test1", "for my obby,"); a bare "new <thing>" is an action; more verbs (design, set up, animate,
  publish, tweak…); news/weather/price questions are web reads. A short reply ("up to you", "yes", "go
  ahead") to an assistant question or offer continues the previous request with its authority (`follows`;
  `agent/tasks._previous_turn`), and the run's request reads "<request> (the user replied: …)". A bare "ok"
  with no offer still grants nothing. "Open Test1 in Studio" routes to the place only
  (`desktop_router._open_place`), never an app, and a place that isn't a saved file is explained (cloud
  places open from Studio's start page). *Recovery:* an XML call with an unknown name is now parsed and
  refused with the closest offered tools (`difflib`), so the model can correct itself instead of printing
  text; the "did not run" message says how to ask for the action. `test_turn_policy.py`.
- **D34 — Web search and reading that hold up.** Measured live 2026-09-24: DuckDuckGo refuses a burst of
  automated searches with a 202 "anomaly" page, which parsed as "No results"; Wikipedia refuses browser
  look-alikes from scripts (403) and accepts a client that names itself with an address; Stack Overflow,
  Medium, news sites and the Roblox DevForum put a Cloudflare/bot check in front of any script; Reddit sends
  logged-out readers to /login. *Search (`web_tools.web_search`):* browser-like headers (no
  "AgenticWorkspace" token in the User-Agent); one DuckDuckGo request per 3 s at most; a refusal is reported
  as one (`refused`, `permanent_failure` - never retried) and DuckDuckGo is left alone for 2 min (doubling to
  20) instead of extending the block; results are kept 15 min; when DuckDuckGo refuses, the same search runs
  in the assistant's browser (DuckDuckGo, then Bing; Bing's tracking links unwrapped). *Reading
  (`web_read`):* a refused request is retried with the honest User-Agent; still refused, or a page built by
  JavaScript, is read in the assistant's browser; failing that, the Wayback Machine's newest copy (dated, with
  its address); otherwise a message that names the reason and what to do. A redirect to a sign-in page counts
  as a refusal. Reading on (`start` > 0) reuses the page. The text extractor now prefers `<main>` and leaves
  out menus, dropdowns (Wikipedia's language list), navboxes, `[edit]` links and hidden text.
  *The browser fallback* (`browser.AgentBrowser.load`) uses a temporary tab, so the page the assistant has
  open and its element numbers are untouched; it waits out an interstitial check up to 12 s. It runs only
  with Settings › Agent › Web › "Use the browser when blocked" on (default) AND when `browser_open` itself
  would run without asking (`tools/dispatch._browser_fallback` - a rule for browser_open, lockdown or a
  tainted request turns it off). Tool timeouts: web_search 60 s, web_read 75 s. `test_web_browser`
  `BlockedSearchTests`, `BlockedReadTests`, `RealBrowserTests.test_load_…` (real Edge, headless; the real
  browser tests now find Edge/Chrome in Program Files instead of skipping).
- **D35 — The sliders, reworked.** Problems: the thumb snapped to where you pressed (`.dragging` removed its
  transition); every `refreshEffort`/`syncAgentMenu` during a drag or glide re-applied the saved level and cut
  the motion; the end stops and the thumb were centred on the track's outer edges, so half a dot was cut off.
  *Motion:* one spring per slider (`lvMoveTo`/`lvStep`/`lvPaint`, level units); code only sets its target -
  a press on the track glides there, a press on the knob grabs it where it is, a drag follows with a little
  weight and a soft pull toward each level, a release settles with ~7% overshoot and saves. The knob
  stretches along its motion and squashes against an end. Passing a level is a detent: the stop pops, the
  knob's core swells, the name slides in. *Geometry:* `--pad` (the knob's radius) keeps knob and stops inside
  the track; the fill always reaches the knob's far edge. *Look:* Effort is a continuous energy bar (round
  knob, flowing stripes that strengthen with the level); Prompt Prep is a stepped bar of three segments (a
  mask) with a square keycap knob and stop marks in the gaps. Colours blend between levels (`@property
  --lv-color`). *Max:* the block is tinted, the name turns to a moving gradient with a "Max" badge, and a
  one-time burst plays on arrival; Effort then runs an aurora through the fill, a breathing halo, a light
  sweep and a comet ring around the knob; Prompt Prep a scan through the segments, a drifting blueprint grid
  and a slowly turning dashed frame. Loops animate only transform, opacity or a small background; reduced
  motion drops all of it (the spring jumps). Keyboard and unavailable-level rules are unchanged.
- **D36 — Three bugs, root causes (2026-09-25).** `test_bugfixes_d36.py`.
  *Roblox tools never reached the model.* (1) "what do you see in Test1 roblox place?" was classified
  mode `none` - the read rule needed "my/this/the place" - so the request carried no tools at all;
  Roblox/Studio mentions and "<name> place" are now read turns (the 8 read-only `roblox_*` tools, including
  the existing `roblox_get_tree`; no new tool was needed). (2) The system prompt named ~20 tools whatever was
  offered, so the model "knew" names it didn't have and guessed; `prompts.for_tools` now drops every sentence
  naming a tool not offered this turn (and "roblox_*" when no Roblox tool is). (3) On a read turn an invented
  name ended the run as "unauthorized" before the model could learn the real names; unknown names now get
  the NOT EXECUTED reply with the exact offered list (or "you have no tools for this message"), and only real
  tools outside the turn's authority end the run. (4) "Place1": Studio's `game.Name` is "Place1" for a place
  opened from Roblox (checked live: placeId 123309239249745, window "Test1"); the plugin now reports the
  published name (`MarketplaceService:GetProductInfo(PlaceId)`, cached, `game.Name` for unsaved/offline
  places). The custom bridge itself was connected and answering (live), so it was kept rather than switched
  to Roblox's official MCP server.
  *Sliders locked before a model loaded.* The slider reads the level for the model the NEXT message uses
  (`_next_model(mode, pinned)`); a change was saved per model under `llm._current_model()`, the model that
  last RAN - before a local model has answered, the last cloud model. The choice landed under the wrong name
  and the slider kept showing (and snapping back to) the shown model's old entry, e.g. an earlier "Max".
  Saves now carry `for_mode`/`for_model` (not settings) and are stored under the same model the slider
  reads; the source line names the model ("your choice for Ternary-Bonsai-2-27B") and the global level reads
  "your default". Capability checks and fallbacks are unchanged (they apply once the model is known).
  *"Save failed" on context length.* The UI sends `null` for every blank box (= "use the default");
  `ModelConfigUpdate` declared `ngl: int = None`, which pydantic 2 reads as "an int", so every save was a 422
  and the UI's catch showed "Save failed.". Fields are `int | None` (null clears the override, as
  `update_model_config` intends), values below a minimum are refused with a message, and the UI shows the
  server's real reason (`api()` now spells out 422 field errors; `saveSettings` shows its error too).
- **D37 — Models fitted to the GPU; Diagnostics; live model activity (2026-09-26).** `test_diagnostics.py`.
  *What was measured (RTX 3060 Ti 8 GB, 2026-09-26):* at their configured 65536 tokens, 7 of 11 installed
  models needed more VRAM than the card had free (Mistral 13.0 GB, DeepSeek-Coder-V2 14.0 GB, R1-7B and
  Qwen-2.5-Coder 8.6 GB, Llama-3.2-3B 9.6 GB...), because the f16 KV cache alone was 3.5-8 GB. With
  `[*] ngl = 99` llama.cpp's `--fit` (on by default) may not touch the layer count, so the NVIDIA driver's
  sysmem fallback lent system RAM instead - running, several times slower, invisibly. Mistral and
  Qwen-2.5-Coder were also set past their trained 32K. What other apps held in VRAM ranged 1.06-1.89 GB within
  one hour, so a plan fitted to one moment is wrong the next.
  *Decision:* `[*] ngl = auto` + `fit-target = 256` (the default 1024 MiB margin would move layers off
  needlessly): `--fit` places each model against the VRAM free *at load time* - all on the GPU when it fits,
  only the overflow on the CPU, never spilling. Context sizes are planned against the everyday baseline
  (other apps at most 1.1 GB, `DEFAULT_OTHER_MB`): the largest context up to the trained one, at least 16K
  (below that the agent's tools and a few files no longer fit), f16 or q8_0 KV cache; a MoE model whose
  weights exceed the card keeps only as many layers' experts in RAM as needed (`n-cpu-moe`, DeepSeek-Coder-V2:
  17 of 27). VRAM is estimated from the GGUF header (`gguf_meta.py`: hybrid Qwen 3.5 caches 1 layer in 4,
  Gemma's sliding-window layers cache only the window, DeepSeek 2's MLA one latent per layer). Applied to
  models.ini (original: `models.ini.bak-20260926-165519`); missing model files are reported, not removed.
  *Verified live (a second router on port 8099, warm requests) - and two rules it forced:* (1) hybrid models
  (Qwen 3.5 / Bonsai 2, `full_attention_interval` in the header) must never be split: `--fit` put layer 0 on
  the CPU, llama.cpp then disabled the fused Gated Delta Net kernel for every layer and Huihui-Qwen3.5-9B gave
  no 120-token reply in 10 minutes - so they keep `ngl = 99` (`keeps_whole`); (2) with `n-cpu-moe`, `--fit`
  aborts ("tensor_buft_overrides already set by user") and the experts left in RAM are memory-mapped from
  E:, a spinning disk (Seagate ST2000DM008): the first reply took 105 s; `load-mode = none` (the build's
  replacement for `--no-mmap`) reads them in at load: 13 s. Tokens/s before -> after: Mistral-7B 42.5 -> 63.5
  (5.9 GB had spilled), DeepSeek-Coder-V2 7.7 -> 18.8 (llama.cpp's own --fit: 13.3-13.9; all experts in RAM:
  13.3), Ternary-Bonsai-2-27B 17.5 -> 28.1, Huihui 26.2 -> 35.4 (it cannot fit 8 GB at any context: stays
  whole with ~0.9 GB lent by Windows; a Q4_K_M quant would fit). Pinned RAM (load-mode none) shows up in the
  same "Shared Usage" counter as a spill, so a spill is only what the GPU part lacks in VRAM (`placement`).
  The file must be UTF-8 without a BOM: llama-server refuses a preset that starts with one.
  *Telemetry:* per-process GPU memory now comes from the "GPU Process Memory" counters through pdh.dll
  in-process (0.4 ms) instead of a PowerShell `Get-Counter` (1.6 s, every 1.5 s while a model was loaded);
  the Shared Usage counter is the RAM a model spilled into.
  *UI:* Settings > Diagnostics (every check, per-model VRAM bars, Apply / Apply all with a backup); under the
  Thinking row a strip with the model, where it sits (GPU / ~N% CPU / N GB in system RAM) and its stage
  (loading into memory · Ns, reading, thinking/writing · tok/s); the Thinking row says "Loading <model> into
  memory" while the router loads it (`agent/models._watch_load`).
  Same round: **Bias check** tool (`composer_tools.BIAS_RULES`, the user's wording kept: both sides equally,
  trust per source, raw findings, a verdict; Research searches "evidence against"/"criticism" too and adds a
  "Sources and trust" table) and the **Terminal Lite Dark/Light** themes (skin `terminal` + `data-tone`).
- **D38 — Privacy: temporary chats, one network layer, private search; the Studio Overlay, a secret theme, an
  app window (2026-09-26/27).** `test_temp_chat.py`, `test_netlayer.py`, `test_search_privacy.py`,
  `test_overlay_theme.py`. The spec came from another model without the code; read for intent, not literally.
  *Temporary chat (`privacy.py`):* enforced where data is written, not in the UI. A chat id `tmp-…` fails
  `chat_store`'s id pattern; `privacy.scope(chat_id)` (a contextvar, copied into tool threads by
  `privacy.wrap`) makes `chat_store.save`, `memory.add/update`, `usage.add`, `run_log.save` and the recorder
  refuse or skip; events and audit lines are tagged `temp` and purged when the chat ends (`on_discard`);
  `session.json` never names it, so a crash can't restore it. Attachments and the code runner use one folder
  per chat under %TEMP%\AgenticWorkspace-temp, deleted on close, on exit and by a sweep at startup; the
  search cache and history are bypassed; the assistant's browser runs a second, private instance with its
  profile in that folder. "Save this chat" adopts it into history only on the user's click.
  *Network (`netlayer.py`):* every outbound httpx client is `netlayer.client(component)` - a transport that
  applies the route (direct, system proxy, a SOCKS5/HTTP proxy, Tor 9050/9150; socks5h so the proxy
  resolves names), the kill switch (a dead route blocks - never a silent direct fallback; the user can go
  direct once or for the session), Require VPN, the in-memory connection log (host, route, size - never a
  URL or query; saved only if asked, never a temporary chat's) and byte counts. litellm gets the route as
  proxy environment variables; a socket/DNS guard (`netlayer.install`) blocks or logs anything that goes
  around it (it caught litellm fetching its price list from GitHub mid-chat - now read from its bundled
  copy). The assistant's browser gets the route as `--proxy-server` + `--host-resolver-rules` and WebRTC is
  off in every window. Per-component overrides: search, models, other.
  *Search modes (`search_engine.MODES`):* Standard; Private (no cookies, Referer or tracking parameters, a
  generic User-Agent, personal details taken out of queries); Maximum Privacy (own SearXNG over its JSON API
  through a proxy/Tor, no history or cache, and a card asks before anything less private is used); Local
  Only (no network - refused by the transport; searches read pages, history, project files, memory);
  Disabled (web and browser tools removed). Per-message override in the Tools menu; optional query preview;
  the search row says mode · backend · route (kept with the saved reply).
  *Speed indicator / Studio Overlay (index.html `NetIndicator`, `Overlay`):* the indicator polls
  `/api/network/stats` once a second only while the window is visible; latency comes from requests already
  made. The Overlay is the Studio look's second layout: four panels (model, Roblox Studio, network,
  session), docked against the chat column's edges or dragged anywhere (positions as window fractions in
  settings.json), collapsible, click-through, Ctrl+Alt+O; painted at most 4x a second, text only.
  *App window (`app_window.py`):* run.bat opens Edge/Chrome in `--app` mode with its own profile (no tabs,
  own taskbar button; APP_WINDOW=0 keeps the old tab; http://127.0.0.1:8000 still works in any browser).
  "Always on top of Roblox Studio" opens the panels (`?hud=1`) in a 280x440 window with a separate profile
  (in the main one Edge ignores `--window-size`), made topmost/layered/click-through with SetWindowPos
  (+ SWP_NOOWNERZORDER - without it Edge silently refuses topmost) and moved with Studio's window.
  *Judaism Mode:* a skin outside THEMES, so `theme` can never hold it and it can't start with the app. Six
  quick clicks on the logo unlock it (`unlocked_themes`); it's on for one window session only (since D41 it
  never appears in Settings). White and
  royal blue, a star lattice, flag bands, turning stars, rising motes, a sweep; moments from the work
  (`SecretFx.pulse/state`: chat opened, tool call, model loading, thinking, reply done). Music: a file the
  user picks, kept in the page's IndexedDB, played only on a click; Ctrl+Alt+K / the star button stop all
  effects and audio. Reduced motion stops everything in the global block.
- **D39 — Voice mode, research depth, Investment research and Market data (2026-09-27).** `test_voice.py`,
  `test_research_finance.py`.
  *Voice (`voice.py`, index.html `Voice`):* whisper.cpp's server (CPU BLAS build; CUDA optional) and Piper, both
  local, downloaded only on the user's click into data/voice (GitHub + Hugging Face through the "other" route;
  zip members checked against path traversal). The page records with Web Audio (echo cancellation on), finds
  speech with an adaptive noise floor (0.4 s pre-roll, ~0.8 s of quiet ends it), sends one 16 kHz WAV per
  utterance, and speaks the reply sentence by sentence as it streams (`Voice.feed` from paintDraft,
  `Voice.finish` from completeTurn), one sentence fetched ahead; barge-in stops it. Piper stays running
  (--output_dir; 0.25 s a sentence warm); whisper-server stays loaded (~0.9 s for a 3.6 s phrase on this CPU).
  A voice turn adds a spoken-style header (`voice.voice_header`). Approvals and questions are answered in the
  chat - voice mode says so. No audio is stored.
  *Research depth (`agent/research.py`):* DEPTHS quick/standard/deep ("Extensive" in the UI - "Deep" was an
  old effort name)/ultra, or auto from the effort. It scales by never holding pages: per-round bullets citing
  [n], deduplicated by word signature, consolidated past NOTE_BUDGET (a merge that loses citations is
  refused), saturation stops a sub-question after two rounds with little new, `per_domain` caps a site,
  large reports are written per section plus a summary. The ledger (sources with their numbers, notes,
  progress) is saved after every round in data/research, never for a temporary chat; "resume" continues the
  newest unfinished one with the same [n]. Research searches are `unnumbered` (only pages read get numbers)
  and the registry's limit follows the depth (max 999 - [n] is three digits): the first live run hit the old
  80-source cap once the finance backbone had registered its figures.
  *Finance (`finance.py`):* keyless SEC EDGAR (company list, XBRL facts - the candidate tag with the newest
  figure, full-year periods only; filings), Yahoo chart API (Chrome UA; query2, then Nasdaq's API as
  fallbacks), Yahoo/Nasdaq headline feeds (only those about the company), US Treasury yield curve, BLS, World
  Bank, ECB; FRED, Alpha Vantage, Finnhub with keys (KeyStore.FINANCE - never in the search chain). EVIDENCE
  classes by domain plus headline wording ("stocks to buy" is OPINION, "price target" ESTIMATE); COMPUTED
  ratios say how they were worked out; per-share figures before a split are marked as filed. Investment
  research = Research with bias check on, a finance plan, the backbone first, evidence-labelled notes and a
  report with bull/bear cases, risks, what to watch, evidence quality - and no buy/sell advice.
- **D40 — A natural voice: Kokoro, Parakeet and live captions (2026-09-27).** `voice_natural.py`,
  `speech_text.py`, `test_voice.py`. Researched against the commercial assistants: every model in the TTS
  Arena top 10 is closed; of the open ones, Kokoro-82M (Elo ~1065) is within ~60 of the best open models,
  which all need a large GPU the local LLM is using - Kokoro runs on the CPU. Through sherpa-onnx (one
  18 MB package, its own ONNX runtime, no other dependencies): Kokoro v1.0, NVIDIA Parakeet TDT 0.6B v2
  (int8) as the accurate listener, and a 20M streaming Zipformer as the secondary listener (live captions
  + end-of-sentence detection; it drops a first word without a second of leading silence, which it now
  gets). Measured on the Ryzen 5 3600: Kokoro int8 is 1.4x SLOWER than real time (int8 needs AVX-512 VNNI),
  full precision is 2.9x faster (6 threads) - so full precision is the default and int8 is "compact".
  Parakeet: ~0.3 s for a 2 s phrase, warm. Naturalness beyond the model: `speech_text.normalize` (money,
  signs, dates, units, abbreviations), `polish` (trim, 8 ms fades, level), a first chunk that may end at a
  comma and later chunks of 110+ characters, gapless scheduling on the AudioContext clock with a 0.14 s
  breath (measured joins: 0.140 s), earcons, and a voice header that asks for spoken style. Live: first
  audio 3.6 s after sending with the LLM loaded (1.55 s of it the LLM). Piper and Whisper remain the fast
  and multilingual options, and fill in whenever the chosen engine isn't set up.
- **D41 — Languages, the voice screen, approving by voice, personas; the secret theme out of Settings
  (2026-09-27).** `test_voice.py`, `test_personas.py`.
  *Languages (`voice.LANGS`):* the old `voice_language` never took effect - Parakeet and whisper's `base.en`
  are English-only. Now `voice_lang` is `en`, a language, or `auto` with `voice_auto_langs`. Other languages
  go to a multilingual whisper model (`voice_multi_model`, small by default: ~1.3 s warm for a 3 s phrase)
  and a native Piper voice per language, spoken from a second Piper process (`_piper_lang`) so switching
  doesn't reload the English one. This whisper.cpp build returns flat noise (~1% each) as
  `detected_language`/`language_probabilities`; `language` (what it decoded in) is right, so that is used,
  and a language outside the chosen ones is re-run in the chosen one (with one extra language, that one).
  The page sends the heard language with the message (`ChatRequest.voice_lang` -> `turn_voice_lang`) and the
  voice header asks for the answer in it; each spoken piece carries it (`SpeakBody.lang`); English-only
  lines ("the rest is in the chat") are skipped in other languages. Piper 1.5+ voices (Hebrew, Korean) add
  multi-codepoint phonemes that piper.exe (2023.11.14) refuses to load; Hebrew also needs the Phonikud
  diacritics model to phonemize - `ADDON_LANGS`: heard and answered in Hebrew, shown instead of spoken.
  Japanese needs a Japanese phonemizer; Korean and Japanese are not offered.
  *Voice screen:* the orb is ~84vmin (was 52), one-word status, captions off by default (`voice_captions`;
  shown anyway when a reply can't be spoken), a 42 px bar (text box, mute, hands-free, chat). New settings:
  pause between sentences (`voice_breath`), volume (a GainNode after the analyser), `voice_patience` (quiet
  that ends a turn: 550/800/1300 ms; "long" also ignores the streaming listener's endpoint),
  `voice_sensitivity`, and all 28 English Kokoro voices grouped by grade and accent.
  *Approving by voice:* the pending approval is also shown above the bar (Allow/Deny, Enter/Esc; the chat
  card's own Enter handler stands down in voice mode, whose text box's Enter is a message). A spoken answer
  must be the whole phrase (`command()`: "approve", "allow it", "cancel", "no", Hebrew equivalents), never
  a barge-in recording for "approve" (it may be its own voice), and nothing is listened to for 450 ms after
  it stops speaking; it never says "approve" itself. `approval_broker.voice_ok`: every action "benign" by
  `permissions.classify` (recomputed, not taken from the card) or media_control/snap_windows, no warning,
  at most 3. `answer(..., via="voice")` refuses to approve anything else, so the limit holds even if the page
  misbehaves; the chat card then stays open for a click.
  *Personas (`personas.py`):* `personas` (Custom always first, default, empty = no change; up to 30, text up
  to 4000) and `persona`. The composer's persona button sends `persona`; the server resolves it and adds a
  PERSONA block to the system prompt (stable per chat, so llama.cpp's prompt cache survives). BUILTIN
  `israelgpt` is never saved or listed: the page sends it in Custom's place while Judaism Mode is on.
  *Judaism Mode* no longer appears anywhere in Settings (no card, no group, no name in the accent note), even
  once found; six logo clicks toggle it and its star menu has its effects and music. `relockSecretTheme` is
  gone.
- **D42 — Studio work in one call; the same tools at every effort level (2026-09-29).** `test_studio_tools.py`.
  The request (written by another model) blamed effort for "the model says it can only read" and asked for a
  loop rule for execute_luau. The user's own chats and run records showed the real causes:
  *Tools gated by a verb list:* `intent_router.classify_turn` gave "stack 10 parts", "do it", "No you do run
  the script", "...so do it pls" and answers to the assistant's own question mode `none`/`read` - no tools that
  change anything - so the model truthfully said it couldn't, then kept saying "I'm a text assistant" once it
  had them. Now: more verbs (stack, pile, line up, do, execute...), spoken fillers and a leading yes/no are
  skipped, "you do X", "...so do it" at the end, and `_answers_question` (a short answer to the assistant's
  question about an ACTION request continues it; never a refusal, thanks, a question or a new "tell me ...").
  A turn without write tools says so beside CURRENT TOOL MODE and forbids "I'm only a text assistant".
  *Effort:* tools and prompt never depended on effort; what did was thinking (off below Thorough) and a small
  model copying its own earlier refusals from the history. The write-access line (`prompts.STUDIO_WRITE_ACCESS`,
  in both prompts, naming no tool so `for_tools` can't drop it) and a test that builds every level's system
  prompt and tool list (identical) keep it that way. The user chose to keep effort's caps, planner and reviewer.
  *A place-name bug:* a project saved with its place in quotes ("\"Scratch Test\"") made `_project_brief` add
  "Studio has a different place open ... ask the user before changing anything" to every request in it - the
  model then asked before every change at every level. `roblox_places.place_key` compares without quotes, case,
  spacing or the MCP's "(placeId: n)"; new entries are saved without quotes.
  *One call:* the bridge deliberately runs no Luau, so the app wasn't connected to anything with execute_luau.
  With the user's approval, Roblox's own Studio MCP (StudioMCP.exe via %LOCALAPPDATA%\Roblox\mcp.bat) is an MCP
  server (data/mcp_servers.json). `studio_mcp.py` fills in studio_id (one Studio open) and datamodel_type (Edit)
  before validation and the approval card, and names the open place in the turn header - otherwise every call
  needs list_roblox_studios + get_studio_state first. `prompts.studio_rules` (from the tools actually offered):
  one script with a loop and a Luau example, Workspace-in-a-Folder as the default place (a model asked "where?"
  first). The bridge gets `roblox_batch` (fallback): create/set/delete in order, `repeat` + `step` expand to the
  plugin's own commands (no new plugin, no Luau), stops at the first failure, the card lists every change.
  REPEATED_WORK_RULE and `prompt_prep.repeat_hint` (turn header, any Prompt Prep mode) + `repeat_constraint`
  (first on Expand/Plan cards, and the planner's instruction). The compact prompt no longer says "call one
  tool, wait for its result" for everything; a local model's several calls of ONE tool stay one step and one
  card (`loop._same_tool_batch`).
  *Fitting:* Roblox's Studio MCP is 28 tools / ~12k tokens; with it Qwen-3.5-4B's 16k window got a 21.7k-token
  prompt and the request failed before the model ran. `schemas._fit_local`: prompt + tools at most 55% of a local
  window - all as they are; else all with prose shortened (`_slim`: descriptions to 200 chars, parameter notes
  and 24+-value enums dropped, every parameter kept); else Studio MCP replaces the overlapping bridge tools; else
  by `studio_mcp.priority` (execute_luau, multi_edit, insert_asset, reads first), whole tools only, and Activity
  says which were left out. 16k: 11 Studio tools; 32k: all 28 shortened; 64k: everything.
  *"Allow all like this":* the card's third button (`scope: "request"`): the same tools run unasked for the rest
  of the request (`loop._decide`, grants reset per message) - only where `permissions.request_grantable` (an
  ordinary ask: never ALWAYS_ASK/ALWAYS_ASK_WHEN, a message send, lockdown or a tainted request); the broker
  refuses the scope otherwise. Also on the voice-mode card (never by voice).
  Live (Quick effort, Qwen-3.5-4B, a temporary chat): "stack 10 parts" -> one execute_luau call with a loop, one
  approval, Workspace.Stack with 10 anchored parts at 0.5..9.5. Earlier runs in the same session found the
  window overflow, the "where?" question and the place-name note, each fixed before the next run.
- **D43 — Confirmation counted in origins; appearance families with a light/dark button; Focus, Command;
  MossadGPT (2026-09-29).** `test_corroboration.py`, `test_appearance.py`.
  *Corroboration* (`corroboration.py`, no model): a claim's support is counted in independent ORIGINS -
  a wire's own sites and any page carrying its dateline/credit count as the wire, one newsroom's domains
  (`FAMILIES`) as one, and passages sharing half their five-word runs as one (union-find). Research tags each
  finding ({single source} / {2 independent} / {confirmed: N independent}), the writer is told to present
  single-source findings as "according to", and `research.with_confirmation` ends every report with "How well
  confirmed" (before an investment report's closing line). The counts are upper bounds - a source can be shown
  to be a copy, never to be independent - and are worded that way. The saved sources carry `origin` (shown as
  "via Reuters" when it isn't the site) and `group` (every source's origin, for Focus's rail).
  *Claim timelines* (composer tool "timeline", the user's "extra capability"): `tasks._with_timelines` adds
  who reported each of the best-sourced claims first and who confirmed it later, by date, with copies marked
  "repeats X - not independent"; the conversation entry is updated so the model sees what the chat shows.
  *Appearance*: `appearance` (classic | sunrise | terminal | eclipse | aurora) x `color_mode` (system | light |
  dark), plus `terminal_phosphor`; `theme` is derived (`config.clean_appearance`) and kept for older code. A
  settings.json without `appearance` is migrated from its theme on load (never rewritten by loading); a client
  that sends only `theme` picks the family through it. Eclipse and Aurora got day versions on
  `data-tone="light"` (same skin rules and motion). data-theme/data-scheme are always the resolved light|dark,
  so Operator's console palette follows the mode too (graphite / cool grey). The top-bar button and
  Ctrl+Alt+L (also by key code, for a Hebrew layout) flip the mode within the family and never leave a secret
  theme; only picking an appearance does.
  *Looks*: Companion removed (a saved one opens as Focus, whose narrow-window rules and live window edge it
  took over). Focus gained the reading rail (`Reader`, read-only: headings, reading progress, sources with
  origins) on windows 1180 px and wider. Command = Studio's sidebar and chat + Operator's live panel open with
  every section expanded, the load chart, and Operator's density (`liveLook()`).
  *MossadGPT*: the secret theme in dark mode is `data-skin="terminal"` + `data-tone="mossad"` +
  `data-secret="mossad"` - navy palette, a tactical grid, a radar behind the chat whose sweep runs only while
  working (paused otherwise; stopped by sfx-still and reduced motion), an ops strip naming the real state
  (STANDBY / ACTIVE / AWAITING GO / HOLD / FAULT / NO LINK) with local time and UTC, and a reticle mark. No
  stars, lattice or bands (the user asked for less of the flag). Music keeps playing from day to night. The
  built-in persona follows: IsraelGPT by day, MossadGPT after dark (BLUF briefings; never claims a real agency
  or invents classified information).
  Verified live in the preview: an older "eclipse" settings.json opened as Eclipse/dark, every family in both
  halves, the Settings page, Focus's rail, Command, MossadGPT and its switch back to Judaism Mode; the user's
  settings.json was restored byte for byte afterwards.
- **D44 — MossadGPT's world monitor, Phase 1 (2026-09-29).** `test_osint.py`. The user asked for a local copy
  of God's Eye View / GodsViewAI / InteLink as a MossadGPT sub-feature the AI can query. Researched first:
  God's Eye View is MIT (ideas reused, no code copied); the other two are commercial. Decisions:
  *No middleman, no key:* each feed from its origin through netlayer (component "other"). Checked live on
  2026-09-29: adsb.lol, OpenSky (anonymous, 400 credits/day), USGS, NOAA SWPC, GDELT, CelesTrak work keyless;
  airplanes.live now answers 403 to unregistered use (dropped); NASA FIRMS needs an Earthdata login (Phase 4,
  optional). CelesTrak refuses a group it served since its last 2-hourly update (403 "has not updated") -
  `sources.NotUpdated`: keep the old orbits, ask again in 2 h (retrying sooner risks a block).
  *Only while MossadGPT is on screen:* the page's heartbeat (`/api/osint/beat`, 3-minute lease) runs
  `osint.collector`; `osint_background` keeps it on. Store: SQLite `data/osint/osint.db` - `cur` (latest
  positions), `pos` (history thinned to 1/min; none for the 20-min world snapshot), `events`; pruned by
  `osint_days` / `osint_max_mb`. Satellites: TLEs saved to `data/osint/tle.txt`, positions/passes computed
  with sgp4 (new dependency, approved; SatrecArray - all 11k in milliseconds).
  *The globe:* canvas, orthographic, no library. Land fills are clipped to the near hemisphere by walking the
  rim the way the hidden part turns (Africa+Eurasia is one ring: a straight join or the shorter arc filled
  the sea). Data times are unix seconds - comparing them with rAF's clock left satellites frozen and every
  earthquake "fresh"; fixed and tested. Click targets are computed on demand, not every frame.
  *The tool:* `world_monitor` (near/events/track/passes/status/show), composer tool "world" added by
  `_begin_composer_tools` for persona `mossadgpt` only (`schemas.world_hidden`). Live test at Quick effort
  (Qwen-3.5-4B): the first question called it; a second answered from memory with invented ISS times. Three
  causes, all fixed: the "none" mode note said "no computer tools" (small models read "no tools" - now
  "no tools that control this computer ... web and data tools still work"); the local instruction's last line
  said "answer directly" (for a world question it now says to call world_monitor - `osint.tool.turn_hint`);
  and the passes list is labelled NEXT (the model had skipped the first pass). After the fixes the same
  question called the tool twice and answered correctly with citations.
  *Voice beside the globe (same day):* in MossadGPT `Voice.applyDock` docks the voice stage left/right/bottom
  or to a pill (`voice.dock` in the browser store; full screen elsewhere, and a pending approval turns the pill
  into a panel); `Globe.voiceFocus` makes the globe the Ops view while docked and the chat column steps aside.
  Each spoken sentence, as it starts playing, goes to `/api/osint/mentions` (`osint.tool.mentions`: regions and
  cities via `geo.places_in` - longest name first, Hebrew prefixes, Russian case endings, a sentence-initial
  capital needs a city of a million - plus aircraft callsigns/registrations/hex from the store and the ISS) and
  `Globe.follow` flies to each in order, at least 350 km wide. Interrupting speech cancels the follow timers.
  The voice harness (fixtures/review/voice_ui.test.js) runs the Voice module alone: docking guards `Globe`,
  `store.get` and the orb canvas.
- **D45 — Local images; the message bar fits its box (2026-10-01).** `test_images.py`. Researched for an RTX 3060
  Ti 8 GB / 48 GB RAM, asked the user, then chose: *engine* stable-diffusion.cpp (MIT, master-929-3f8527a, CUDA 12)
  - the image twin of llama.cpp, one exe, GGUF models, an async job API (`/sdcpp/v1/img_gen`, `/jobs/{id}`,
  `/cancel`; no progress field, so progress is time against the model's last render). ComfyUI was the
  alternative - rejected as a heavy Python stack beside the app. *Models:* FLUX.2 klein 4B Q8_0 (fast, and the
  only one that edits a reference image), Z-Image-Turbo Q6_K (balanced), Krea 2 Turbo Q4_K_M (best; the user
  asked for it; a community quantization, as nobody first-party publishes a GGUF). `image_setup.py` downloads
  each from its source, streams SHA-256 against the published hash (mismatch = deleted), writes `manifest.json`.
  The CUDA runtime DLLs are copied from the PrismML llama.cpp build already on E: rather than downloaded again.
  *Speed:* E: is a hard disk: a cold load is 90-120 s, so `--mmap` (Windows' cache serves the next start),
  `--eager-load` (the load is its own step) and `--vae-tiling`. `--offload-to-cpu` was tried and removed: the
  engine's auto-fit already keeps what fits on the GPU, forcing offload doubled Z-Image's sampling (52 s vs 23 s).
  *One GPU, two models:* a 374 s render came from starting the engine while Gemma held the VRAM - auto-fit
  planned for RAM. Now: unload the chat model (`llama_local.unload_all`: the router has no "unload all"),
  wait for the driver to free it (`_wait_for_vram`), restart an engine that started with 1.5 GB less than is
  free now, and `image_gen.yield_gpu()` before every local chat call. Measured warm at 1024²: 14 / 27-34 / 55 s.
  *The turn:* `generate_image` is hidden unless the turn asks (composer tool "image": chip, `/image`, or
  `wants_image`) - like `world_monitor`. Its result is a host result (`loop._HOST_RESULT_TOOLS`): a small model
  copied the UNTRUSTED frame into its reply, and read "generated" as "made last turn"; the message now says the
  image is for this request and the app shows it. `agent.tasks._with_images` appends `![model - seed](/api/images/<id>.png)`
  lines; `renderMarkdown` draws only that local form. A real bug found on the way: under Local · Auto the tool
  schema was sized for the pinned model, not the one `_fit_for_local` chose (Gemma 16k refused a 25k request) -
  `local_planned` is now read first. *Filter:* minors + sexual content always refused; `image_filter` (on by
  default, the user asked for it switchable) adds real named people + sexual content and sexual violence; words only.
  *The bar:* `.composer` is a size container; labels drop at 700/540/420 px, the row wraps only below 360 px with
  the model and Send pushed right. Measured in the preview: nothing outside the box in Studio, MossadGPT, its Ops
  column (325 px) and a 375 px phone.

- **D46 — The revamp: images made fast, a shell with places, tools it picks itself, careful mode (2026-10-01).**
  `test_images.py`, `test_agent_features.py`, `test_revamp_ui.py`. *Images:* measured on the RTX 3060 Ti with
  ~2.3 GB held by other apps: per-request `easycache` cut Fast 42 -> 13 s (same picture) and Balanced 89 -> 34 s;
  Best turned grainy with it, so `step_cache` is off for Best (39 s). `--max-vram -<reserve>` plus one retry with
  1 GB more after the engine's "cannot make enough memory available" ended the out-of-memory failures; progress
  and the time left are read from sd-server's log (it has no progress field); Stop kills the engine
  (`cancel_generating` is false in master-929). A background warm-up reads the model's files sequentially when the
  Image tool is turned on (a cold HDD start was 248 s of mmap random reads; sequential is ~150 MB/s). A gallery
  with favourites, a trash that restores, Again / Variations / Edit, 1-4 pictures a request, and a Move dialog that
  copies the models to another drive, checks each SHA-256 against the manifest and only then switches the folder
  (the old copies stay). *Shell:* the sidebar has places (Home, Images, Projects, Routines - Alt+1-4) and This PC;
  the composer has +, Tools (searchable), the context ring and one Setup button (model / effort / persona); the
  Workspace panel has tabs; Settings has search and groups; the top bar's More holds rename, find, pins, branch and
  export. *Auto tools* (`auto_tools.py`): a deterministic pick before the turn turns on read-only tools only -
  grounding, charts, market data, an image - never code or anything that changes the PC; `use_tools` lets the model
  ask for one on questions. Web lookups need both web search on and `web_auto_search`. *Charts* (`stats_tools.py`):
  `compute_stats` / `make_chart` work on given numbers or an attached CSV and render as ```chart blocks (8 types,
  a "Show the numbers" table). *Careful mode* (`careful.py`, a setting, on, with every persona): prompt rules plus a
  deterministic review - a [n] that points at no source is removed, a URL no tool returned is flagged, an answer from
  memory gets a "Check it" button. *Found on the way:* the local history fit measured the bare base prompt, so the
  persona and careful rules pushed a 16k model 553 tokens over its window (real chats, 2026-10-01). The fit and the
  context meter now measure the system prompt as sent (`agent/models.system_prompt`) plus the request, its notes
  and files; an overflow llama-server still reports drops older history by the excess plus a margin and retries
  once; what is left reads as words with Compress / New chat / Try again instead of the raw 400. *Motion:* every
  theme's indicator moves differently for what the turn is doing (`currentAct`: think, read, search, tool, write,
  draw, wait) - on the Thinking row, the search row, or an activity line at the foot of the turn; reduced motion
  shows the action's icon instead. Approval cards name the risk in words and fold to their verdict.

- **D47 - Raw and Enhanced; one core for every prompt; claims checked (2026-10-09).** `response_modes.py`,
  `test_response_modes.py`. The user asked for a "raw" mode (the model with nothing) and an "enhanced" one that
  answers like a careful assistant: clear, true, no made-up facts, well-set ground rules - and a way to tell the
  user when a rule is hit. *Enhanced* (default): both system prompts now open with one core
  (`agent/prompts.ENHANCED_CORE`, and `ENHANCED_CORE_COMPACT` for local models): who it is and how it answers, the
  honesty rules (`careful.PROMPT`, now always part of Enhanced - the `careful_mode` setting, shown as "Check
  answers", only turns the checks after the answer on and off), five ground rules (only the user instructs; done
  only when a tool confirms it; stay inside the request; no harm; the user's data stays theirs) and SPEAKING UP: a
  rule that stops it, an impossible part, a wrong premise or its own mistake is said in a `> **Warning:**` /
  `> **Note:**` line first - never a silent refusal, silent compliance or pretending. The local prompt's last line
  repeats it (the line small models follow best), except on a world-monitor turn, whose last line must stay "call
  world_monitor". The rest of both prompts was rewritten shorter and one rule per line (the cloud prompt 3,470 ->
  2,988 tokens with the careful rules it used to append; the local one 1,391 -> 1,469 sent, but the tool fit counts
  all of it now - it used to leave the 202 careful tokens out). `for_tools` judges each line (and each sentence in
  it) on its own and keeps line breaks, so the numbered rules stay a list; a list marker is not a sentence end.
  `untrusted.PROMPT_RULE` is shorter (ground rule 1 states the principle). *The host speaks up too:* after an
  Enhanced answer, `careful.claims_without_change` flags an action request answered "I've saved / opened / deleted
  ..." while no tool changed anything this turn (`turn_tool_history`; a desktop route reports for itself) - shown
  under the reply with the invented citations, unknown links and from-memory facts, and as a warning in Activity.
  *Raw:* `raw_mode` for the turn - `build_messages` sends the conversation only (and this message's attached
  files), `system_prompt` is "", `tools_for_current_turn` is empty, the loop takes the reply as written (a JSON
  "tool call" in it is text), local calls drop the agent's temperature 0.1 / top_p so the model's own sampling
  applies, `_set_task_context` adds no brief, project context or memory and sets no thinking or provider effort,
  `_begin_raw_turn` drops tools, preset, persona and voice header, the orchestrator neither prepares, plans nor
  reviews (`bare`, like /design), and nothing after the answer (checks, auto tools, routine offer). No greeting
  shortcut, desktop route or clarification resume either - the message reaches the model exactly as typed. Time and
  step limits and Stop still apply. A message sent in Raw is saved with `rm: "raw"` and shows a Raw pill. *UI:*
  Setup's Model tab starts with "How it answers" (Enhanced / Raw); the Setup label and the line under the message
  box say Raw; Settings > Agent > How it answers has Response mode and Check answers.

- **D48 - The redesign: results, not machinery; one place per fact; calm by default (2026-10-10).** The user found
  the app "sloppy" and "fake" (audit: UI_UX_AUDIT.md, top). Agreed: audit -> mockup (the user's Design canvas
  "Workspace redesign", on their Agentic Workspace design system) -> build; clean look, Aurora kept. *Look:* the
  Classic appearance (the design system's paper / warm charcoal) is the user's look again - set in their
  settings.json, not by a migration; every appearance stays. *Shell:* the panel is closed until opened
  (`panelOpen2.<look>`; the console looks still open it) and has three tabs - Activity (suggestions inside it, only
  when there is one), This chat, This PC (All only in Operator/Command); the sidebar's This PC box is one model row
  (`paintModelRow`: the model and what happens next - "Ready · GPU", "Loads when you send") plus the image engine
  and Roblox Studio only while they are busy or connected; the top bar's speed meter is off by default
  (`net_indicator` false); the hint under the box says what the permission settings really promise
  (`approvalPromise`); the context ring shows from 50%; the Setup label leaves out Balanced. *Home:* four starters in
  one row (the observer's chips first, `DEFAULT_CHIPS` can set up the box) and Continue - the sidebar's own chats,
  newest first, the open one included (it used to say "No saved chats yet" beside one); no Talk orb (the composer's
  microphone), no empty cards. *Chat:* a greeting goes to the model (the canned "Hi! What would you like me to do?"
  read as fake; stale state is still cleared first); under a finished reply `buildWorkLines` shows one line per
  change or failure (with Undo from the event's `undo_id`), one line for what it only looked at, and Details for
  the full list; planning/review rows are off in the chat (`show_activity` false - always in Activity); Prompt
  Prep's silent Clarify notes no longer sit under the message (only a plan or spec you approved); approval cards
  fold the raw arguments when a preview of the change exists and drop the generic context line; the live steps
  don't repeat "waiting for approval"; a turn where only a deterministic desktop route ran is not model-reviewed
  (it repeated the route's own result as "issues"); the executor's "[status: ...]" line no longer reaches the
  user's message; `careful.claims_without_change` also catches "Saved your notes..." without an "I". *Settings:*
  six pages - General, Assistant, Models, Permissions, Voice and images, Connections - each part of a page a tab
  at its top (`SETTINGS_PAGES`); Assistant and Appearance rewritten to one line a row, the rest under "More
  options"; web search modes as one-line cards with the full text folded below. *Judaism Mode:* switchable from the
  Persona menu once found and with Ctrl+Alt+J (and the palette); by day calm at rest (still lattice, the flag's
  bands on the window's edges, the motes, sweep, turning edge and spinning mark only while it works); MossadGPT's
  chips without tiles, the Ops view's layers panel folded (`globe.layers2`), Home fits its column; one theme button
  (the theme's own menu); IsraelGPT and MossadGPT personas rewritten shorter, to sit on the Enhanced core. *Fixed on
  the way:* `span.tier.input` drew as a text field (the `.input` class); the diagnostics toast came back on every
  start (now once per change). Checked in Chromium against a stand-in model: Classic light/dark, Terminal, Eclipse,
  Aurora, Sunrise, Judaism Mode and MossadGPT (+ Ops view), 390 px wide.
- **D49 - The D48 leftovers: an answer is a work line; every settings page one line a row; a quieter Ops HUD
  (2026-10-10).** *Approvals:* an answered card folds into the same row its change gets under the reply ("Write
  notes.txt · you allowed it", `settleApproval` builds a `.work-line.ap-done`; "What was asked" opens the card
  again), styled last in the stylesheet so no theme's `.card` rules bring the card back. The answer event carries
  `asked` (the step words of each action, `events.describe_tool`) and each card's view a `brief` with the same
  words; `buildWorkLines` pairs the answers with the tools that ran (`approvalAnswers`): an allowed change says
  "you allowed it", a declined request keeps its own line ("Delete old.log · you said no" - older chats name the
  tools). `agent/sessions._slim` now keeps `undo_id` (a reopened chat lost its Undo buttons) and `asked`.
  *Settings:* Proactive help, Permissions, Memory, Privacy, Voice, Images, Web search, Network, MCP, Models,
  Diagnostics and General rewritten to one short line a row; what most people never change folds under "More
  options" (Proactive: what it looks for and reads, muted; Permissions: each tool and messaging - with how many
  tools are set differently; Voice: quick replies, captions, sounds, pause, compact model, memory; Images: step
  cache, room for drawing, licences; Web search: research, finance, history and cache; Network: each part's route,
  the speed indicator, the connection log). The per-tool list leads with the approval card's words
  (`/api/permissions/tools` sends `label`), the tool id small, tools set differently first; the web search mode
  cards have their own short lines (`SEARCH_MODE_SHORT` - the first sentence was cut mid-word); settings sliders
  and checkboxes take the theme's accent. The image filter still says that anything sexual involving minors is
  always refused and can't be turned off. *Ops HUD:* short layer names (the full name, its source and an error on
  hover), "failed" in the warning colour instead of a red "error" on every row and a count in the panel's title,
  one-line reasons, a shorter footnote; Rewind is one title row (Live, or the time and how long ago, with a Live
  button only while rewound) and a one-line note; the readout leaves out the clock unless rewound (the top bar has
  it); on a phone the layers take the width. *Fixed on the way:* Diagnostics said a missing model file twice; the
  `span.tier.input` dot still stretched in a list row (`.input`'s flex: 1).
- **D50 - A small start context, interactive replies, a built-in browser, models on the SSD (2026-10-10).** The
  user's meter read 11,027 tokens of instructions and tools before a word, and 100% with no model loaded; they
  wanted ChatGPT/Claude-style interactive replies chosen by the model, the assistant's browser inside the app instead
  of an Edge window, and their main models on the A: SSD. *Start context:* skills fade - one nothing picked, loaded or
  used for `skills.FADE_AFTER` (4) requests leaves the chat's list (a chat that once searched carried the web tools
  forever); with Studio's bridge connected Roblox is the default when nothing else matched, or comes with Studio's
  words (`_RX_STUDIO_ON`) - it used to join EVERY request; MCP tools are skills: Roblox Studio's MCP is `studio`
  (Studio's words, load_skill, or a tool of it used), any other server's `mcp` (its name, or "mcp"); the index line
  offers both. A fresh chat's meter: about 3,000 tokens. The meter no longer measures against a model that isn't
  loaded and isn't pinned (`context_window.status(known=False)`: the last auto-picked 16K model, long unloaded, read
  100%). *Interactive replies* (index.html "Interactive replies"): charts answer the pointer (a guide and each
  series' value) and their legend hides a series; a ```chart of `"type": "function"` plots formulas with a slider
  per parameter - `MathExpr` compiles them without eval (own names only), the axes hold while you drag and only
  grow; a ```widget is the model's own small page in an iframe with `sandbox="allow-scripts"` (no same-origin), a
  CSP with `default-src 'none'`, the theme's colours as CSS variables, its height reported by postMessage (nonce,
  source checked) - and only once the reply is complete (`MD.live`); a ```choices block is buttons that send the
  option; every table sorts by its column (keyboard too) and filters past 8 rows. The model is told about them only
  when it helps: the composer tool `visuals` (auto_tools: explanations, comparisons, formulas; `use_tools` can turn
  it on), plus one line on ```choices in both prompts. *Built-in browser* (browser.py, `browser_builtin`, on by
  default): the assistant's browser runs headless (`--headless=new`, 1280x860), each page session sends the
  browser's name without "Headless" (bot checks refuse it); `browser.Viewer` - its own DevTools session on the
  page the assistant works in - gives the Browser panel (sidebar "Browser", or by itself when a browser_* tool
  runs, unless you closed it this run) a JPEG of the page (`/api/browser/view/frame`, 204 when unchanged, the
  state in headers) and takes YOUR clicks, wheel, typing and keys (`/api/browser/view/input`) and the address bar
  (`/nav`: words become a search; never this app's pages). The page is laid out at the panel's own size
  (Emulation.setDeviceMetricsOverride) so the picture is 1:1. The assistant's own clicks and fills keep their
  approvals and refusals; "fill it in yourself" now points at the panel. *Models:* Ornith 1.5 9B and the Fast and
  Balanced image models (with the engine) moved to `A:\ClankerLocalCpp` (copied through the device bridge, checked
  against their published SHA-256 - Ornith's from ornith-ai/Ornith-1.5-9B-GGUF - then deleted from E: as the user
  chose); `image_root` A:, `image_extra_roots` E: (Best stays there); models.ini points Ornith at A:.
  `model_assets.py`: a known model's vision projector (Ornith: bartowski's mmproj-Ornith-1.5-9B-f16.gguf, 918 MB,
  SHA-256 pinned) downloads through the network layer when asked (Settings > Models row), is checked, and only
  then gets its `mmproj =` line beside the model's line (the file kept as models.ini.bak-<time>); Hugging Face was
  blocked from Claude's side, so the app fetches it.
- **D51 - The newest llama.cpp by default, PrismML's fork only for the models just it loads (2026-10-10).** The
  user asked for the new llama.cpp. Installed: upstream b11541 (Windows CUDA 12.4, Oct 10) in `E:\ClankerLocalCpp`
  and PrismML's prism-b10770-6684606 (Oct 9) in `E:\ClankerLocalCpp\prism`, both checked against GitHub's
  SHA-256 digests; the old builds (stock c1d0e7a of Aug 25, prism b10735) are kept in `E:\ClankerLocalCpp\_old`.
  The CUDA 12.4 runtime DLLs stayed (the same files). PrismML's newest release still sits on upstream's code from
  Aug 25 (GitHub compare: 925 commits behind), so running it for everything - D29's "prism first" - would have
  thrown the update away. The router runs every model with its own executable (`server-models.cpp`: the child's
  binary path is the router's; a preset has no key for another one), so the build is chosen per router:
  run.bat and launcher.pyw now start the newest build, and `llama_builds.py` (setting `llama_build`: auto | latest
  | prism, Settings > Models > llama.cpp) restarts the router on PrismML's fork when a model only it loads is
  about to run (`llama_local.needs_prism`: PTQ1_0 / PQ2_0, Ternary Bonsai 2), and back on the user's next pick of
  another model. The running router is found by psutil (llama-server with `--models-preset` on LLAMA_PORT; else
  the /models status args); a switch kills it with its model servers (`taskkill /T`) and starts the other build
  with the old router's own arguments (else run.bat's), the way the old one ran: a minimized console window of its
  own under run.bat, windowless with data/logs/llama-server.log under launcher.pyw (`AGENTIC_WINDOWLESS=1`), and
  then stopped on app exit as the launcher would have. A build that doesn't answer in 60 s brings the old one
  back and the turn says why. Only the user's pick moves the router back (`call_local`, force-load); helper calls
  (`model_text` with another model, the eviction step of Reload) only ever move it TO the build a model needs, so
  nothing ping-pongs. The chat's loading row reads "Starting PrismML's llama.cpp for <model>" meanwhile. Checked
  before switching: every models.ini key is still an argument of b11541 (`ngl` included - preset keys are any
  argument spelling), and /models still reports `status.value`/`status.args`; newer routers also report
  `architecture.input_modalities`, so "image" there now counts as a vision model too. Ajax (PewDiePie's model) was
  NOT downloaded: it isn't released (data.pewdiepie.com: "I've decided to release Ajax when it's ready"); the
  GitHub repos named for it ship an `Ajax-Desktop-Setup.zip`, a malware lure.

- **D52 - Selectable base system messages for text and voice (2026-10-12).** The user's shorter message is
  the Compact default, with corrected verification scope, justified retries, batching, follow-up continuity,
  long-file append and source-page reading. system_messages.py holds pure templates, validation, rendering and
  metadata. Detailed extends Compact; Original retains agent/prompts.py's old local/cloud constants unchanged;
  Custom replaces only the base instructions, supports {NAME}, preserves literal JSON braces, and leaves the
  existing persona injection and host permission controls intact. Built-in tool guidance still uses for_tools;
  custom instructions do not pass through that filter, because it could otherwise remove a user's restrictions.
  agent/models.system_prompt resolves the chosen message, so history fitting and context display measure what
  is sent. agent/tasks captures a selection for the complete turn and clears it in finally, including failures;
  changing settings during a reply affects the next turn. Raw and the existing specialized task prompts stay
  separate. Settings > Assistant > System message provides preset previews, Edit a copy, a custom draft,
  assistant name, Save/Cancel and a sticky action bar. Drafts survive preset changes and stay local until the
  settings API confirms saving. Oversized/empty custom saves are rejected before mutation. settings.json uses
  temporary-file replacement; failed system-message writes preserve the prior in-memory and disk selection.
  The real settings file was not migrated or touched by tests. Defaults apply on restart. Tests cover API
  persistence and failure, per-turn consistency, custom restrictions, voice language headers, persona and context,
  editor races and escaping. Browser verification used fixtures/review/system_messages_preview.py (real CSS and
  controller, memory-only settings, no app startup/services), across 13 palettes and all three motion settings
  at normal and 390px widths. No real-model quality or microphone claims; cutoff-controller defects are separate.

- **D53 - Context and effort controls match the workspace (2026-10-12).** The context popover has an
  explicit bounded width, aligned definition-list metrics, separate wrapping model metadata, three token
  segments when a summary exists, and compression status in its own full-width row. Automatic thresholds
  expose selected/disabled states; settings must confirm before the selected threshold changes. In-flight
  saves reject duplicate requests and keep keyboard focus across poll redraws. The Effort and Prompt Prep
  menus use native ranges with direct named choices, theme tokens and brief interaction transitions. Auto
  is separate from the fixed effort scale. The particle canvases, spring/detent simulation, animated text,
  glowing max badges and theme-specific slider sweeps were removed. Ranges preview a drag without saving;
  release commits the nearest supported stop, cancel restores the confirmed choice, and keyboard movement
  skips unsupported stops. The existing per-model/session/project persistence and capability fallbacks stay.
  Verification: 13 new event/network regressions and isolated real-browser checks of the actual markup/code,
  all 13 palettes, three motion settings and two widths (156 combinations), plus 320px state stress checks.
  Recorded layouts and screenshots are under fixtures/review/ui-remake/. No app startup/model/microphone
  calls or live settings/data writes were needed. Physical pointer dragging still needs a user check; its
  commit/cancel/poll lifecycle is covered by automated event tests.

- **D54 - Codex-style stepped sliders (2026-10-12).** The user rejected D53's native-range plus button-grid
  design and explicitly chose a slider rather than dropdowns. Effort and Prompt Prep now use an unboxed,
  compact layout with a thin neutral rail, discrete stops, one selected name beside the label and endpoint
  captions. Auto is a separate quiet control, outside the fixed scale. Budgets, source labels, routing and
  session overrides fold under More options; the settings footer uses its full available text width.
  Descriptions follow drag previews; polling cannot reset the preview; keyboard navigation skips unsupported
  stops. Escape and pointer cancellation restore the confirmed level. The existing generic save helper
  optimistically updated settings and swallowed errors; these two controls now call the settings/session API
  directly, validate the returned selection, disable duplicate requests and only then update their state.
  Replies for a previously selected model cannot relabel the new model. Existing choice IDs/names and
  per-model/project/session behavior are preserved. No backend, system-message or context changes in this pass.
  Verified: 18 context/effort tests, 56 Node UI checks, 80 guarded Python checks with zero protected writes;
  78 isolated browser layouts across 13 themes, all three motion settings and desktop/390px widths, without
  overflow or header overlap. Interaction transitions: Full 160ms, Reduced 60ms, Off 0ms. Browser checks
  confirm keyboard selection, pointer click, Auto, unsupported levels and failed saves. Physical held-pointer
  dragging with the live app remains unverified; automated events cover its lifecycle. Evidence is under
  fixtures/review/ui-remake/codex-slider-checks.json and codex-sliders-*.jpg. D53 context fixes remain current.

- **D55 - Cross-check is independent of effort (2026-10-12).** The user asked to move Cross-checked
  out of the slider. The stored `max` ID now displays Max; numeric budgets are unchanged. Max/Workflow
  perform primary-model review. A separate `cross_check` boolean defaults Off and saves atomically via
  /api/settings. The switch appears in Setup > Effort and Settings > Agent; failed/unconfirmed/duplicate
  saves preserve the previous value. Effort and Cross-check never change one another.
  Cross-check is snapshotted for each run and applies to eligible Enhanced requests, independent of effort.
  Raw, trivial, rejected and internal-only routes retain their existing review exemptions. /api/effort
  reports availability. Local mode is unavailable because the backend hosts one model at a time; cloud
  selection requires a configured available model different from the actual executing model (not merely
  a different API key). An On preference can be saved while unavailable, with an explicit explanation.
  Failed/unavailable/malformed second reviews emit status events and an explicit reply note. A blocked
  second verdict cannot become a successful verification. Tests exercise the real settings API, runtime
  profiles, step/tool/time/context limits, thinking and request token budgets, session overrides and safe
  mid-run effort changes using scripted model replies, with isolated state and protected-write auditing.
  Verified: 52 guarded Python checks, 59 Node UI checks (21 context/effort), 78 browser layouts across
  13 themes x Full/Reduced/Off x 1440/390px; expanded On at 320px, keyboard effort independence and failed
  toggle recovery. No overflow/header overlap or browser errors. Evidence: cross-check-layouts.json and
  cross-check-phone.jpg under fixtures/review/ui-remake. Root/static HTML identical, CRLF/no BOM; live
  settings SHA256 unchanged, protected-write attempts zero. Restart backend and refresh UI to load it.
  No Max animation, second local-model runtime, live model quality/speed benchmark or live cloud review
  was added/tested in this pass. Earlier D54 screenshots remain historical evidence for the slider remake.

- **D56 - Claude-inspired effort polish and honest runtime semantics (2026-10-12).** The user supplied
  Claude Medium/High/Ultracode references and asked for satisfying animated sliders. Thin circular rails
  became fuller rounded tracks with rectangular visual thumbs, six stops, Faster/More thorough captions,
  default/use-case guidance, a selected label next to Effort and matching Prompt Prep. Native ranges retain
  keyboard/touch semantics; smooth CSS thumb/fill transitions, press/focus treatment and brief label
  feedback make changes readable. Polls preserve previews and do not recreate/restart the selected label.
  Workflow alone displays a CSS pixel wave/sweep. Classic uses contrast-checked lavender; other themes
  derive the tint from their accent/info colors. Reduced, Off and OS reduced motion stop the animations;
  a static pattern remains. No JS loop, canvas, forced-layout animation restart, new dependency or download.
  Animation reflects a supported fixed Workflow selection; fallback, Auto and failed-save restoration
  cannot display it as active. The independent Cross-check switch and confirmed-save behavior remain.
  Runtime audit found Workflow's declared stage_caps/delegation were never consumed. Its profile now
  honestly reports orchestration='none'/delegation=False instead of pretending to enforce stages. Detailed
  planning, model review, larger budgets and existing 32K/structured-call/measured-reliability gating remain;
  gating now keys off the Workflow ID. A saved zero reasoning_tokens override was also being replaced
  by the legacy reserve in _local_reply_tokens; zero is now respected. Legacy no-budget behavior remains.
  Verified 53 guarded Python checks (real API/pipeline with scripted replies; all six profiles, override
  payload, safe-boundary changes, cross-check failures), 62 Node checks (24 context/effort), 468 browser
  states (six levels x 13 themes x three motion settings x 1440/390px). No horizontal overflow/header
  overlap; highest-label contrast >=5:1 across all palettes. CSS transforms/opacity change in Full, none
  in Reduced/Off; failed/fallback choices stop the effect. Expanded On at 320x640 scrolls vertically within
  the menu without sideways overflow. No browser errors. Real settings/data untouched; HTML identical,
  CRLF/no BOM. Evidence: polished-effort-checks.json, polished-effort-animation.json, polished-*-phone.jpg
  under fixtures/review/ui-remake. Restart backend and refresh UI. No live model quality/speed improvement
  was measured; enforced stages/delegation and physical held-pointer dragging remain outside this pass.

- **D57 - Contained braided energy and continuous slider feedback (2026-10-12).** User reported an
  outside dot, asked for a clearer but lightweight effect distinct from Claude, and movement with feel.
  Rail decoration is clipped and endpoint stop centers inset 3px; the visual thumb and focus ring remain
  siblings outside the clip. Max and Workflow now use two repeating SVG-mask filaments moving in opposite
  directions and a travelling charge. Max is slower/warm, Workflow stronger/themed. This replaces D56's
  dotted texture. Only small transform/opacity layers animate; no JS animation loop, blur or new assets.
  Every changed stop gets a finite directional impulse (also Prompt Prep), with smooth thumb/fill motion.
  Runtime settings remain unchanged. The real interaction bug was paint(target) -> save -> sync(cur),
  which sent the thumb backwards during confirmation. S.levelPending now keeps a separate visual target
  bound to mode/model. Saving text/ARIA name the active applied effort; active settings only change after
  confirmed save. Polling does not reset or restart the feedback. Failed saves restore the actual selection;
  changed models ignore pending visuals from the previous model. Auto/fallback cannot activate energy.
  Reduced/Off/OS reduced motion stop the filaments, charge and movement impulse; static filaments remain.
  Verified 66 Node checks (28 context/effort), deferred saves/polling/failure/model switch, Auto/fallback and
  Cross-check independence; 468 browser states (all six stops x 13 themes x three motion settings x two
  widths), no outside dots, overflow/header collisions or browser errors. Computed fill/thumb positions
  interpolate and filament transforms change. Evidence: energy-layouts.json, energy-motion.json and
  energy-*-phone.jpg under fixtures/review/ui-remake. Root/static HTML identical, CRLF/no BOM; live settings
  preserved. Refresh the page, Full motion for effects. Physical held-pointer dragging/hardware performance
  and OS reduced motion are unverified; backend/model behavior unchanged in this pass.

- **D58 - Continuous pointer motion and aligned pixel sliders (2026-10-12).** The user asked to return
  closer to the pixel effect, remove distracting per-move effects and make the selector follow the mouse
  before reaching a stop. Native step=any now preserves fractional value/--pos during input. Preview labels
  stay discrete, polls preserve position and no settings are saved while dragging. Release finds the nearest
  supported stop by raw distance (ties prefer the rounded preview) and settles/saves via confirmed flow.
  Keyboard starts at integer stops above/below the raw position, skips unsupported choices and clears drag
  treatment. Drag ownership is bound to mode/model; polling/input/release cancel a stale context before it
  can save to a newly selected model. Escape/pointercancel suppress late change. Pending-save visuals remain.
  Removed braided masks/charge, travel impulse, label bounce and hover/press scaling. Max/Workflow use two
  small calm pixel layers, stopped in Reduced/Off/OS reduced motion. No animation timer, RAF or dependency.
  Alignment now uses a common rail/input/caption width and shared 24px native/visual handle geometry. Stop
  centers use the same inset/travel equation as the handle; endpoint handles/dots are inside the rail.
  A header grid aligns title/value/Auto. While dragging, handle position has no transition delay; only
  release/keyboard uses a gentle 200ms settling transition, shortened/disabled by app motion settings.
  Verified 70 Node tests (32 context/effort): fractional Effort/Prep input, polling, supported-neighbor raw
  distance, ties, keyboard, owner changes, cancel and deferred saves. Actual browser pointer drag generated
  trusted fractional input events while Quick stayed active; release selected Reviewed at index3. 468
  states (six stops x 13 themes x three motion settings x two widths) passed caption/native/stop alignment,
  containment, no overflow/header collisions, removed name animation and quiet Reduced/Off behavior.
  Evidence: continuous-layouts.json, continuous-pointer-drag.json and continuous-workflow-phone.jpg under
  fixtures/review/ui-remake. Input tracing is preview-only, memory-only; no production telemetry was added.
  Root/static HTML identical, CRLF/no BOM; current live settings preserved. Refresh UI. Live app/OS-level
  reduced-motion on actual hardware unverified; browser-native pointer gesture is verified.

- **D59 - Independent continuous-slider audit and stable status alignment (2026-10-12).** The current
  files already contained D58's continuous step=any pixel refinement. An independent review reproduced
  two gaps: cancelSliderDrag cleared ownership so a later input could paint a stale preview, and real
  refreshEffort unconditionally applied replies/errors after a model switch or newer poll. A cancelled
  input now restores confirmed controls until a new gesture/explicit key action re-arms it. Refreshes
  capture mode/model and a monotonic generation, ignoring both stale successful replies and errors.
  Added four regression groups using the shipped real refresh function, deferred responses and cancelled
  event sequences. Continuous movement, discrete release/keyboard selection, pending save semantics,
  independent Cross-check and the calm two-layer pixel texture remain. No per-move flash or bounce.
  Headers now share 86px label / flexible name / 36px Auto columns. A 37px minimum header replaces the
  old caption gap: fallback status no longer moves the track by ~15px. Confirmed equal settled track top
  with/without fallback and 320px fallback fit. Rail/native/visual handle/caption geometry remains matched.
  Verified 74 Node checks (36 context/effort), 468 fresh browser states across six stops x all 13 themes x
  Full/Reduced/Off x 1440/390px, shared value alignment, rail/caption alignment, contained stops/handles,
  no horizontal overflow or browser errors. Re-ran a real native pointer gesture after final CSS changes:
  trusted fractional positions throughout, active Quick unchanged during drag, Reviewed chosen on release.
  Evidence: aligned-continuous-layouts.json, aligned-pointer-drag.json, aligned-fallback-geometry.json and
  aligned-continuous-workflow.jpg under fixtures/review/ui-remake. Root/static HTML identical, CRLF/no BOM;
  settings preserved from task start. No backend/model edits or downloads. Actual-app subjective feel and
  hardware OS reduced-motion preference remain unverified; app settings and browser gestures checked.

- **D60 - Snappy direct sliders and stationary pixel shimmer (2026-10-12).** User rejected D59's
  slow drifting/breathing texture and asked for online research and Claude video references. Apple HIG
  supports live slider feedback; Material uses fast motion for small components and restrained effects;
  Google advises opacity/transform for continuous CSS animation. Inspected the Claude mobile Ultracode
  recording and an independent desktop-inspired recreation. A YouTube effort demo shows the terminal
  picker, not the desktop effect; exact desktop footage is not claimed. Research links and scope are in
  fixtures/review/ui-remake/slider-research.md. New masks are generated locally; no third-party code copied.
  Fixed square cells replace the translating circular wallpaper. Five sparse irregular masks phase opacity
  independently, with the same cycles across Max/Workflow and stronger Workflow emphasis. Width follows
  the filled selection, so no pixel appears past the handle. Native pointer tracking remains immediate;
  release/keyboard settle shortened from 200 to 120ms. Dots remain 3px instead of becoming bars as the
  label changes; highest levels hide markers beneath the texture. No per-move sweep/flash, canvas, timer
  loop, blur, new dependency, model change, or download. Reduced/Off and OS reduced-motion keep cells static.
  Fractional inputs still paint raw geometry every time, but full description/limit synchronization happens
  only when the rounded preview changes. Cheap accessible text is checked each input so same-owner
  capability updates cannot leave an outdated availability announcement. Two new regressions verify DOM
  write counts, immediate movement, support changes, cancellation, and no premature save. D59 owner,
  polling, pending save, fallback, keyboard and independent Cross-check semantics remain.
  Verified 76 Node checks (38 context/effort), 468 fresh theme/motion/width/stop browser states, no console
  errors, aligned headers/rail/captions, contained handle/dots and matching energy/selection extents.
  Actual trusted browser drag produced fractional values while Quick stayed active; release selected Max.
  Pixel opacity changes over samples while transform stays none. Evidence: stationary-layouts.json,
  stationary-drag.json, stationary-motion.json, stationary-workflow.jpg. Root/static identical CRLF/no BOM;
  settings hash preserved from task start. Refresh UI. Actual-app subjective feel/hardware FPS unmeasured.
- **D61 - Chat limits: refuse, then close the chat (2026-10-13).** The owner's terms: refuse sexual content
  involving minors (always on), weapons capable of mass casualties, malware/attacks on others' systems; answer
  self-harm with care (never counted, never locks). `chat_guard.py`. Detection is "rules + model check": the
  image filter's own patterns flag a possible minors case; otherwise every message of 12+ characters that isn't
  a greeting gets one short classification call (`agent.models.model_text`, 24 tokens, temperature 0) to the
  model in use, with the previous user message for follow-ups. No topic word lists for the other categories -
  the model judges intent, and the policy text lists what stays allowed (fiction, news, own-system security,
  CTFs, adult content between adults). A failed/unreadable check allows the message, except a minors rule hit
  (fail closed). Screening runs in `agent/tasks._screen` before mark_pending/any model step; a refusal is saved
  to the transcript but never enters the conversation. Strikes live on the chat (`guard`, kept by chat_store
  KEEP_KEYS); `strike_limit` (3) refusals - or one minors refusal - lock it. Locked: /api/chat refuses, message
  actions other than pin refuse (delete/retry/branch would escape), a locked temporary chat can't be saved; the
  page shows a closing banner and disables the shared message box only in that chat (Home still starts a new
  chat). Owner override, deliberately quiet: double-click the banner's lock icon -> passphrase dialog
  (`POST /api/chats/{id}/unlock`); Settings > Privacy > More options sets/changes the passphrase and the
  optional limits (`/api/guard/*`). Passphrase = salted PBKDF2 (200k) in data/chat_guard.json; 5 wrong tries
  per 10 minutes. Forgetting it: delete that file (limits return to defaults). Costs ~1 s per screened message.
  NOT verified with a real local model: classification accuracy/false refusals of the 9B/35B models.
- **D62 - Local video (2026-10-13).** `video_gen.py`: sd-cli -M vid_gen (the image engine's own build) with
  Wan 2.2 TI2V 5B Turbo (step+CFG distilled, Apache-2.0) Q5_K_M, umt5-xxl Q5_K_M, Wan 2.2 VAE and the taew2_2
  tiny decoder (MIT), on G:. One clip at a time; the image engine and chat model are freed first. Timings and
  why auto-fit (--max-vram -1.5 --mmap) instead of --offload-to-cpu: video_gen.MODELS comment (1135 s -> 22 s).
  Gallery data/videos (webm + json), trash, restore, purge/empty (user click only). Videos place in index.html
  (VideosView) reuses the Images layout; the reference picker lists Images' pictures. Images and Videos trash
  gained Delete forever / Empty trash (image_gen.purge/empty_trash). test_videos.py (sd-cli mocked).
- **D63 - Media pass (2026-10-14).** Measured choices, details in the module comments:
  anime video = 5B Turbo + anime LoRA (vs Wan 14B I2V, which blurs without its Lightning LoRA - sd.cpp 959 and 964
  both crash with a LoRA on the low-noise model - and AnimateDiff, soft); the tiny decoder's colour flicker
  (per-frame saturation 0.17-0.26 vs 0.17-0.20) made the full VAE the default. image_plan.py: planning in Pony tags
  with code-side rules a small planner kept breaking (danbooru counts, no "solo" with 2+ people, negatives that
  contradict the request are dropped). Frames all start from frame 1 (chaining compounded colour). MTP on Ornith
  9B: +34% at 8K context but 7-9 tok/s at 65K (VRAM spill), so only an optional 32K preset has it. models.model_text
  gained `images=` for vision models. Audio/music run as separate processes in their own envs (PyTorch versions
  differ); nothing of them loads into the app.

## Data directories (all under `data/`, git-ignored)
| Path | What | Retention |
|---|---|---|
| `chats/<id>.json`, `chats/trash/` | saved chats (transcript + model conversation, no screenshots) | until deleted; trash kept |
| `images/<id>.png`, `<id>.json` | made images and their prompt, model, seed (image_gen.py) | until deleted |
| `projects/<id>.json`, `projects/trash/` | projects | until deleted; trash kept |
| `index/<hash>.json` | folder index cache (paths, sizes, languages, README head) | rebuilt after 5 min |
| `session.json` | which chat and project were open | overwritten |
| `memory.json` | saved memory (D17) | until edited |
| `undo/` | undo backups + manifest (D6) | 300 changes / 14 days / 2 GB |
| `awareness.json` | learned app-usage counts (proactive) | until cleared in Settings |
| `model_caps.json` | detected model capabilities (D4) | refreshed per server build |
| `recordings/` | replay recordings when `record_model_io` is on (D13) | manual |
| `routines.json` | saved routines and their last run (D21) | until deleted |
| `browser-profile/` | the assistant's own browser profile (D23) - the user's sign-ins there | until deleted |
| `mcp_servers.json` | MCP server configuration incl. secrets (D24) - never shown or logged | until removed |
| `scheduled/<name>.ps1` | reminder scripts Task Scheduler runs (D21) | removed when the task is cancelled |
| `benchmarks.json` | per-model benchmark results (D28) | replaced by the next run of that model |
| `runs/<run id>.json` | past runs for the Activity panel: request, effort, model, usage, slimmed events (D27) | newest 300 / 30 days |
| `netlog.jsonl` | the connection log (host, route, size; never a temporary chat's) - only when "Save the log" is on (D38) | newest 500 entries |
| `search_history.json` | searches and pages read - only when search history is on, never Max/Local/temporary (D38) | the chosen days |
| `app-window-profile/`, `overlay-window-profile/` | the app window's and the overlay window's browser profiles (D38) - this app's page only | until deleted |
| `voice/` | whisper.cpp, its model, Piper and the voice (D39) - downloaded on the user's click; `voice/out` holds Piper's file for a moment | until deleted |
| `research/<task>.json` | a research run's ledger: sources, notes, progress - for "resume" (D39); never a temporary chat's | until deleted |
| `mcp_servers.json` → `roblox_studio` | Roblox's own Studio MCP (D42): `%LOCALAPPDATA%\Roblox\mcp.bat`, no secrets | until removed in the MCP tab |
| %TEMP%\AgenticWorkspace-temp\tmp-… | one temporary chat's files, uploads and private browser profile (D38) | deleted on close, exit and startup |

## Deferred
Things not done, and why (limits imposed by others are under Limitations):
- **Blender and Figma adapters** - interface only (D22), by the brief. See Next steps.
- **Roblox plugin run in real Studio** - the v3 plugin is now installed in
  `%LOCALAPPDATA%\Roblox\Plugins` (2026-09-24; audited: only 127.0.0.1, no loadstring; the previous
  pre-v3 copy is in `data/plugin_backup/`). Its first live run needs the user: open a place in Studio,
  click Bridge, allow HTTP to 127.0.0.1, then run the Roblox flow in "Suggested next steps".
- ~~schtasks on Windows~~ - done 2026-09-24 (D30): `/sd` follows the Windows short-date order.
- ~~Live web search~~ - verified live on Windows 2026-09-24 (D30).
- ~~Ambient idle mode~~ - built 2026-09-24 (D30).

## Limitations (imposed by models, llama-server, Roblox, Windows - not work left undone)
- **Roblox:** `ScriptEditorService:UpdateSourceAsync` and `StudioTestService` behaviour is taken from the
  API reference, not run here; Ctrl+Z in Studio can't be driven by the app, so non-script changes are
  "Not undoable here". Studio's HttpService must be enabled by the user.
- **Windows Task Scheduler:** `/TR` is limited to 261 characters; one-off `/sd` dates may be read in the
  system locale's order.
- **Models / llama-server:** small local models get a reduced tool list and one call per step; effort levels
  above what a model's context or tool protocol supports fall back (D4, D12); streaming Stop before the first
  token relies on Windows socket semantics (one of the 6 Windows-only tests).
- **Proxies:** with a proxy configured, web reads go through it and the proxy resolves names (the
  connect-time address check applies only to direct connections). Routed, only literal private addresses
  and local-only names are refused before sending; a hostile public name that resolves to 127.0.0.1 at a
  proxy running on this PC reaches this PC's loopback. Tor refuses private addresses itself; a generic
  local proxy may not.
- **Network layer (D38):** it covers this app's Python traffic and the assistant's browser (proxy flags).
  Not covered: asyncio sockets on the proactor loop (none used for outbound traffic today), pip/run.bat,
  links the user opens in their own browser, ping (ICMP - refused while routed). Edge itself still calls
  Microsoft services (edge.microsoft.com, bing.com, office.com) when the assistant's browser or the app
  window starts, even with background networking off - through the route when one is set;
  AGENTIC_BROWSER can point at a Chromium build. A VPN can only be detected, not switched.
- **Overlay window (D38):** Windows only; Roblox Studio's window is found by its title and process name.
- **Voice (D39):** the Browser pane of the Claude app blocks the microphone, so live capture was not tested
  there (the pipeline was, with synthesized speech); the app window asks for microphone permission once.
  Barge-in relies on the browser's echo cancellation - with loud speakers it can stop itself (turn it off).
  Hebrew speech runs on the Phonikud + Shaul pipeline (`voice_hebrew.py`, non-commercial license; the D41 add-on
  note is outdated); Hebrew and Russian recognition with whisper small is usable but weaker than English.
- **Finance (D39):** Yahoo's chart API is unofficial and rate-limits (Nasdaq's is the fallback, US listings
  only); SEC figures are as filed (no split adjustment for older per-share values); non-US companies get
  prices and news but no filings. Ultra-deep research takes hours on a local model and many keyless searches
  (DuckDuckGo may pause it for minutes; a Brave/Tavily key or SearXNG helps).
- **Browsers:** the Fetch Metadata origin rules assume a current Edge/Chrome (Sec-Fetch-Site since 2020).
- **Web (D34):** keyless search engines rate-limit scripts, so DuckDuckGo can pause for minutes; without the
  browser fallback, searches say so rather than return nothing (a SearXNG instance avoids it). Sites that
  demand a sign-in (Reddit) or a check a person must complete can't be read automatically; the user can
  complete it in the assistant's browser. Wikipedia's bot policy asks for a contact address in the
  User-Agent; this app has none to give, so it names itself and its local address.
- **Benchmark:** usable context is probed up to 32,768 tokens (a larger window is recorded as "at least");
  Cancel before a model's first byte relies on closing the socket from another thread, which interrupts the
  read on Windows (immediate there) but not on Linux; a benchmark is tied to the llama-server build it ran on.

## Next steps
- **Blender adapter:** subclass `app_registry.AppAdapter` in `blender_adapter.py`; talk to a small Blender
  add-on over a localhost socket (bpy runs inside Blender). Declare tools (`blender_status`,
  `blender_read_scene`, `blender_run_operator`...) in `tools/schemas.py` with registry policy and a
  permission tier; implement `preview`/`fingerprint`/`restore` for anything that should be undoable
  (e.g. a text block or a node tree serialized to JSON), everything else says "Not undoable". Register with
  `app_registry.register_adapter` and `undo_store.register_external`. Copy `test_roblox.py`'s shape with a
  fake add-on.
- **Figma adapter:** Figma's plugin API runs only inside Figma: a Figma plugin with a long-poll loop like
  the Roblox bridge (`roblox_bridge.py` protocol v3 is the template: sessions, deadlines, late-result drop),
  or the REST API for read-only file access (needs a personal token - store it like MCP secrets).

## Change log (stands in for commits — see D1)
- 2026-09-23 — ARCHITECTURE.md created; baseline 365/365.
- 2026-09-23 — Phase 1 (1.1–1.8) and its review fixes; 463/463.
- 2026-09-24 — Phase 2 split (D14); 464/464.
- 2026-09-24 — Phase 3.1 content is data (D15); 475/475.
- 2026-09-24 — Phase 3.2 per-folder rules (D16); 488/488.
- 2026-09-24 — Phase 2 review fixes; Phase 3.3 memory (D17); 506/506.
- 2026-09-24 — Phase 3.4 Prompt Prep (D18); 520/520.
- From here on, see `git log` (D19).
- 2026-09-24 — (this copy has no .git) Ternary Bonsai 2 27B on PrismML's fork (D29): models.ini, run.bat,
  `llama_local` build detection + load hint, `test_bonsai.py`; `test_projects` outside-folder test made
  Windows-safe (its POSIX path pattern never matched on Windows); 696 run, OK (4 skipped).
- 2026-09-24 — PrismML fork installed + verified, Bonsai benchmarked (D29); ambient idle screen, locale-aware
  schtasks dates, live web check, Roblox plugin v3 installed, desktop shortcut (D30); 699 run, OK (4 skipped).
- 2026-09-24 — Context window meter + automatic/manual compression (D31); 711 run, OK (4 skipped).
- 2026-09-24 — Readability pass, Dusk theme, real Effort/Prompt Prep sliders, "what was added" view, XML
  tool-call recovery, missing-model marking (D32); 717 run, OK (4 skipped).
- 2026-09-24 — Tool calls: turn policy for scoped/indirect requests and replies, "open X in Studio",
  unknown-tool hints (D33); web search/read fallbacks and main-content extraction (D34); slider rework (D35);
  741 run, OK (0 skipped - the real-browser tests now find Edge).
- 2026-09-25 — Roblox tools offered and named correctly, published place name, per-model slider saves, model
  settings save (D36); 750 run, OK. Plugin copied to %LOCALAPPDATA%\Roblox\Plugins (previous copy:
  data/plugin_backup/AgenticBridge.server.lua.before-placename). Not committed: this copy has no .git and git
  isn't installed.
- 2026-09-26 — Diagnostics + models.ini fitted to the GPU, in-process GPU counters, live model strip (D37);
  Bias check; Terminal Lite Dark/Light; plan verified on a live router and refined (hybrid models stay whole,
  MoE experts read into RAM at load); 827 run, OK (`RealBrowserTests` failed once in an earlier full run and
  passed alone - known flaky: it shares the machine's browser).
- 2026-09-27 — Temporary chats, the network layer, private search modes, the speed indicator, the Studio
  Overlay, Judaism Mode, the app window and the overlay window (D38); verified live against the real app
  (canary chat through a crash/restart, a forwarding SOCKS5 relay, the kill switch, Require VPN, the leak
  test, every search mode, the overlay, the theme, both windows); 895 run, OK - also with a proxy route in
  settings.json (tests now pin the direct route where they check this PC's own address checks).
- 2026-09-27 — Voice mode (whisper.cpp + Piper, local), research depth up to Ultra-deep with a scalable ledger and
  resume, Investment research and Market data with evidence labels (D39); verified live: the finance sources,
  an Investment research run, a Market data answer, the voice setup download (242 MB), speech and
  recognition, and the spoken reply loop; 924 run, OK.
- 2026-09-27 — Natural voice (D40): Kokoro + Parakeet + live captions via sherpa-onnx (requirements.txt), speech
  normalisation, gapless delivery; models downloaded through the app's setup (1,042 MB incl. both Kokoro
  copies); benchmarked and verified live; 932 run, OK.
- 2026-09-27 — Languages, the voice screen, approving by voice, personas, Judaism Mode out of Settings (D41);
  whisper small + the Hebrew Piper voice downloaded through the app's setup (525 MB, approved); netlayer's
  `close()` copies its pools (a Stop from another thread raced it). Verified live: the voice screen, the
  approval card with spoken/typed/keyed answers (a refused voice approval keeps the chat card open), Settings >
  Voice, a persona saved and cleaned by the server, IsraelGPT answering through the real local model, and a
  Hebrew turn answered in Hebrew; 949 run, OK.
- 2026-09-29 — Studio work in one call (D42): the turn classifier, the write-access line and effort-invariance
  test, Roblox's Studio MCP connected (with the user's approval) with its routing arguments filled in, the Luau
  loop rule, `roblox_batch`, the repeat hint in Prompt Prep, fitting tools to a local window, "Allow all like
  this", and the quoted-place-name bug. Verified live at Quick effort with
  Qwen-3.5-4B: "stack 10 parts" -> one execute_luau call, one approval, 10 stacked parts; 1004 run, OK.
- 2026-09-29 — Confirmation and appearance (D43): `corroboration.py` (origins, "How well confirmed" in research
  reports, the Claim timelines tool, "via" on saved sources), appearance families with the top-bar light/dark
  button and Ctrl+Alt+L, Eclipse and Aurora by day, settings migration (theme -> family, Companion -> Focus),
  Focus's reading rail, the Command look, MossadGPT (theme and persona). Verified live in the preview; the
  user's settings.json restored unchanged. 1036 run, OK; smoke_test 42, OK.
- 2026-09-29 — MossadGPT's world monitor, Phase 1 (D44): `osint/` (geo, store, sources, satellites, collector, tool),
  `geo/` (Natural Earth + GeoNames, built by `geo/build_geo.py` from the approved downloads), `/api/osint/*`, the
  globe behind the chat and the Ops view, `world_monitor` for MossadGPT's turns, the "none"-mode note and the local
  world-question instruction. Verified live (feeds, globe, a card click, two MossadGPT questions answered from the
  tool); settings.json unchanged. 1054 run, OK; smoke_test 42, OK.
- 2026-10-01 — Local images and the message bar (D45): `image_gen.py`, `image_setup.py` (files on
  E:\ClankerLocalCpp\Images, every SHA-256 verified), `generate_image`, `/image`, the Image chip, Settings > Images,
  `/api/images/*`, `llama_local.unload_all`, `local_planned`, the composer container queries. Verified live (a chat
  turn made and showed an image; Try it); settings.json unchanged.
- 2026-10-01 — The revamp (D46): image speed (easycache, VRAM reserve, warm-up, log progress), gallery, trash and
  the Move dialog; the shell (places, Setup, Tools search, panel tabs, Settings search, More, shortcuts); auto tools,
  charts and careful mode; the context-fit fix and overflow retry; error cards, approval verdicts, per-action
  indicators, the privacy line, Storage. 1142 run, OK. Verified in the preview; no live model run this pass.
- 2026-10-01 — Voice/world pass (another assistant, after checkpoint 11): per-turn Auto effort, transcript review,
  voice timings, the app motion setting for the orb/globe, world_monitor evidence in the Ops view. Not logged here
  at the time. It dropped the call that moves the globe as each spoken sentence plays (and changed the test that
  guarded it), and its evidence box could show unstyled outside the Ops view and covered the chat column.
- 2026-10-01 — The globe follows the assistant again; Ops view HUD (checkpoint 12-globe-follow): `Globe.follow` is
  called again per spoken sentence (with the citation highlight); `collector.set_focus` carries kind/layer/id so a
  tracked aircraft or satellite gets the reticle; following pauses on your own drag/zoom until the next question
  (Resume chip, reticle toggle remembered per browser); a click on a dot no longer counts as moving it. The globe
  and its HUD share one box (`--globe-l/r/b`), so nothing covers the chat column or the docked voice panel; narrow
  windows stack the globe above the chat. Canvas: night side, limb shading, target ring and name tag, flight arc,
  readout; controls (zoom, reset, follow, layers) and keyboard (arrows, + -, 0, Esc). Tests: fixtures/review/
  globe.test.js (run by test_review_voice_ui.py), test_osint FocusTests. 1149 run, OK. Verified in the preview with
  simulated tool events and the real /api/osint/mentions; settings.json unchanged; no live model or microphone run.
- 2026-10-01 — Windowless start: `launcher.pyw` (run by pythonw via `Agentic Workspace.vbs`; run.bat hands over
  unless `run.bat console`) - dependency sync only when requirements.txt changed, keys.local.bat, llama-server with
  CREATE_NO_WINDOW and a log, uvicorn in-process (graceful shutdown capped at 5 s), the app window watched by title
  and browser process: closed for 4 s stops the server and the llama-server it started. Every console child gets
  CREATE_NO_WINDOW (under run.bat they shared its console; under pythonw each opened a window - found live:
  PowerShell from app_registry). One instance (named mutex). test_launcher.py. 1155 run, OK; verified live.
- 2026-10-06 — Skills (`skills.py`): an action turn sent all 78 tool schemas (~8.2k tokens). Now: a core + the
  skills the request's words pick + this chat's skills (kept, so llama.cpp's cached tool prefix stays) + load_skill
  for more, with a MORE SKILLS index line. Skills only shrink what is shown: authority stays the mode's, and calling
  a real tool of an unloaded skill runs and loads it (runtime.execute_tool_guarded -> skills.note_used); bad names get
  close matches from all skills. prompts.for_tools already drops sentences naming unoffered tools, so the system
  prompt shrinks too. Measured (cl100k): local 64k action 9,901 -> 4,229; local 16k 6,472 -> 3,569; cloud 11,086 ->
  5,360. Also: market_data only with the Market tool; Windows shell windows out of the live block. Setting skills_on.
  Project Velocity reviewed (AGPL-3.0, Docker + Postgres/TimescaleDB + Cesium): ideas adapted, no code copied -
  osint/intel.py (GPS jamming from ADS-B NACp<8/NIC<7 per 0.5° cell over 30 min; watch-officer alerts for squawks
  7500/7600/7700, high jamming and strong quakes, one per thing per hour; changes()), store.at/history_range/count_in,
  /api/osint/replay and the globe's Rewind; world_monitor jamming/alerts/changes. AIS ships left out (keyless sources
  are regional; global needs an AISStream key). test_skills.py, IntelTests. 1171 run, OK; verified in the preview
  on live data (a jamming cell over Oklahoma; rewind); settings.json unchanged; no live model run.
- 2026-10-09 - D47 Raw / Enhanced (checkpoint `.checkpoints\14-before-prompt-modes` holds the code before it):
  response_modes.py; agent/prompts.py (ENHANCED_CORE, both prompts rewritten, for_tools keeps lines); careful.py
  (PROMPT in the core, claims_without_change); agent/models.py, agent/tasks.py (_raw_turn, _begin_raw_turn),
  agent/loop.py, tools/schemas.py, orchestrator.py, agent/sessions.py, core/config.py, core/state.py, api/server.py,
  untrusted.py; index.html (Setup, hint, Settings, Raw pill, the claim note). test_response_modes.py (16);
  test_agent_features and test_studio_tools updated (the fit test isolates a fresh chat's skills). Run in a Linux
  VM: 1187 tests, only the platform failures the Windows baseline doesn't have (paths, NTFS, sockets, model
  paths) plus test_projects' list test once (timing; passes alone). No live model run yet.
- 2026-10-10 - D48 the redesign (checkpoint `.checkpoints\15-before-redesign` holds the code before it): index.html
  (shell, home, work lines, approval card, settings pages, Judaism Mode), agent/tasks.py (greeting, route message),
  orchestrator.py (no review of route-only turns), careful.py, personas.py, tools/approvals.py, core/config.py and
  netlayer.py (defaults), tests updated (test_task_flow greeting, test_overlay_theme net_indicator default).
- 2026-10-10 - D49 the leftovers (checkpoint `.checkpoints\16-before-polish` holds the code before it): index.html
  (approval fold, work lines, every settings page, Ops HUD), tools/approvals.py (`brief`, `asked`),
  agent/sessions.py (`undo_id`, `asked` kept), api/server.py (tool `label`), test_approvals.py (AnswerRecordTests,
  3). Container run: 1190 tests, the same 15 platform failures as the committed baseline (Windows paths, junctions,
  models.ini on E:, launcher, sherpa). Checked in Chromium in every appearance, 390 px wide and with motion off;
  no live model run.
- 2026-10-10 - D50 (checkpoint `.checkpoints\17-before-start-context` holds the code before it): skills.py,
  tools/schemas.py, agent/context_window.py, api/server.py (context, browser panel, vision file, tool label),
  composer_tools.py and auto_tools.py (visuals), agent/prompts.py (choices line), browser.py (headless, Viewer),
  core/config.py (browser_builtin), model_assets.py (new), index.html (interactive replies, Browser panel, vision
  line, Settings switch); tests: test_skills (+7), test_model_assets (new, 4), test_web_browser (the panel against a
  real headless Chromium), test_agent_features, fixtures/review/interactive.test.js (new; run by
  test_review_voice_ui), and three tool-visibility tests now state their request (test_multilingual_tools,
  test_roblox, test_search_privacy). Container: 1201 tests, the same 15 platform failures as before. On the PC:
  settings.json image_root/image_extra_roots and models.ini's Ornith line (backups `*.bak-before-ssd`).
- 2026-10-10 - D51 (checkpoint `.checkpoints\18-before-llama-builds` holds the code before it): llama_builds.py
  (new), llama_local.py (force_load switches first; vision from input_modalities; load hint), agent/models.py
  (`_ensure_build`), api/server.py (/api/llama/builds, /apply, setting, stop on exit, Reload's eviction stays),
  core/config.py (`llama_build`), run.bat, launcher.pyw (newest first, AGENTIC_WINDOWLESS), diagnostics.py (both
  builds), index.html (Settings > Models > llama.cpp, "Starting ... for"); tests: test_llama_builds (new, 15),
  test_bonsai and test_launcher (the newest build starts). On the PC: llama.cpp b11541 and prism-b10770 installed,
  the old builds in E:\ClankerLocalCpp\_old.
- 2026-10-12 - D52 (checkpoint .checkpoints/19-before-system-messages): system_messages.py (new),
  agent/models.py (base selection), agent/tasks.py (turn snapshot), core/config.py (defaults and atomic saves),
  api/server.py (preset metadata and validated settings), static/system-messages.js (new settings controller),
  synchronized index.html/static/index.html, test_system_messages.py, fixtures/review/system_messages_ui.test.js,
  isolated preview fixture and UI test registration. Verified 77 Python + 38 Node checks; 78 browser
  palette/motion/width combinations and isolated custom save. Real settings unchanged; zero guarded data writes.
- 2026-10-12 - D53 (checkpoint .checkpoints/20-before-context-effort-remake): index.html/static/index.html,
  fixtures/review/context_effort_ui.test.js and context_effort_preview.py, test_review_voice_ui.py registration,
  docs and visual evidence. Verified 80 Python + 51 Node checks, 156 palette/motion/width layouts and six
  narrow context states. No new dependencies/downloads; real settings and data untouched.
- 2026-10-12 - D54 (checkpoint .checkpoints/21-before-codex-sliders): index.html/static/index.html,
  context_effort_ui.test.js and context_effort_preview.py, docs and updated visual evidence. Verified
  80 Python + 56 Node checks and 78 palette/motion/width layouts. Real settings and data untouched.
- 2026-10-12 - D55 (checkpoint .checkpoints/23-before-cross-check-toggle): effort.py, core/config.py,
  api/server.py, agent/models.py, agent/tasks.py, orchestrator.py, synchronized HTML, focused runtime/API
  and reviewer tests, Node UI tests, isolated preview, docs and cross-check visual evidence. 52 guarded
  Python + 59 Node checks; 78 palette/motion/width layouts. Real settings/data untouched.
- 2026-10-12 - D56 (checkpoint .checkpoints/24-before-effort-polish): synchronized HTML, effort.py,
  agent/models.py, test_effort.py, test_effort_integration.py, context_effort_ui.test.js, documentation and
  visual/style evidence. 53 guarded Python + 62 Node checks, 468 browser states. Settings unchanged during
  guarded tests; the current hash differs from D55 because the live file was modified before checkpoint 24.
- 2026-10-12 - D57 (checkpoint .checkpoints/25-before-slider-energy): synchronized HTML, focused Node
  control regressions, docs and energy visual/layout/motion evidence. 66 Node checks and 468 browser states.
  No backend/model edits, dependencies/downloads or live settings writes.
- 2026-10-12 - D58 (checkpoint .checkpoints/26-before-continuous-sliders): synchronized HTML, fractional
  control regressions, preview-only native event tracing, docs and alignment/gesture evidence. 70 Node
  checks, 468 browser states and native pointer drag. Live settings/backend/model configuration untouched.
- 2026-10-12 - D59 (checkpoint .checkpoints/27-before-slider-audit): synchronized HTML, four focused
  Node regression groups for stale input/real refreshes, shared/stable headers, docs and fresh evidence.
  74 Node checks and 468 browser states; real pointer gesture rechecked. Live settings preserved.
- 2026-10-12 - D60 (checkpoint .checkpoints/28-before-stationary-pixels): synchronized HTML,
  stationary pixel CSS masks, 120ms settle, stable markers, preview detail-update optimization;
  context_effort_ui.test.js adds two input/accessibility regressions. 76 Node checks and 468 browser
  states pass; trusted fractional drag, changing opacity and unchanged settings confirmed. Research
  and evidence in fixtures/review/ui-remake; no backend/model changes or downloads.
- 2026-10-13 - Image models (no checkpoint): Animagine XL 4.0 and Pony Diffusion V6 XL added to image_gen.MODELS
  as local q8_0 GGUF conversions on E: (sd-cli -M convert); a model's `negative`, `suffix` and `scheduler`
  are now passed to sd-server. Pony uses DPM++ 2M Karras / 28 steps / CFG 6 (Euler a at 20 steps was mushy).
- 2026-10-13 - D61 (no checkpoint): chat_guard.py (new), agent/tasks._screen, agent/sessions (ensure_chat,
  lock checks), chat_store KEEP_KEYS, api/server (/api/guard*, unlock, locked chat refusals), index.html
  (lock banner, owner unlock, Settings > Privacy chat limits), test_chat_guard.py (19 tests).
