"""Regression tests for the cut-off tool call bug (Cross-checked effort +
Detailed preparation): a native tool call whose arguments were truncated by
the length limit reached the approval card as "(no arguments)". Also covers
the runtime activity events the Workspace panel shows.

Only the model server is faked; parsing, the retry, the agent loop,
approval routing and the executor are the real code."""
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import agent.loop as agent_loop
import agent.models as llm
import agent.parsing as parsing
import core.config as config
import core.state as shared
import events as event_log
import tools.approvals as approvals
import tools.dispatch as dispatch
import tools.schemas as schemas
import llama_local
import permissions
import roblox_bridge


TRUNCATED = '{"command": "printf \'%s\' \'<!DOCTYPE html><html><head><style>body{margin:0} body{marg'
COMPLETE = json.dumps({"filename": "C:/tmp/snake.html", "content": "<html></html>", "justification": "Write the game"})


def _native(args: str, finish: str = "tool_calls", name: str = "execute_command", cid: str = "c1", content: str = ""):
    tc = SimpleNamespace(id=cid, function=SimpleNamespace(name=name, arguments=args))
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content, tool_calls=[tc]))])


def _say(text: str):
    return SimpleNamespace(choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content=text, tool_calls=None))])


class _LoopHarness:
    """Runs agent_loop.run_agent_loop with a scripted model and captures approvals."""

    def __init__(self, script, approve=False):
        self.script = iter(script)
        self.approve = approve
        self.approvals, self.executed, self.events = [], [], []

    def _record(self, real_emit):
        def emit(kind, label, **fields):
            event = real_emit(kind, label, **fields)       # the live dict finish_event later updates
            self.events.append((kind, label, event))
            return event
        return emit

    def run(self):
        saved = {k: shared.app_state.get(k) for k in ("conversation", "settings", "mode", "effort_thinking", "turn_policy")}
        real_emit = event_log.emit_event
        try:
            shared.app_state["conversation"] = [{"role": "user", "content": "make a snake game"}]
            shared.app_state["mode"] = config.MODE_CLOUD
            shared.app_state["turn_policy"] = {"mode": "action", "allow_mutation": True}
            shared.task_board.clear()
            shared.ledger.clear()
            with patch.object(llm, "call_model", side_effect=lambda: next(self.script)), \
                 patch.object(schemas, "tool_allowed_current_turn", return_value=True), \
                 patch.object(permissions, "decide", return_value="ask"), \
                 patch.object(approvals, "ask_batch_approval_os", side_effect=lambda actions: (self.approvals.append(actions), self.approve)[1]), \
                 patch.object(dispatch, "execute_tool", side_effect=lambda n, a: (self.executed.append((n, a)), {"ok": True, "message": "ok"})[1]), \
                 patch.object(event_log, "emit_event", side_effect=self._record(real_emit)):
                self.result = agent_loop.run_agent_loop()
            self.conversation = list(shared.app_state["conversation"])
        finally:
            shared.app_state.update(saved)
        return self.result


class ParsingTests(unittest.TestCase):
    def test_truncated_native_arguments_are_marked_malformed_not_empty(self):
        calls, _ = parsing.parse_response(_native(TRUNCATED, finish="length").choices[0].message)
        (_tc, name, args), = calls
        self.assertEqual(name, "execute_command")
        self.assertIsInstance(args, parsing._MalformedArgs)
        self.assertTrue(args.truncated)
        self.assertEqual(args.raw_len, len(TRUNCATED))

    def test_valid_and_genuinely_empty_arguments_are_unchanged(self):
        (_tc, _n, args), = parsing.parse_response(_native(COMPLETE, name="write_file").choices[0].message)[0]
        self.assertEqual(args["filename"], "C:/tmp/snake.html")
        self.assertNotIsInstance(args, parsing._MalformedArgs)
        for empty in ("", "{}", None):
            (_tc, _n, args), = parsing.parse_response(_native(empty, name="observe").choices[0].message)[0]
            self.assertEqual(args, {})
            self.assertNotIsInstance(args, parsing._MalformedArgs)

    def test_models_cannot_call_internal_or_unknown_tools(self):
        for name in ("desktop_route", "format_disk"):
            reason = agent_loop._invalid_call_reason(name, {})
            self.assertTrue(reason.startswith(f"NOT EXECUTED - '{name}' is not"), reason)
            self.assertIn("nothing ran", reason)

    def test_qwen_xml_tool_calls_in_text_are_recovered(self):
        # Exactly what Ternary Bonsai 2 printed in a real chat (it was shown as the reply).
        text = ("<tool_call>\n<function=web_read>\n<parameter=url>\n"
                "https://www.jpost.com/israel-news/politics-and-diplomacy/article-909521\n"
                "</parameter>\n</function>\n</tool_call>")
        calls, prose = parsing.parse_response(SimpleNamespace(content=text, tool_calls=None))
        (_tc, name, args), = calls
        self.assertEqual(name, "web_read")
        self.assertEqual(args, {"url": "https://www.jpost.com/israel-news/politics-and-diplomacy/article-909521"})
        self.assertEqual(prose, "")

    def test_qwen_xml_multiple_params_json_values_and_cut_off(self):
        text = ("Writing it now.\n<tool_call>\n<function=write_file>\n<parameter=filename>\nC:/tmp/a.txt\n</parameter>\n"
                "<parameter=content>\nline 1\nline 2\n</parameter>\n</function>\n</tool_call>")
        (_tc, name, args), = parsing.parse_response(SimpleNamespace(content=text, tool_calls=None))[0]
        self.assertEqual((name, args), ("write_file", {"filename": "C:/tmp/a.txt", "content": "line 1\nline 2"}))
        arr = "<function=zip_paths><parameter=paths>[\"a.txt\", \"b.txt\"]</parameter><parameter=output_zip>o.zip</parameter></function>"
        (_tc, _n, args), = parsing.parse_response(SimpleNamespace(content=arr, tool_calls=None))[0]
        self.assertEqual(args["paths"], ["a.txt", "b.txt"])
        cut = "<tool_call>\n<function=read_file>\n<parameter=filename>\nC:/notes.txt"      # length limit hit
        (_tc, name, args), = parsing.parse_response(SimpleNamespace(content=cut, tool_calls=None))[0]
        self.assertEqual((name, args), ("read_file", {"filename": "C:/notes.txt"}))
        # An invented name is still a call (not the reply): the loop refuses it
        # and names the closest real tools, so the model can retry.
        (_tc, name, args), = parsing.parse_response(SimpleNamespace(
            content="<tool_call><function=roblox_list_tree><parameter=tree_path>Workspace</parameter></function></tool_call>",
            tool_calls=None))[0]
        self.assertEqual((name, args), ("roblox_list_tree", {"tree_path": "Workspace"}))
        with patch.object(schemas, "tools_for_current_turn", return_value=[
                {"function": {"name": n}} for n in ("roblox_get_tree", "roblox_list_children", "read_file")]):
            reason = agent_loop._invalid_call_reason("roblox_list_tree", {"tree_path": "Workspace"})
        self.assertIn("not one of your tools", reason)
        self.assertIn("Closest: roblox_get_tree, roblox_list_children.", reason)
        # D36: and the whole list it may choose from
        self.assertIn("Your tools are exactly: read_file, roblox_get_tree, roblox_list_children.", reason)

    def test_invalid_reason_covers_malformed_and_missing_required(self):
        self.assertIn("cut off", agent_loop._invalid_call_reason("execute_command", parsing._MalformedArgs(TRUNCATED, True)))
        self.assertIn("missing required argument 'command'", agent_loop._invalid_call_reason("execute_command", {}))
        self.assertIsNone(agent_loop._invalid_call_reason("execute_command", {"command": "dir"}))


class RetryTests(unittest.TestCase):
    def test_native_call_cut_off_is_retried_with_more_room(self):
        again = _native(COMPLETE, name="write_file")
        with patch.object(llm, "_local_complete", return_value=again) as comp:
            out = llm._retry_if_cut_off(_native(TRUNCATED, finish="length"), {"max_tokens": 4096, "messages": []})
        self.assertIs(out, again)
        self.assertEqual(comp.call_args.args[0]["max_tokens"], 8192)
        self.assertIn("append=true", comp.call_args.args[0]["messages"][-1]["content"])

    def test_reply_that_ran_out_while_reasoning_is_retried_without_thinking(self):
        cut = _say("<think>" + "considering the layout " * 50)
        cut.choices[0].finish_reason = "length"
        again = _native(COMPLETE, name="write_file")
        with patch.object(llm, "_local_complete", return_value=again) as comp:
            llm._retry_if_cut_off(cut, {"max_tokens": 4096, "messages": [],
                                         "extra_body": {"cache_prompt": True, "chat_template_kwargs": {"enable_thinking": True}}})
        body = comp.call_args.args[0]["extra_body"]
        self.assertEqual(body["chat_template_kwargs"], {"enable_thinking": False})
        self.assertTrue(body["cache_prompt"])

    def test_complete_replies_are_not_retried(self):
        with patch.object(llm, "_local_complete") as comp:
            res = _native(COMPLETE, name="write_file")
            self.assertIs(llm._retry_if_cut_off(res, {"max_tokens": 2048, "messages": []}), res)
        comp.assert_not_called()

    def test_thinking_gets_its_own_reply_budget(self):
        saved = shared.app_state.get("effort_thinking")
        try:
            shared.app_state["effort_thinking"] = False
            plain = llm._local_reply_tokens()
            shared.app_state["effort_thinking"] = True
            self.assertEqual(llm._local_reply_tokens(), plain + config.LOCAL_THINKING_TOKENS)
        finally:
            shared.app_state["effort_thinking"] = saved


class AgentLoopTests(unittest.TestCase):
    def test_cut_off_call_never_reaches_approval_and_the_model_is_told(self):
        h = _LoopHarness([_native(TRUNCATED, finish="length", cid="bad"),
                          _native(COMPLETE, name="write_file", cid="good"),
                          _say("Wrote the file.")], approve=True)
        h.run()
        cards = [a for batch in h.approvals for a in batch]
        self.assertEqual([a["tool_name"] for a in cards], ["write_file"])       # the empty card is gone
        self.assertTrue(all(a["args"] for a in cards))
        self.assertEqual([n for n, _ in h.executed], ["write_file"])
        answer = next(m for m in h.conversation if m.get("role") == "tool" and m.get("tool_call_id") == "bad")
        self.assertIn("cut off by the output length limit", answer["content"])
        failed = [f for k, l, f in h.events if k == "tool" and f.get("tool") == "execute_command"]
        self.assertEqual(len(failed), 1)

    def test_every_tool_call_gets_exactly_one_result(self):
        h = _LoopHarness([_native(TRUNCATED, finish="length", cid="bad"), _say("Stopped.")])
        h.run()
        asked = [c["id"] for m in h.conversation if m.get("role") == "assistant" for c in m.get("tool_calls") or []]
        answered = [m["tool_call_id"] for m in h.conversation if m.get("role") == "tool"]
        self.assertEqual(sorted(asked), sorted(answered))

    def test_repeated_unusable_calls_stop_with_a_clear_message(self):
        h = _LoopHarness([_native(TRUNCATED, finish="length", cid=f"b{i}") for i in range(10)])
        res = h.run()
        self.assertEqual(res["status"], "error")
        self.assertIn("nothing was run", res["message"])
        self.assertEqual(h.approvals, [])
        self.assertEqual(h.executed, [])

    def test_missing_required_argument_is_rejected_before_approval(self):
        h = _LoopHarness([_native("{}", cid="empty"), _say("ok")])
        h.run()
        self.assertEqual(h.approvals, [])
        self.assertEqual(h.executed, [])


class ActivityEventTests(unittest.TestCase):
    def test_model_calls_and_notes_are_reported(self):
        call = _native(COMPLETE, name="write_file", content="I'll write the game to one file first.")
        call.usage = SimpleNamespace(completion_tokens=321, prompt_tokens=1500, completion_tokens_details=None)
        h = _LoopHarness([call, _say("Done.")], approve=True)
        h.run()
        models = [f for k, l, f in h.events if k == "model"]
        self.assertEqual(len(models), 2)
        self.assertEqual(models[0]["status"], "done")               # finished in place by finish_event
        self.assertEqual(models[0]["tokens_out"], 321)
        self.assertEqual(models[0]["label"], "Model replied")
        self.assertIn("duration_ms", models[0])
        notes = [(l, f) for k, l, f in h.events if k == "thought"]
        self.assertEqual(len(notes), 1)                              # the final answer is not a "note"
        self.assertEqual(notes[0][1]["note"], "I'll write the game to one file first.")
        self.assertEqual(notes[0][1]["next_tools"], ["write_file"])
        json.dumps([f for _k, _l, f in h.events], default=str)      # everything is serializable

    def test_reasoning_excerpt_is_trimmed_and_secrets_are_hidden(self):
        msg = SimpleNamespace(content="<think>" + "step " * 400 + "then use api_key=sk-abcdef1234567890XYZ to call</think>",
                              tool_calls=None)
        with patch.object(event_log, "emit_event") as emit:
            agent_loop._emit_model_note(msg, "", ["execute_command"])
        _kind, label = emit.call_args.args
        fields = emit.call_args.kwargs
        self.assertEqual(label, "Reasoned before acting")
        self.assertLessEqual(len(fields["reasoning"]), 601)
        self.assertTrue(fields["reasoning"].startswith("…"))
        self.assertNotIn("sk-abcdef", fields["reasoning"])
        self.assertIn("[hidden]", fields["reasoning"])
        self.assertGreater(fields["reasoning_words"], 400)


class WriteFileAppendTests(unittest.TestCase):
    def test_long_files_can_be_written_in_parts(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "game.html")
            self.assertTrue(dispatch.execute_tool("write_file", {"filename": path, "content": "<html>"})["ok"])
            res = dispatch.execute_tool("write_file", {"filename": path, "content": "</html>", "append": True})
            self.assertTrue(res["ok"])
            self.assertIn("Appended", res["message"])
            self.assertEqual(Path(path).read_text(encoding="utf-8"), "<html></html>")
            dispatch.execute_tool("write_file", {"filename": path, "content": "new"})
            self.assertEqual(Path(path).read_text(encoding="utf-8"), "new")


class LocalToolSetTests(unittest.TestCase):
    def _names(self, ctx):
        # The catalogue each window gets - with skills off; test_skills covers what a request is shown of it.
        saved = shared.app_state.get("turn_policy")
        try:
            shared.app_state["turn_policy"] = {"mode": "action"}
            with patch.object(llama_local, "model_context_limit", return_value=ctx), \
                 patch.object(roblox_bridge, "is_connected", return_value=False), \
                 patch.dict(shared.app_state["settings"], {"skills_on": False}):
                return {t["function"]["name"] for t in schemas.tools_for_current_turn(local=True)}
        finally:
            shared.app_state["turn_policy"] = saved

    def test_small_context_models_still_get_file_and_system_tools(self):
        names = self._names(8192)
        for tool in ("write_file", "read_file", "edit_file", "list_dir", "make_dir", "move_path",
                     "delete_path", "search_files", "open_application", "kill_process", "execute_command"):
            self.assertIn(tool, names)
        self.assertNotIn("zip_paths", names)                      # still a compact set

    def test_large_context_models_get_the_full_catalogue(self):
        names = self._names(250_000)
        self.assertIn("zip_paths", names)
        self.assertIn("schedule_task", names)
        self.assertIn("write_file", names)

    def test_read_only_turns_stay_read_only(self):
        saved = shared.app_state.get("turn_policy")
        try:
            shared.app_state["turn_policy"] = {"mode": "read"}
            with patch.object(llama_local, "model_context_limit", return_value=250_000):
                names = {t["function"]["name"] for t in schemas.tools_for_current_turn(local=True)}
        finally:
            shared.app_state["turn_policy"] = saved
        self.assertIn("read_file", names)
        self.assertNotIn("write_file", names)


class UiTests(unittest.TestCase):
    html = Path(__file__).resolve().parent.parent.joinpath("index.html").read_text(encoding="utf-8")

    def test_effort_names_have_no_deep(self):
        self.assertIsNone(re.search(r"['\"]Deep['\"]|\bDeep\b", self.html))
        self.assertIn("max: 'Max'", self.html)
        import orchestrator
        self.assertEqual(orchestrator.EFFORT_LABELS["max"], "Max")

    def test_activity_feed_exists_and_model_rows_stay_out_of_chat_steps(self):
        self.assertIn('id="sec-activity"', self.html)
        self.assertIn("const PANEL_ONLY = new Set(['model', 'thought'])", self.html)
        self.assertIn("ingestActivity(d.events)", self.html)


if __name__ == "__main__":
    unittest.main()
