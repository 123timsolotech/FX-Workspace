"""
replay_harness.py
=================
Runs the real agent pipeline against recorded model responses - no live
model, no llama-server. Used by the replay tests and for reproducing bugs.

Fixture format ("agentic-replay/1", JSON; see fixtures/replay/README.md):

    {
      "format": "agentic-replay/1",
      "meta": {"name": str, "provenance": "recorded" | "reconstructed",
               "evidence": str, "model": str, "notes": str},
      "setup": {"mode": "local" | "cloud", "entry": "agent" | "loop",
                "settings": {...}, "turn_policy": "action" | "read" | "none",
                "local_context": int},
      "request": str,
      "responses": [ <model message>, ... ],      # tool-bearing calls, in order
      "aux_responses": [ <model message>, ... ],  # tool-free calls (planner, reviewer, summary)
      "approvals": [true, false, ...],            # answers to approval cards, in order
      "tools": {"mode": "stub" | "real", "results": {"<tool>": [ {ok, message}, ... ]}},
      "expect": { ... checked by check_expectations ... }
    }

A <model message> is {"finish_reason", "content", "reasoning_content",
"tool_calls": [{"id", "name", "arguments": <raw string, exactly as sent>}],
"usage": {...}, "chunks": [...optional SSE deltas for streaming replay...]}.

The string "{sandbox}" anywhere in a fixture is replaced by a per-run temp
folder, so "real" tool mode never touches anything outside it.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import agent.loop as agent_loop
import agent.models as llm
import agent.prompts as prompts
import agent.tasks as tasks
import core.config as config
import core.state as shared
import events as event_log
import recorder
import tools.approvals as approval_path
import tools.dispatch as dispatch
import tools.files as files
import tools.runtime as runtime
from run_control import run_control
import llama_local
import llm_compat
import roblox_bridge
import system_metrics

FIXTURE_DIR = Path(__file__).with_name("fixtures") / "replay"
FORMAT = "agentic-replay/1"


def load_fixture(name_or_path) -> dict:
    path = Path(name_or_path)
    if not path.suffix:
        path = FIXTURE_DIR / f"{name_or_path}.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("format") != FORMAT:
        raise ValueError(f"{path.name}: unsupported fixture format {data.get('format')!r}")
    if (data.get("meta") or {}).get("provenance") not in ("recorded", "reconstructed"):
        raise ValueError(f"{path.name}: meta.provenance must be 'recorded' or 'reconstructed'")
    return data


def list_fixtures() -> list:
    return sorted(p.stem for p in FIXTURE_DIR.glob("*.json"))


def _substitute(value, sandbox: str):
    if isinstance(value, str):
        # Fixtures are JSON; a Windows path inside a JSON-encoded string needs escaped backslashes.
        return value.replace("{sandbox_json}", sandbox.replace("\\", "\\\\")).replace("{sandbox}", sandbox)
    if isinstance(value, list):
        return [_substitute(v, sandbox) for v in value]
    if isinstance(value, dict):
        return {k: _substitute(v, sandbox) for k, v in value.items()}
    return value


def as_response(msg: dict):
    """A recorded message as the object shape the OpenAI/litellm clients return."""
    calls = [SimpleNamespace(id=c.get("id") or f"call_{i}", type="function",
                             function=SimpleNamespace(name=c["name"], arguments=c.get("arguments", "")))
             for i, c in enumerate(msg.get("tool_calls") or [])]
    usage = msg.get("usage") or {}
    return SimpleNamespace(
        choices=[SimpleNamespace(finish_reason=msg.get("finish_reason", "stop"), message=SimpleNamespace(
            content=msg.get("content") or "", tool_calls=calls or None,
            reasoning_content=msg.get("reasoning_content")))],
        usage=SimpleNamespace(completion_tokens=usage.get("completion_tokens"),
                              prompt_tokens=usage.get("prompt_tokens"),
                              completion_tokens_details=None) if usage else None)


@dataclass
class ReplayResult:
    result: dict
    approvals: list = field(default_factory=list)       # each: list of action dicts shown on one card
    executed: list = field(default_factory=list)        # (tool, args) actually run
    events: list = field(default_factory=list)          # activity events emitted during the run
    requests: list = field(default_factory=list)        # kwargs of every model request
    conversation: list = field(default_factory=list)
    sandbox: str = ""
    unused_responses: int = 0
    prep_cards: list = field(default_factory=list)      # Prompt Prep cards shown (Expand / Plan first)

    @property
    def status(self):
        return self.result.get("status")

    @property
    def message(self):
        return self.result.get("message") or ""


class _Model:
    """Serves recorded responses in order; tool-bearing and tool-free calls
    come from separate queues so planner/reviewer calls never shift the script."""

    def __init__(self, responses, aux, requests, stop_at=None, on_stop=None, at_response=None):
        self.responses, self.aux, self.requests = list(responses), list(aux), requests
        self.served, self.stop_at, self.on_stop = 0, stop_at, on_stop
        self.at_response = at_response or {}

    def completion(self, **kwargs):
        self.requests.append({k: copy.deepcopy(v) for k, v in kwargs.items() if k not in ("messages", "tools")}
                             | {"messages": copy.deepcopy(kwargs.get("messages") or []),
                                "tools": [t["function"]["name"] for t in kwargs.get("tools") or []]})
        # A request that offers tools - natively, or as a catalogue in the prompt
        # for models without native tool support - is an agent step.
        agent_step = bool(kwargs.get("tools")) or "response_format" in (kwargs.get("extra_body") or {}) or any(
            "You can call these tools" in str(m.get("content", "")) for m in kwargs.get("messages") or [])
        queue = self.responses if agent_step else self.aux
        if not queue:
            if agent_step:
                raise RuntimeError("replay: the agent asked the model more times than the recording has replies")
            return as_response({"finish_reason": "stop", "content": ""})
        if agent_step:
            if self.stop_at is not None and self.served == self.stop_at and self.on_stop:
                self.on_stop()          # the user pressed Stop while the model was generating this reply
            self.served += 1
        msg = queue.pop(0)
        if "raise" in msg:              # a recorded server error, e.g. llama-server refusing `tools`
            raise RuntimeError(msg["raise"])
        return as_response(msg)

    def transport(self, url, payload, headers, timeout):
        """Stands in for llama-server's streaming endpoint: every local agent
        request is streamed, so each recorded reply is served as SSE chunks
        (explicit msg["chunks"], or the message cut into small fragments -
        which exercises fragment reassembly on every replay)."""
        import llm_stream
        self.requests.append({k: copy.deepcopy(v) for k, v in payload.items() if k != "tools"}
                             | {"tools": [t["function"]["name"] for t in payload.get("tools") or []], "streamed": True})
        agent_step = bool(payload.get("tools")) or "response_format" in payload or any(
            "You can call these tools" in str(m.get("content", "")) for m in payload.get("messages") or [])
        if not agent_step:
            msg = self.aux.pop(0) if self.aux else {"finish_reason": "stop", "content": ""}
            if "raise" in msg:
                raise llm_stream.StreamHTTPError(msg["raise"])
            return llm_stream.chunk_message(msg, size=int(msg.get("chunk_size") or 7))
        if not self.responses:
            raise RuntimeError("replay: the agent asked the model more times than the recording has replies")
        if self.stop_at is not None and self.served == self.stop_at and self.on_stop:
            self.on_stop()
        if self.at_response and str(self.served) in self.at_response:
            self.at_response[str(self.served)]()     # e.g. the user changes effort while this reply streams
        self.served += 1
        msg = self.responses.pop(0)
        if "raise" in msg:
            raise llm_stream.StreamHTTPError(msg["raise"])
        return llm_stream.chunk_message(msg, size=int(msg.get("chunk_size") or 7))


def _fixture_props(setup: dict, url: str):
    if "/props" in url and isinstance(setup.get("props"), dict):
        return setup["props"]
    raise ConnectionError("replay: no llama-server (add setup.props to the fixture to simulate /props)")


def run_fixture(fx: dict, sandbox: str | None = None) -> ReplayResult:
    """Replay one fixture through the real pipeline and collect what happened."""

    own_tmp = None
    if sandbox is None:
        own_tmp = tempfile.TemporaryDirectory(prefix="replay_")
        sandbox = own_tmp.name
    fx = _substitute(copy.deepcopy(fx), sandbox)
    setup = fx.get("setup") or {}
    model_name = (fx.get("meta") or {}).get("model") or "replay-model"
    requests, approvals, executed = [], [], []
    def settings_change(changes):
        if callable(changes):           # a test acting mid-reply itself (e.g. through the real API)
            return changes

        def apply():                    # what POST /api/settings does, while the reply is streaming
            shared.app_state["settings"].update(changes)
            tasks._queue_effort_change()
        return apply
    model = _Model(fx.get("responses") or [], fx.get("aux_responses") or [], requests,
                   stop_at=setup.get("stop_at_response"), on_stop=lambda: run_control.stop(),
                   at_response={k: settings_change(v) for k, v in (setup.get("settings_at_response") or {}).items()})
    answers = list(fx.get("approvals") or [])
    tools = fx.get("tools") or {"mode": "stub"}
    stub_results = {k: list(v) for k, v in (tools.get("results") or {}).items()}
    real_execute = dispatch.execute_tool

    def fake_approval(actions):
        approvals.append(copy.deepcopy(actions))
        return answers.pop(0) if answers else False

    # Prompt Prep cards: the user's answers are scripted too (default: send as shown).
    prep_cards, prep_answers = [], list(fx.get("prep_answers") or [])

    class _PrepGate:
        def request(self, kind, payload, cancelled=None, **kw):
            prep_cards.append({"kind": kind, **copy.deepcopy(payload)})
            answer = prep_answers.pop(0) if prep_answers else {"action": "send"}
            return {"action": answer.get("action", "send"), "text": answer.get("text", payload.get("text", ""))}

    def run_tool(name, args):
        executed.append((name, copy.deepcopy(args)))
        if tools.get("mode") == "real":
            return real_execute(name, args)
        queue = stub_results.get(name)
        return queue.pop(0) if queue else {"ok": True, "message": "ok"}

    keys = ("conversation", "settings", "mode", "local_model", "local_engaged", "turn_policy",
            "effort_thinking", "task_context", "iteration_budget", "intent_guard", "current_user_request",
            "pending_clarification", "resolved_clarification", "effort_session", "active_project", "run_budget",
            "untrusted_seen", "prep_session", "prep_mode", "active_chat", "brief_untrusted", "last_run_history",
            "context_summary", "context_last_compression")
    saved = {k: copy.deepcopy(shared.app_state.get(k)) for k in keys}
    start_seq = shared.app_state.get("event_seq", 0)
    ctx_limit = int(setup.get("local_context") or 32768)
    # Model capabilities live in the sandbox, seeded from the fixture: a replay
    # neither reads nor changes the user's data/model_caps.json.
    import model_caps
    import undo_store
    sandbox_undo = undo_store.UndoStore(os.path.join(sandbox, ".undo"))
    import memory
    sandbox_memory = memory.MemoryStore(os.path.join(sandbox, ".memory.json"))
    for text in setup.get("memory") or []:          # fixtures may seed saved memory
        entry = sandbox_memory.add(text["text"] if isinstance(text, dict) else text)
        if isinstance(text, dict) and text.get("pinned"):
            sandbox_memory.update(entry["id"], pinned=True)
    # Chats, projects and folder indexes too: a replay reads and writes only the sandbox.
    import chat_store
    import projects
    import agent.sessions as sessions
    sandbox_chats = chat_store.ChatStore(os.path.join(sandbox, ".chats"))
    sandbox_projects = projects.ProjectStore(os.path.join(sandbox, ".projects"))
    sandbox_index = projects.FolderIndex(os.path.join(sandbox, ".index"))
    import routines
    sandbox_routines = routines.RoutineStore(os.path.join(sandbox, ".routines.json"))
    import run_log
    sandbox_runs = run_log.RunLog(os.path.join(sandbox, ".runs"))
    saved_caps = (model_caps.CAPS_PATH, model_caps._cache)
    model_caps.CAPS_PATH = os.path.join(sandbox, "model_caps.json")
    model_caps._cache = {model_name: {"model": model_name, **setup["model_caps"]}} if setup.get("model_caps") else {}
    try:
        # Defaults, not the user's settings.json: a replay must not depend on
        # (or be widened by) whatever permissions the user happens to have set.
        shared.app_state["settings"] = {**copy.deepcopy(config.DEFAULT_SETTINGS), **(setup.get("settings") or {})}
        shared.app_state["mode"] = config.MODE_LOCAL if setup.get("mode", "local") == "local" else config.MODE_CLOUD
        shared.app_state["local_model"] = model_name if setup.get("mode", "local") == "local" else ""
        shared.app_state["untrusted_seen"] = []
        # A fixture may start mid-conversation (e.g. a question already answered);
        # only the keys restored below can be set.
        shared.app_state.update({k: copy.deepcopy(v) for k, v in (setup.get("app_state") or {}).items() if k in keys})
        shared.task_board.clear()
        shared.ledger.clear()
        patches = [
            patch.object(llm_compat, "completion", side_effect=model.completion),
            patch.object(llama_local, "server_models", return_value=[model_name]),
            patch.object(llama_local, "server_is_up", return_value=True),
            patch.object(llama_local, "model_context_limit", return_value=ctx_limit),
            patch.object(llama_local, "is_vision_model", return_value=False),
            patch.object(llm, "resolve_local_model", return_value=model_name),
            patch.object(llm, "engage_local_model"),
            patch.object(llm, "candidate_pairs", return_value=[("replay/cloud-model", "k")]),
            patch.object(llm, "STREAM_TRANSPORT", model.transport),
            # File changes made during a replay are journaled in the sandbox, never
            # in the user's real undo trail (data/undo).
            patch.object(files, "undo", sandbox_undo),
            patch.object(runtime.tool_exec, "undo", sandbox_undo),
            # Saved memory too: a replay neither reads nor marks the user's data/memory.json.
            patch.object(shared, "memory_store", sandbox_memory),
            patch.object(shared, "chat_store", sandbox_chats),
            patch.object(shared, "project_store", sandbox_projects),
            patch.object(shared, "folder_index", sandbox_index),
            patch.object(shared, "routine_store", sandbox_routines),
            patch.object(shared, "run_log", sandbox_runs),
            patch.object(sessions, "SESSION_PATH", os.path.join(sandbox, ".session.json")),
            patch.object(tasks.agent, "prep_gate", _PrepGate()),
            # A replay never touches the network: /props answers come from the fixture.
            patch.object(llm, "_http_json", side_effect=lambda url, timeout: _fixture_props(setup, url)),
            # The real ask_batch_approval_os runs (views, events); only the
            # user's answer is scripted, at the broker.
            patch.object(approval_path.broker, "request", side_effect=lambda actions, context_line="", **kw: fake_approval(actions)),
            patch.object(dispatch, "execute_tool", side_effect=run_tool),
            patch.object(system_metrics, "begin_model_load"),
            patch.object(system_metrics, "finish_model_load"),
        ]
        # "Record runs for replay" writes into the sandbox, never the real
        # data/recordings (a test that points REC_DIR elsewhere keeps its folder).
        if os.path.abspath(recorder.REC_DIR).startswith(os.path.join(config.BASE_DIR, "data")):
            patches.append(patch.object(recorder, "REC_DIR", os.path.join(sandbox, ".recordings")))
        # A replay never talks to a real Studio. setup "roblox": "fake" means the
        # test runs testing_fakes.FakeStudio against the real bridge instead.
        if setup.get("roblox") != "fake":
            patches.append(patch.object(roblox_bridge, "is_connected", return_value=False))
        for p in patches:
            p.start()
        try:
            if setup.get("entry", "agent") == "loop":
                shared.app_state["conversation"] = [{"role": "system", "content": prompts.SYSTEM_PROMPT_COMPACT},
                                                  {"role": "user", "content": fx["request"]}]
                shared.app_state["turn_policy"] = {"mode": setup.get("turn_policy", "action"),
                                                 "allow_mutation": setup.get("turn_policy", "action") == "action",
                                                 "reason": "replay"}
                result = agent_loop.run_agent_loop()
            else:
                shared.app_state["conversation"] = [{"role": "system", "content": prompts.SYSTEM_PROMPT_COMPACT}]
                req = SimpleNamespace(message=fx["request"], with_vision=False, mode=setup.get("mode", "local"),
                                      local_model=model_name, local_text_model=None)
                # entry "turn": the whole turn as the server runs it (tasks._run_turn:
                # orchestrator, then the chat is saved and a routine is offered).
                result = (tasks._run_turn(req) if setup.get("entry") == "turn"
                          else tasks.agent.run(req, board=tasks._board))
        finally:
            for p in reversed(patches):
                p.stop()
        return ReplayResult(result=result, approvals=approvals, executed=executed,
                            events=event_log.events_since(start_seq), requests=requests,
                            conversation=copy.deepcopy(shared.app_state["conversation"]),
                            sandbox=sandbox, unused_responses=len(model.responses), prep_cards=prep_cards)
    finally:
        shared.app_state.update(saved)
        shared.task_board.clear()
        model_caps.CAPS_PATH, model_caps._cache = saved_caps
        if own_tmp is not None:
            own_tmp.cleanup()


def check_expectations(fx: dict, run: ReplayResult) -> list:
    """Compare a replay against fixture["expect"]; returns a list of problems."""
    exp = fx.get("expect") or {}
    problems = []

    def want(cond, text):
        if not cond:
            problems.append(text)

    if "status" in exp:
        want(run.status == exp["status"], f"status {run.status!r} != {exp['status']!r}")
    for text in exp.get("message_contains", []):
        want(text.lower() in run.message.lower(), f"final message lacks {text!r}: {run.message[:200]!r}")
    if "approval_cards" in exp:
        want(len(run.approvals) == exp["approval_cards"], f"{len(run.approvals)} approval cards, expected {exp['approval_cards']}")
    if exp.get("no_empty_approvals", True):
        empty = [a for card in run.approvals for a in card if not a.get("args")]
        want(not empty, f"approval card with no arguments: {[a.get('tool_name') for a in empty]}")
    if "executed" in exp:
        names = [n for n, _ in run.executed]
        want(names == exp["executed"], f"executed {names}, expected {exp['executed']}")
    for name in exp.get("never_executed", []):
        want(all(n != name for n, _ in run.executed), f"{name} ran but must not")
    for kind, needle in exp.get("events_contain", []):
        want(any(e.get("kind") == kind and needle.lower() in json.dumps(e, default=str).lower() for e in run.events),
             f"no {kind} event containing {needle!r}")
    for needle in exp.get("tool_results_contain", []):
        want(any(m.get("role") == "tool" and needle.lower() in str(m.get("content", "")).lower() for m in run.conversation),
             f"no tool result containing {needle!r}")
    for name in exp.get("executed_once", []):
        want(sum(1 for n, _ in run.executed if n == name) == 1, f"{name} should run exactly once")
    if "max_model_requests" in exp:
        n = sum(1 for r in run.requests if r.get("tools"))
        want(n <= exp["max_model_requests"], f"{n} model requests, allowed {exp['max_model_requests']}")
    return problems
