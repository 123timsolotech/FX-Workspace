"""
main.py
=======
FX Workspace - entrypoint. run.bat starts it with `uvicorn main:app`.

Everything that used to live in this file was split into modules in Phase 2
(behaviour unchanged; see ARCHITECTURE.md, D14):

  core/config.py      constants, settings.json load/clean/save
  core/state.py       shared runtime state (app_state, locks, task board, ledger)
  core/observer.py    awareness engine glue
  events.py           audit log + the UI event stream
  agent/prompts.py    system prompts
  agent/parsing.py    tool-call parsing (native, printed as text, malformed)
  agent/models.py     model calls: history fitting, local (streamed) and cloud
  agent/loop.py       the agent loop
  agent/tasks.py      turn handling, desktop routes, task queues, the orchestrator
  tools/schemas.py    tool schemas and per-turn tool authority
  tools/files.py      paths, undo journal, atomic writes, secret files
  tools/dispatch.py   execute_tool: every tool's implementation
  tools/runtime.py    registry, executor, execute_tool_guarded
  tools/approvals.py  approval views, broker, gate_batch
  routing/handoff.py  model registry and handoff
  api/server.py       the FastAPI app and every route

History of this backend (kept from the original main.py):

FX Workspace - backend.

This revision targets two things: (A) the integration seams that only break
when a LOCAL llama.cpp model is driving, and (B) the agent's lack of working
memory and situational awareness.

A. LOCAL MODEL INTEGRATION
   1. Tool calls emitted as TEXT are now parsed. Most small local models
      cannot reliably populate the OpenAI `tool_calls` field and instead
      print <tool_call>{...}</tool_call>, a ```json block, or a bare object
      into `content`. The old loop saw no tool_calls, treated that text as
      the final answer, and the agent appeared to "reply about" doing the
      task instead of doing it. See _extract_tool_calls_from_text.
   2. Malformed tool arguments no longer kill the turn. json.loads on a raw
      model string was unguarded; one stray trailing comma from a 4B model
      raised inside the loop and surfaced as a 500. See _coerce_args.
   3. Vision routing is decided by scanning the WHOLE message list, not just
      messages[-1]. Screenshots arrive as a user message, but any tool result
      appended after one made the last message text-only, so requires_vision
      came out False and the image was silently stripped by the text
      sanitiser - the model was asked to click on an image it never received.
   4. Context is now budgeted in TOKENS against the real llama-server
      context size, not in characters. A 2200x1400 JPEG is ~300KB of base64;
      two of those exceed an 8192-token context on their own. Screenshots are
      captured small in local mode, and stale ones are demoted to a text
      placeholder so only the newest image is ever resident.
   5. History is trimmed in whole turns. Dropping messages one at a time
      orphaned `tool` messages from the assistant message carrying their
      tool_calls, which OpenAI-compatible servers (llama-server included)
      reject outright - it read as a random mid-conversation failure.
   6. Local mode gets a compact prompt and a reduced tool schema, because the
      full schema alone was consuming roughly a third of an 8K context.
   7. click_at no longer overwrites the reference screenshot metadata. It was
      replacing the thumbnail-space meta the model's image was drawn in with
      full-resolution meta, so every click AFTER the first was scaled wrong.

C. THIS REVISION (proactive + privacy + Roblox)
   * awareness.py watches cheap local signals and offers help on its own,
     with its reasoning, through /api/suggestions (never acts by itself).
   * approval_broker.py shows approvals inline in the web UI when it is
     focused and falls back to the native popup otherwise.
   * Local mode is the default; cloud calls strip screenshots unless the user
     allows them (Settings > Privacy).
   * The Roblox bridge gained script read/edit, search, Output and a pushed
     Studio context; its tools join the small local tool set whenever Studio
     is connected. roblox_places.py (imported but missing) now exists.
   * Every executed tool emits a UI event, so the chat shows live steps.

B. WORKING MEMORY AND LIVE VIEW  (see context.py)
   Every model call now carries a regenerated block containing the live
   machine state, the current task board, and a ledger of what has already
   been executed. Side-effecting calls that exactly repeat a recent one are
   blocked, identical consecutive tool batches are short-circuited, and each
   approved batch is followed by an observed before/after delta.
"""
import core.modpath  # noqa: F401  (the module folders on sys.path - first)
from api.server import app  # noqa: F401,E402  (uvicorn main:app)
