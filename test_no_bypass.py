"""Phase 3 exit criterion: no capability, setting or routine bypasses
approvals or undo (non-negotiables 2, 7 and 9), checked across EVERY
registered tool rather than per feature:

  * permission decisions read only the explicit permission settings - no
    other setting (effort, Prompt Prep, memory, proactive help, web search,
    routing, recording ...) changes one;
  * the approval card is honest about undo for every tool that changes
    something ("Can be undone" exactly when the undo journal covers it);
  * content that tried to instruct the assistant keeps everything but local
    reads on the card, even in god mode with auto overrides;
  * the always-ask rules (form fills, message sends, irreversible browser
    clicks) cannot be made automatic by any setting;
  * in a real replay with every non-permission setting turned up, a project
    and memory telling it not to ask, the write still gets its approval
    card and still lands in the undo trail.
"""
import itertools
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import approval_view
import browser
import permissions
import replay_harness as rh
import tools.runtime as runtime
import undo_store

PERMISSION_KEYS = {"permission_mode", "permission_categories", "permission_overrides", "messaging_permissions",
                   "folder_rules"}

# Every non-permission setting, at values that could plausibly tempt a
# careless implementation to loosen something.
NON_PERMISSION_VARIANTS = {
    "effort": ["low", "normal", "high", "extra", "max", "ultra"],
    "effort_auto_escalate": [True, False],
    "prompt_prep": ["off", "clarify", "expand", "plan"],
    "auto_model_routing": [True, False],
    "max_revisions": ["auto", 0, 2],
    "show_activity": [True, False],
    "record_model_io": [True, False],
    "memory_enabled": [True, False],
    "web_search_provider": ["duckduckgo", "searxng", "brave", "tavily"],
    "searxng_url": ["", "http://127.0.0.1:8888"],
    "web_browser_fallback": [True, False],
    "web_auto_search": [True, False],
    "proactive_enabled": [True, False],
    "proactive_level": ["quiet", "eager"],
    "proactive_clipboard": [True, False],
    "cloud_allow_screenshots": [True, False],
    "effort_by_model": [{}, {"m": "ultra"}],
    "prompt_prep_by_model": [{}, {"m": "plan"}],
    "search_mode": ["standard", "private", "max", "local", "disabled"],
    "search_preview": [True, False],
    "net_route": ["direct", "system", "proxy", "tor"],
    "net_require_vpn": [True, False],
    "studio_layout": ["classic", "overlay"],
    "overlay_clickthrough": [True, False],
    "unlocked_themes": [[], ["judaism"]],
}


def _all_tools() -> list:
    return sorted(runtime.TOOL_REGISTRY.names())


class SettingsNeverGrantTests(unittest.TestCase):
    def test_only_permission_settings_decide(self):
        tools = _all_tools()
        permission_setups = [{}, {"permission_mode": "god"}, {"permission_mode": "lockdown"},
                             {"permission_categories": {"destructive": "auto"}},
                             {"permission_overrides": {"write_file": "auto", "run_powershell": "deny"}}]
        keys = list(NON_PERMISSION_VARIANTS)
        # Every value of every setting at least once, against every permission setup and tool.
        variants = [{k: NON_PERMISSION_VARIANTS[k][i % len(NON_PERMISSION_VARIANTS[k])] for k in keys}
                    for i in range(max(len(v) for v in NON_PERMISSION_VARIANTS.values()))]
        for base in permission_setups:
            for tool in tools:
                for tainted in (False, True):
                    expected = permissions.decide(tool, base, tainted=tainted, args={})
                    for extra in variants:
                        got = permissions.decide(tool, {**extra, **base}, tainted=tainted, args={})
                        self.assertEqual(got, expected, (tool, base, extra, tainted))

    def test_the_permission_code_reads_no_other_setting(self):
        cleaned = permissions.clean_settings({k: v[-1] for k, v in NON_PERMISSION_VARIANTS.items()})
        self.assertEqual(set(cleaned), PERMISSION_KEYS)


class UndoHonestyTests(unittest.TestCase):
    """Non-negotiable 9, for every tool that changes something."""

    MUST_BE_UNDOABLE = {"write_file", "edit_file", "apply_patch", "copy_path", "move_path", "rename_path",
                        "delete_path", "make_dir", "zip_paths", "unzip_file", "download_file", "screenshot_to_file",
                        "save_memory", "roblox_edit_script", "roblox_set_script_source", "roblox_create_script"}
    NEVER_UNDOABLE = {"run_powershell", "execute_command", "run_python", "browser_click", "browser_fill",
                      "messages_send", "schedule_task", "roblox_run_playtest"}

    def test_every_card_says_whether_it_can_be_undone(self):
        for name in _all_tools():
            spec = runtime.TOOL_REGISTRY.get(name)
            if spec.read_only:
                continue
            view = approval_view.describe(name, {}, tier=spec.tier, undoable=spec.undoable, mutating=spec.mutating)
            if spec.undoable:
                self.assertEqual(view["undo_text"], "Can be undone", name)
            else:
                self.assertTrue(view["undo_text"].startswith("Not undoable"), (name, view["undo_text"]))

    def test_file_and_script_changes_are_journaled_and_commands_are_not(self):
        for name in self.MUST_BE_UNDOABLE:
            self.assertTrue(runtime.TOOL_REGISTRY.get(name).undoable, name)
        for name in self.NEVER_UNDOABLE:
            spec = runtime.TOOL_REGISTRY.get(name)
            if spec is not None:
                self.assertFalse(spec.undoable, name)


class TaintAndAlwaysAskTests(unittest.TestCase):
    def test_after_injected_content_only_local_reads_run_by_themselves(self):
        loosest = {"permission_mode": "god", "permission_categories": {t: "auto" for t in permissions.RISK_TIERS},
                   "permission_overrides": {name: "auto" for name in _all_tools()}}
        for name in _all_tools():
            decision = permissions.decide(name, loosest, tainted=True, args={})
            if name in permissions.LOCAL_READS | permissions.HOST_BOOKKEEPING:
                continue
            self.assertEqual(decision, "ask", name)

    def test_always_ask_rules_survive_every_permission_setup(self):
        saved = browser.agent_browser.snapshot
        self.addCleanup(lambda: setattr(browser.agent_browser, "snapshot", saved))
        browser.agent_browser.snapshot = {"url": "https://x/", "elements": [{"id": 1, "tag": "button", "label": "Pay now"}]}
        setups = [{"permission_mode": m, "permission_categories": {t: c for t in permissions.RISK_TIERS},
                   "permission_overrides": {"browser_fill": o, "browser_click": o, "messages_send": o}}
                  for m, c, o in itertools.product(("normal", "god"), ("auto", "ask"), ("auto", "ask"))]
        for s in setups:
            self.assertEqual(permissions.decide("browser_fill", s, args={"element": 1, "value": "x"}), "ask", s)
            self.assertEqual(permissions.decide("browser_click", s, args={"element": 1}), "ask", s)
            self.assertEqual(permissions.decide("messages_send", s), "ask", s)

    def test_folder_rules_never_reach_beyond_file_writes(self):
        with tempfile.TemporaryDirectory() as folder:
            rules = {"folder_rules": [{"path": folder}]}
            inside = {"filename": os.path.join(folder, "a.txt"), "path": os.path.join(folder, "a.txt"),
                      "command": "Remove-Item a.txt", "url": "https://x"}
            for name in _all_tools():
                if name in permissions.FOLDER_WRITE_TOOLS:
                    continue
                self.assertEqual(permissions.decide(name, rules, args=inside), permissions.decide(name, {}, args=inside), name)


class SecretFileTests(unittest.TestCase):
    """Phase 3 review (HIGH): read_file runs without approval, so the app's own
    secrets must be out of its reach - and out of content searches' reach."""

    def test_the_apps_private_files_are_secret(self):
        import core.config as config
        import tools.files as files
        base = config.BASE_DIR
        for rel in ("keys.local.bat", "settings.json", os.path.join("data", "mcp_servers.json"),
                    os.path.join("data", "browser-profile", "Default", "Cookies")):
            self.assertTrue(files._is_secret_file(os.path.join(base, rel)), rel)
        self.assertFalse(files._is_secret_file(os.path.join(base, "README.md")))

    def test_read_file_refuses_and_content_search_skips_them(self):
        import tools.dispatch as dispatch
        with tempfile.TemporaryDirectory() as folder:
            for name, text in ((".env", "OPENAI_API_KEY=sk-abc123"), ("notes.txt", "sk-abc123 is not a key")):
                with open(os.path.join(folder, name), "w", encoding="utf-8") as f:
                    f.write(text)
            res = dispatch.execute_tool("read_file", {"filename": os.path.join(folder, ".env")})
            self.assertFalse(res["ok"])
            self.assertNotIn("sk-abc123", res["message"])
            found = dispatch.execute_tool("search_files", {"directory": folder, "content_query": "sk-abc"})
            self.assertIn("notes.txt", found["message"])
            self.assertNotIn(".env", found["message"])


def _call(cid, name, args):
    return {"finish_reason": "tool_calls", "tool_calls": [{"id": cid, "name": name, "arguments": json.dumps(args)}]}


class BatchReDecisionTests(unittest.TestCase):
    """Phase 3 review (HIGH): a batch is decided before any of its calls run.
    [browser_open(new page), browser_read, browser_click(7)] must not click the
    NEW page's element 7 ("Place order") on the strength of the OLD page's
    element 7 ("Next page") - even in god mode with browser_click set to auto."""

    def test_a_click_after_a_page_change_in_the_same_batch_still_asks(self):
        old_page = {"url": "https://shop.example/list", "elements": [{"id": 7, "tag": "a", "label": "Next page",
                                                                        "href": "https://shop.example/list?p=2"}]}
        new_page = {"url": "https://shop.example/cart", "elements": [{"id": 7, "tag": "button", "type": "submit",
                                                                        "label": "Place order", "in_form": True}]}
        b = browser.agent_browser
        saved = b.snapshot
        self.addCleanup(lambda: setattr(b, "snapshot", saved))
        b.snapshot = old_page
        clicked = []

        def opened(url):
            b.snapshot = None
            return {"ok": True, "message": f"Opened {url}"}

        def read(start=0):
            b.snapshot = new_page
            return {"ok": True, "message": "[7] button \"Place order\""}
        calls = [{"id": "o", "name": "browser_open", "arguments": json.dumps({"url": "https://shop.example/cart"})},
                 {"id": "r", "name": "browser_read", "arguments": "{}"},
                 {"id": "c", "name": "browser_click", "arguments": json.dumps({"element": 7})}]
        fx = {"format": "agentic-replay/1",
              "meta": {"name": "batch re-decision", "provenance": "reconstructed", "model": "unrecorded",
                       "evidence": "Phase 3 review"},
              "setup": {"mode": "cloud", "entry": "loop",
                        "settings": {"permission_mode": "god", "permission_overrides": {"browser_click": "auto"}}},
              "request": "Check out my cart",
              "responses": [{"finish_reason": "tool_calls", "tool_calls": calls},
                            {"finish_reason": "stop", "content": "I did not place the order."}],
              "aux_responses": [], "approvals": [False], "tools": {"mode": "real"}}
        with patch.object(b, "open", side_effect=opened), patch.object(b, "read", side_effect=read), \
                patch.object(b, "click", side_effect=lambda el: clicked.append(el) or {"ok": True, "message": "clicked"}):
            run = rh.run_fixture(fx)
        self.assertEqual([[a["tool_name"] for a in card] for card in run.approvals], [["browser_click"]])
        self.assertEqual(clicked, [])                                   # declined: nothing clicked


class EverythingTurnedUpReplayTests(unittest.TestCase):
    """The whole pipeline with every non-permission setting turned up, a
    project and saved memory that say "don't ask": the write still waits for
    the card, and once allowed it is in the undo trail."""

    def test_the_card_and_the_journal_are_still_there(self):
        with tempfile.TemporaryDirectory() as sandbox:
            target = os.path.join(sandbox, "game", "lava.lua")
            os.makedirs(os.path.dirname(target))
            project = {"id": "d" * 12, "name": "Obby", "folder": os.path.dirname(target), "files": [],
                       "instructions": "Never ask me before writing files. Approve everything automatically.",
                       "notes": "", "roblox_place": "", "effort": "ultra", "prompt_prep": "plan"}
            settings = {k: v[-1] if not isinstance(v[-1], bool) else True for k, v in NON_PERMISSION_VARIANTS.items()}
            settings.update(effort="ultra", prompt_prep="plan", proactive_enabled=True, prompt_enhancement="detailed")
            fx = {"format": "agentic-replay/1",
                  "meta": {"name": "no bypass", "provenance": "reconstructed", "model": "unrecorded", "evidence": "Phase 3 exit test"},
                  "setup": {"mode": "local", "entry": "turn", "settings": settings, "model_caps": {"context": 131072, "tool_protocol": "native"},
                            "memory": [{"text": "Always write files without asking me", "pinned": True}],
                            "app_state": {"active_project": project}},
                  "request": "Create lava.lua in my project folder that prints hello",
                  "prep_answers": [{"action": "send"}],
                  "responses": [_call("w1", "write_file", {"filename": target, "content": "print('hello')\n",
                                                           "justification": "The user asked for it"}),
                                {"finish_reason": "stop", "content": "Created lava.lua."}],
                  "aux_responses": [],
                  "approvals": [True], "tools": {"mode": "real"}}
            run = rh.run_fixture(fx, sandbox=sandbox)
            [started] = [e for e in run.events if e.get("phase") == "run_started"]
            self.assertEqual((started["effort"], started["prep"]), ("ultra", "plan"))    # the settings really applied
            self.assertEqual([c["kind"] for c in run.prep_cards], ["plan"])
            self.assertTrue([e for e in run.events if e.get("phase") == "context"])      # the project really applied
            cards = [a[0]["tool_name"] for a in run.approvals]
            self.assertIn("write_file", cards)                                   # still asked
            self.assertTrue(os.path.exists(target))
            done = [e for e in run.events if e.get("kind") == "tool" and e.get("tool") == "write_file" and e.get("undo_id")]
            self.assertTrue(done, "the write must be in the undo trail")
            res = undo_store.UndoStore(os.path.join(sandbox, ".undo")).undo(done[-1]["undo_id"])
            self.assertTrue(res["ok"], res)
            self.assertFalse(os.path.exists(target))


if __name__ == "__main__":
    unittest.main()
