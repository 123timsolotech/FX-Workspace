"""Personas (personas.py), what voice mode may approve (approval_broker.voice_ok), and Judaism Mode
staying out of Settings: a persona is cleaned and resolved on the server and lands in the system
prompt; IsraelGPT is never listed; a spoken "approve" can only allow low-risk actions."""
import inspect
import os
import unittest

import main  # noqa: F401  (wires the app together)
import approval_broker
import core.config as config
import core.state as shared
import personas

HERE = os.path.dirname(os.path.abspath(__file__))


class PersonaSettingsTests(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(config.DEFAULT_SETTINGS["persona"], "custom")
        self.assertEqual(config.DEFAULT_SETTINGS["personas"], [{"id": "custom", "name": "Custom", "text": ""}])
        self.assertEqual(personas.clean_settings({}), {"personas": [{"id": "custom", "name": "Custom", "text": ""}], "persona": "custom", "ai_immersive": False})

    def test_cleaning(self):
        raw = {"personas": [
            {"id": "p-tutor", "name": "  Maths   tutor ", "text": " Be patient. "},
            {"id": "p-tutor", "name": "Duplicate", "text": "x"},              # same id: dropped
            {"id": "israelgpt", "name": "Fake", "text": "x"},                # a built-in can't be saved over
            {"id": "../evil", "name": "Bad id", "text": "x"},
            {"id": "p-noname", "name": "   ", "text": "x"},
            "not a dict",
            {"id": "custom", "name": "Renamed", "text": "Short answers." + "x" * 5000},
        ], "persona": "israelgpt"}
        out = personas.clean_settings(raw)
        self.assertEqual([p["id"] for p in out["personas"]], ["custom", "p-tutor"])      # Custom first, always
        self.assertEqual(out["personas"][0]["name"], "Custom")
        self.assertEqual(len(out["personas"][0]["text"]), personas.MAX_TEXT)
        self.assertEqual(out["personas"][1], {"id": "p-tutor", "name": "Maths tutor", "text": "Be patient."})
        self.assertEqual(out["persona"], "custom")                     # the built-in is never the saved choice
        many = {"personas": [{"id": f"p{i}", "name": f"P{i}", "text": ""} for i in range(50)]}
        self.assertEqual(len(personas.clean_settings(many)["personas"]), personas.MAX_PERSONAS)
        self.assertEqual(config.clean_privacy_settings({"personas": raw["personas"]})["personas"], out["personas"])

    def test_resolve(self):
        s = {"personas": [{"id": "custom", "name": "Custom", "text": "Call me Captain."},
                          {"id": "p-blunt", "name": "Blunt", "text": "Be blunt."}], "persona": "p-blunt"}
        self.assertEqual(personas.resolve(s, "custom"), "Call me Captain.")
        self.assertEqual(personas.resolve(s, "p-blunt"), "Be blunt.")
        self.assertEqual(personas.resolve(s, None), "Be blunt.")                   # no choice sent: the saved one
        self.assertEqual(personas.resolve(s, "missing"), "")
        self.assertIn("IsraelGPT", personas.resolve({}, "israelgpt"))
        self.assertIn("Shalom", personas.resolve({}, "israelgpt"))
        mossad = personas.resolve({}, "mossadgpt")
        self.assertIn("MossadGPT", mossad)
        self.assertIn("BLUF", mossad)
        self.assertIn("never claim to be, speak for or have access to any real intelligence agency", mossad)
        self.assertIn("never invent classified", mossad)
        self.assertEqual(personas.clean_settings({"persona": "mossadgpt"})["persona"], "custom")   # never a saved choice
        self.assertEqual(personas.resolve({}, "custom"), "")                        # empty Custom: its normal self
        self.assertIn("never overrides safety", personas.block("x"))

    def test_it_reaches_the_system_prompt(self):
        import agent.models as models
        import agent.tasks as tasks
        src = inspect.getsource(models.system_prompt)                              # build_messages and the fit both use it
        self.assertIn("personas.block(shared.app_state['turn_persona'])", src)
        self.assertIn("system_prompt(local, base_prompt)", inspect.getsource(models.build_messages))
        begin = inspect.getsource(tasks._begin_composer_tools)
        self.assertIn('shared.app_state["turn_persona"] = personas.resolve(', begin)
        self.assertIn('"turn_persona"', inspect.getsource(tasks._run_turn_in_chat))      # this turn's only
        self.assertEqual(shared.ChatRequest(message="hi", persona="p-x").persona, "p-x")

    def test_build_messages_puts_it_in_the_system_message(self):
        import agent.models as models
        keys = ("turn_persona", "conversation", "mode", "design_mode", "turn_voice", "turn_instructions")
        saved = {k: shared.app_state.get(k) for k in keys}
        self.addCleanup(lambda: [shared.app_state.__setitem__(k, v) if v is not None else shared.app_state.pop(k, None)
                                 for k, v in saved.items()])
        shared.app_state.update(turn_persona="Talk like a pirate.", design_mode=False, turn_voice=False, turn_instructions="",
                                conversation=[{"role": "system", "content": "x"}, {"role": "user", "content": "hi"}])
        msgs = models.build_messages()
        self.assertIn("PERSONA", msgs[0]["content"])
        self.assertIn("Talk like a pirate.", msgs[0]["content"])
        shared.app_state["turn_persona"] = ""
        self.assertNotIn("PERSONA (", models.build_messages()[0]["content"])


class VoiceApprovalTests(unittest.TestCase):
    def act(self, tool, **extra):
        return {"tool": tool, "args": {}, **extra}

    def test_only_low_risk_actions(self):
        ok = approval_broker.voice_ok
        self.assertTrue(ok([self.act("open_application")]))
        self.assertTrue(ok([self.act("browser_open"), self.act("media_control")]))
        self.assertTrue(ok([self.act("snap_windows")]))
        for tool in ("execute_command", "run_python", "type_text", "click_at", "keyboard_action", "delete_path", "write_file",
                     "download_file", "browser_fill", "browser_click", "messages_send", "open_file_in_os", "mcp__x__y", ""):
            self.assertFalse(ok([self.act(tool)]), tool)
        self.assertFalse(ok([self.act("open_application"), self.act("execute_command")]))      # one risky action spoils it
        self.assertFalse(ok([self.act("open_application", warning="Looks like a phishing page")]))
        self.assertFalse(ok([self.act("open_application")] * 4))                                  # too many to hear at once
        self.assertFalse(ok([]))
        # The tier is worked out again on the server, not taken from the card's description.
        self.assertFalse(ok([self.act("execute_command", tier="benign")]))

    def test_the_broker_refuses_a_spoken_approval_of_anything_else(self):
        broker = approval_broker.ApprovalBroker(native_ask=lambda a, c: True)

        def pend(tool):
            import threading
            broker._pending = {"id": "abc", "actions": [{"tool": tool}], "context": "", "created_at": 0,
                               "answer": None, "event": threading.Event()}
        pend("execute_command")
        self.assertFalse(broker.pending()["voice_ok"])
        self.assertFalse(broker.answer("abc", True, "voice"))              # refused, still pending
        self.assertIsNone(broker._pending["answer"])
        self.assertTrue(broker.answer("abc", False, "voice"))              # "cancel" always works
        pend("execute_command")
        self.assertTrue(broker.answer("abc", True))                        # a click can approve it
        pend("open_application")
        self.assertTrue(broker.pending()["voice_ok"])
        self.assertTrue(broker.answer("abc", True, "voice"))
        self.assertTrue(broker._pending["answer"])

    def test_the_endpoint_passes_it_through(self):
        import api.server as server
        src = inspect.getsource(server.answer_approval)
        self.assertIn('via = "voice" if body.via == "voice" else ""', src)
        self.assertIn("approvals.broker.answer(request_id, body.approved, via,", src)


class PageTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
            self.html = f.read()

    def block(self, start, end):
        i = self.html.index(start)
        return self.html[i:self.html.index(end, i)]

    def test_judaism_mode_never_appears_in_settings(self):
        appearance = self.block("function renderAppearance() {", "\nfunction ")
        for s in ("SECRET_THEME_INFO", "setSecretTheme", "unlocked_themes", "Judaism", "SecretFx"):
            self.assertNotIn(s, appearance, s)
        self.assertNotIn("relockSecretTheme", self.html)
        self.assertNotIn("in Settings > Appearance now", self.html)
        self.assertIn("n >= 6", self.html)                                   # the secret way still works

    def test_persona_picker(self):
        self.assertIn('id="persona-btn"', self.html)
        js = self.block("/* ---- Personas (personas.py)", "// ---- files")
        self.assertIn("persona: personaForTurn()", self.html)                # sent with every message
        # IsraelGPT (by day) and MossadGPT (after dark): only in Judaism Mode, in Custom's place, never saved or editable.
        self.assertIn("if (p.id === 'custom' && activeSecretTheme() === 'judaism') return secretPersona();", js)
        self.assertIn("activeSecretTheme() === 'judaism' ? (colorScheme() === 'dark' ? MOSSADGPT : ISRAELGPT) : ''", js)
        self.assertNotIn("editPersona('israelgpt')", js)
        self.assertNotIn("editPersona('mossadgpt')", js)
        self.assertIn("renderPersonaBtn();", self.block("function applyAppearance() {", "\nfunction "))


if __name__ == "__main__":
    unittest.main()
