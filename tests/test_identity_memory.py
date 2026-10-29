"""1.4.2 Phase 5: the soul file's header, one value per subject, the memory overview and notes, Work sessions."""
import os
import tempfile
import unittest
from unittest.mock import patch

import core.state as shared
import identity
import memory
import memory_graph
import profile_store


class IdentityTests(unittest.TestCase):
    def test_the_header_says_whose_file_it_is(self):
        b = identity.block("My name is FX.")
        self.assertIn("defines YOU, the assistant", b)
        self.assertIn('"I" and "my" in it mean you', b)
        self.assertEqual(identity.block("  "), "")
        with patch.object(profile_store, "get", return_value={"text": "I build Roblox games."}):
            self.assertIn("PERSON YOU ARE TALKING TO, not you", profile_store.brief())

    def test_save_and_read(self):
        d = tempfile.mkdtemp()
        with patch.object(identity, "PATH", os.path.join(d, "identity", "soul.md")):
            identity.save("# Me\r\nI am FX")
            self.assertEqual(identity.get(), "# Me\nI am FX\n")

    def test_it_goes_in_the_system_prompt(self):
        import agent.models as models
        with patch.object(identity, "get", return_value="My name is Nova."), \
                patch.dict(shared.app_state, raw_mode=False, design_mode=False, turn_persona=""):
            self.assertIn("My name is Nova.", models.system_prompt(True))


class MemoryGraphTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = memory.MemoryStore(os.path.join(self.dir, "memory.json"))
        for target in (patch.object(shared, "memory_store", self.store),
                       patch.dict(shared.app_state, settings={"memory_enabled": True})):
            target.start()
            self.addCleanup(target.stop)

    def test_a_new_value_replaces_the_old_one(self):
        a = self.store.add("My GPU is an RTX 3060")
        b = self.store.add("my GPU is an RTX 4070")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual([e["text"] for e in self.store.list()], ["my GPU is an RTX 4070"])
        self.store.add("I prefer short answers")
        self.store.add("I prefer dark themes")                       # preferences sit side by side
        self.assertEqual(len(self.store.list()), 3)
        self.assertEqual(memory.subject_key("My name is Shon"), "name")
        self.assertEqual(memory.subject_key("I live in Haifa"), "i live at")
        self.assertIsNone(memory.subject_key("Use 4-space indents in Lua"))

    def test_overview_is_one_line_per_topic(self):
        self.store.add("My main Roblox place is Obby World")
        self.store.add("The Obby World game has 12 levels")
        self.store.add("I prefer short answers")
        ov = memory_graph.overview_block()
        self.assertIn("MEMORY OVERVIEW", ov)
        self.assertIn("Projects: 2 memories", ov)
        self.assertNotIn("Obby World", ov)            # what they say goes only with a request they match
        self.assertLessEqual(len(ov), memory_graph.OVERVIEW_MAX + 200)
        with patch.dict(shared.app_state["settings"], memory_overview=False):
            self.assertEqual(memory_graph.overview_block(), "")

    def test_notes_and_links(self):
        self.store.add("My main Roblox place is Obby World")
        self.store.add("Obby World needs a Saturday playtest")      # Work? no - 'playtest' isn't work; topic guessed
        self.store.update(self.store.list()[1]["id"], topic="Work")
        written = memory_graph.write_notes()
        names = sorted(os.path.basename(p) for p in written)
        self.assertEqual(names, ["projects.md", "work.md"])
        with open(os.path.join(memory_graph.notes_dir(), "projects.md"), encoding="utf-8") as f:
            text = f.read()
        self.assertIn("generator: fx-memory-notes", text)
        self.assertIn("[[Work]]", text)                              # both mention Obby World
        g = memory_graph.graph()
        self.assertIn("e:Obby World", {n["id"] for n in g["nodes"]})

    def test_upkeep_drops_duplicates_and_writes_notes(self):
        self.store.add("Use 4-space indents in Lua")
        e = self.store.add("Use 4 space indents in Lua.")
        self.assertEqual(len(self.store.list()), 2)                  # not exactly the same text
        with patch.object(shared, "project_store", None):
            r = memory_graph.maintain()
        self.assertEqual(len(r["removed"]), 1)
        self.assertEqual(len(self.store.list()), 1)
        self.assertGreaterEqual(r["notes"], 1)
        self.assertTrue(e)


class SessionKindTests(unittest.TestCase):
    def test_work_sessions_leave_out_reminders_memory_and_notes_unless_asked(self):
        import tools.schemas as schemas
        with patch.dict(shared.app_state, active_project="p1", current_user_request="fix the build script"):
            self.assertEqual(schemas.session_kind(), "work")
            self.assertEqual(schemas.session_off(), {"reminders", "memory", "notes"})
        with patch.dict(shared.app_state, active_project="p1", current_user_request="remember that the build uses Lua 5.1"):
            self.assertNotIn("memory", schemas.session_off())
        with patch.dict(shared.app_state, active_project=None, current_user_request="fix the build script"):
            self.assertEqual(schemas.session_kind(), "assistant")
            self.assertEqual(schemas.session_off(), set())


if __name__ == "__main__":
    unittest.main()
