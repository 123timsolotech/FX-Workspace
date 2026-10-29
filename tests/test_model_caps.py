"""Per-model capability detection (model_caps.py): facts come from the
server's /props and from observed replies, never from model names."""
import json
import os
import tempfile
import unittest
from types import SimpleNamespace

import model_caps


class _Isolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = (model_caps.CAPS_PATH, model_caps._cache)
        model_caps.CAPS_PATH = os.path.join(self.tmp.name, "model_caps.json")
        model_caps._cache = None

    def tearDown(self):
        model_caps.CAPS_PATH, model_caps._cache = self.saved
        self.tmp.cleanup()


PROPS_NATIVE = {"build_info": "b10621-c1d0e7a00", "chat_template": "{% if tools %}...<tool_call>...{% endif %}<think>",
                "chat_template_caps": {"supports_tool_calls": True, "supports_tools": True,
                                       "supports_parallel_tool_calls": True},
                "default_generation_settings": {"n_ctx": 32768}}


class DetectionTests(_Isolated):
    def test_props_with_tool_support_mean_native_protocol(self):
        rec = model_caps.detect("Qwen-3-8B", "http://x", lambda url, t: PROPS_NATIVE, loaded=True)
        self.assertEqual(rec["tool_protocol"], "native")
        self.assertEqual(rec["context"], 32768)
        self.assertEqual(rec["reasoning"], "template")
        self.assertEqual(rec["server_build"], "b10621-c1d0e7a00")
        with open(model_caps.CAPS_PATH, encoding="utf-8") as f:
            self.assertIn("Qwen-3-8B", json.load(f))            # persisted

    def test_template_without_tools_means_generic(self):
        props = {**PROPS_NATIVE, "chat_template_caps": {"supports_tool_calls": False}, "chat_template": "plain"}
        rec = model_caps.detect("m", "http://x", lambda url, t: props, loaded=True)
        self.assertEqual(rec["tool_protocol"], "generic")
        self.assertNotIn("reasoning", {k for k, v in rec.items() if v})

    def test_unloaded_model_is_never_queried(self):
        calls = []
        model_caps.detect("m", "http://x", lambda url, t: calls.append(url) or PROPS_NATIVE, loaded=False)
        self.assertEqual(calls, [])

    def test_the_name_alone_says_nothing(self):
        # A name that "sounds" capable gets no capabilities until the server says so.
        self.assertIsNone(model_caps.protocol("Qwen-3-8B-Instruct-ToolCalling"))

    def test_a_runtime_refusal_outranks_props(self):
        model_caps.update("m", tool_protocol="schema")
        rec = model_caps.detect("m", "http://x", lambda url, t: PROPS_NATIVE, loaded=True)
        self.assertEqual(rec["tool_protocol"], "schema")

    def test_new_server_build_starts_over(self):
        model_caps.update("m", tool_protocol="text", server_build="b1")
        rec = model_caps.detect("m", "http://x", lambda url, t: PROPS_NATIVE, loaded=True)
        self.assertEqual(rec["tool_protocol"], "native")

    def test_server_errors_leave_the_record_alone(self):
        def boom(url, t):
            raise ConnectionError("down")
        self.assertEqual(model_caps.detect("m", "http://x", boom, loaded=True), {})


class ObservationTests(_Isolated):
    def test_reasoning_channel_and_inline_think(self):
        model_caps.observe_reply("a", SimpleNamespace(content="ok", reasoning_content="step by step"))
        model_caps.observe_reply("b", SimpleNamespace(content="<think>hmm</think>ok", reasoning_content=None))
        self.assertEqual(model_caps.get("a")["reasoning"], "channel")
        self.assertEqual(model_caps.get("b")["reasoning"], "inline")

    def test_refusal_messages(self):
        self.assertTrue(model_caps.tools_refused("tools param requires --jinja flag"))
        self.assertFalse(model_caps.tools_refused("context size exceeded"))
        self.assertTrue(model_caps.schema_refused("unsupported response_format"))

    def test_reply_schema_offers_each_tool_and_an_answer(self):
        tools = [{"function": {"name": "list_dir", "parameters": {"type": "object", "properties": {"directory": {"type": "string"}}}}}]
        schema = model_caps.reply_schema(tools)
        self.assertEqual(schema["oneOf"][0]["properties"]["tool"], {"const": "list_dir"})
        self.assertEqual(schema["oneOf"][-1]["required"], ["answer"])
        self.assertIn("list_dir(directory?: string)", model_caps.catalogue_prompt(tools))


class RouterContextTests(unittest.TestCase):
    """Verified on llama-server b10621: the router's global --ctx-size overrides a
    preset's ctx-size, and /v1/models reports each model's real command line."""

    def test_real_ctx_comes_from_the_router_not_models_ini(self):
        import llama_local
        payload = {"data": [
            {"id": "Qwen-3.5-4B-Q4_K_M", "status": {"value": "unloaded", "args": [
                "llama-server.exe", "--jinja", "--ctx-size", "65536", "--model", "x.gguf"]}},
            {"id": "Loaded-One", "status": {"value": "loaded", "args": ["-c", "8192", "--mmproj", "p.mmproj"]}},
            {"id": "No-Status"}]}
        status = llama_local._router_status(payload)
        self.assertEqual(status["Qwen-3.5-4B-Q4_K_M"], {"loaded": False, "ctx": 65536, "mmproj": False,
                                                         "exe": "llama-server.exe"})             # which build (D29)
        self.assertEqual(status["Loaded-One"], {"loaded": True, "ctx": 8192, "mmproj": True, "exe": None})  # reads images
        self.assertNotIn("No-Status", status)
        saved = dict(llama_local._cache)
        try:
            llama_local._cache["status"] = status
            self.assertEqual(llama_local.model_context_limit("Qwen-3.5-4B-Q4_K_M"), 65536)
        finally:
            llama_local._cache.clear()
            llama_local._cache.update(saved)


if __name__ == "__main__":
    unittest.main()
