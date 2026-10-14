"""Offline checks for routing and deterministic desktop execution."""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import desktop_router
import intent_router
import approval
import app_registry
import system_metrics


class IntentRouterTests(unittest.TestCase):
    def test_turn_policy_separates_chat_read_and_action(self):
        self.assertEqual(intent_router.classify_turn("hi")["mode"], "none")
        self.assertEqual(intent_router.classify_turn("How do I open Proton VPN?")["mode"], "none")
        self.assertEqual(intent_router.classify_turn("What windows are open?")["mode"], "read")
        self.assertEqual(intent_router.classify_turn("Can you open Proton VPN?")["mode"], "action")
        self.assertEqual(intent_router.classify_turn("Hey, open Notepad")["mode"], "action")

    def test_music_and_scan_routes_both_tasks(self):
        routed = intent_router.route("Play music in tidal and scan C with wiztree")
        self.assertEqual(routed.app_name, "tidal")
        self.assertEqual(routed.actions, ["play"])
        self.assertEqual(routed.remaining_task, "scan C with wiztree")

    def test_named_music_is_not_replaced_with_current_queue(self):
        self.assertIsNone(intent_router.route("Play my favorites in tidal"))

    def test_explicit_proton_vpn_connect(self):
        routed = intent_router.route("open proton vpn and connect")
        self.assertIsInstance(routed, intent_router.DesktopIntent)
        self.assertEqual(routed.app_name, "Proton VPN")
        self.assertEqual(routed.actions, ["connect"])

    def test_ambiguous_proton(self):
        routed = intent_router.route("Open proton")
        self.assertIsInstance(routed, intent_router.Clarification)
        response = routed.as_response()
        self.assertIn("Proton VPN", response["options"])
        self.assertEqual(response["option_values"][0], "Open Proton VPN")

    def test_connect_to_proton_preserves_connect_intent(self):
        routed = intent_router.route("Connect to proton")
        self.assertIsInstance(routed, intent_router.Clarification)
        response = routed.as_response()
        self.assertEqual(response["options"], ["Proton VPN", "Proton Mail", "Proton Drive"])
        self.assertEqual(response["option_values"][0], "Connect to Proton VPN")

    def test_old_frontend_answer_resolves_once(self):
        routed = intent_router.resolve_answer("Connect to proton", "Proton VPN")
        self.assertIsInstance(routed, intent_router.DesktopIntent)
        self.assertEqual(routed.app_name, "Proton VPN")
        self.assertEqual(routed.actions, ["connect"])

    def test_custom_answer_is_not_forced_back_to_proton(self):
        routed = intent_router.resolve_answer("Connect to proton", "Windscribe")
        self.assertIsInstance(routed, intent_router.DesktopIntent)
        self.assertEqual(routed.app_name, "Windscribe")
        self.assertEqual(routed.actions, ["connect"])

    def test_non_app_request_is_not_hijacked(self):
        self.assertIsNone(intent_router.route("Explain how VPN tunnelling works"))

    def test_open_and_scan_separates_app_from_action(self):
        routed = intent_router.route("open wiztree and scan C")
        self.assertEqual(routed.app_name, "wiztree")
        self.assertEqual(routed.remaining_task, "scan C")

    def test_compound_clarification_retains_scan_for_all_answer_formats(self):
        original = "Connect to proton and scan C with wiztree"
        question = intent_router.route(original)
        self.assertIsInstance(question, intent_router.Clarification)
        for answer in ["Proton VPN", "Connect to Proton VPN", question.as_response()["option_values"][0]]:
            with self.subTest(answer=answer):
                resolved = intent_router.resolve_answer(original, answer)
                self.assertEqual(resolved.app_name, "Proton VPN")
                self.assertEqual(resolved.actions, ["connect"])
                self.assertEqual(resolved.remaining_task, "scan C with wiztree")

    def test_scan_routes_to_named_app(self):
        for message in ["Scan drive C with wiztree", "scan C: using WizTree"]:
            routed = intent_router.route(message)
            self.assertEqual(routed.app_name.casefold(), "wiztree")
            self.assertEqual(routed.actions, ["scan"])
            self.assertEqual(routed.arguments["drive"], "C:")

    def test_screenshot_sentence_compiles_before_app_lookup(self):
        tasks = intent_router.compile_request(
            "play music on tidal, scan drive G in wiztree and establish vpn connection in proton")
        self.assertEqual([t["compiled"]["kind"] for t in tasks], ["desktop"] * 3)
        self.assertEqual([t["compiled"]["app_name"] for t in tasks], ["tidal", "wiztree", "Proton VPN"])
        self.assertEqual([t["compiled"]["actions"] for t in tasks], [["play"], ["scan"], ["connect"]])
        self.assertEqual(tasks[1]["compiled"]["arguments"], {"drive": "G:"})

    def test_latest_reported_sentence_is_fully_host_compiled(self):
        tasks = intent_router.compile_request("Prev song, scan drive G and connect to proton")
        self.assertEqual([task["compiled"]["kind"] for task in tasks],
                         ["desktop", "desktop", "clarification"])
        self.assertEqual(tasks[0]["compiled"]["app_name"], "active media")
        self.assertEqual(tasks[0]["compiled"]["actions"], ["previous"])
        self.assertEqual(tasks[1]["compiled"]["app_name"], "WizTree")
        self.assertEqual(tasks[1]["compiled"]["actions"], ["scan"])
        self.assertEqual(tasks[1]["compiled"]["arguments"], {"drive": "G:"})

    def test_bare_drive_scan_defaults_to_wiztree(self):
        routed = intent_router.route("scan drive G")
        self.assertEqual(routed.app_name, "WizTree")
        self.assertEqual(routed.actions, ["scan"])
        self.assertEqual(routed.arguments, {"drive": "G:"})

    def test_skip_disconnect_and_bare_scan_are_all_host_actions(self):
        tasks = intent_router.compile_request("Skip song, disconnect from vpn, scan drive g")
        self.assertEqual([task["compiled"]["kind"] for task in tasks], ["desktop"] * 3)
        self.assertEqual([task["compiled"]["app_name"] for task in tasks],
                         ["active media", "Proton VPN", "WizTree"])
        self.assertEqual([task["compiled"]["actions"] for task in tasks],
                         [["next"], ["disconnect"], ["scan"]])
        self.assertEqual(tasks[2]["compiled"]["arguments"], {"drive": "G:"})

    def test_generic_pause_close_and_vpn_status_are_host_actions(self):
        pause_close = intent_router.compile_request("pause music and close wiztree")
        self.assertEqual([task["compiled"]["actions"] for task in pause_close], [["pause"], ["close"]])
        self.assertEqual(pause_close[0]["compiled"]["app_name"], "active media")
        status = intent_router.route("check vpn connection status")
        self.assertEqual(status.app_name, "Proton VPN")
        self.assertEqual(status.actions, ["status"])

    def test_contextual_scan_inherits_prior_app(self):
        tasks = intent_router.compile_request("open wiztree and scan G")
        self.assertEqual(tasks[1]["compiled"]["app_name"], "WizTree")
        self.assertEqual(tasks[1]["compiled"]["actions"], ["scan"])
        self.assertEqual(tasks[1]["compiled"]["arguments"]["drive"], "G:")

    def test_new_task_is_not_a_proton_answer(self):
        pending = {"options": ["Proton VPN", "Proton Mail"], "option_values": []}
        self.assertTrue(intent_router.is_new_request("Scan drive C with wiztree", pending))
        self.assertFalse(intent_router.is_new_request("Connect to Proton VPN", pending))

    def test_open_connect_clarification_keeps_connect(self):
        question = intent_router.route("Open proton and quick connect")
        self.assertEqual(question.as_response()["option_values"][0], "Connect to Proton VPN")


class DesktopRouterTests(unittest.TestCase):
    def test_malformed_compound_target_never_auto_opens_fuzzy_app(self):
        apps = {"remote desktop connection": {"display": "Remote Desktop Connection", "path": "mstsc"},
                "wiztree": {"display": "WizTree", "path": "wiztree"}}
        with patch.object(app_registry, "refresh_app_registry", return_value=apps):
            match, _candidates = app_registry.resolve_app("wiztree and establish vpn connection in proton")
        self.assertIsNone(match)

    def test_wiztree_scan_verifies_requested_drive_and_results(self):
        ready = {"ok": True, "entries": [
            {"id": "drive", "name": "Drive", "value": "G:", "control_type": "ComboBox", "enabled": True},
            {"id": "scan", "name": "Scan", "control_type": "Button", "enabled": True},
        ]}
        completed = {"ok": True, "entries": ready["entries"] + [
            {"id": "h1", "name": "File Name", "control_type": "Header"},
            {"id": "h2", "name": "Allocated", "control_type": "Header"},
        ]}
        with patch.object(desktop_router.elements, "list_elements", side_effect=[ready, completed]), \
             patch.object(desktop_router.elements, "act_element", return_value={"ok": True}) as act, \
             patch.object(desktop_router.time, "sleep"):
            result = desktop_router._scan_drive("WizTree", "G:")
        self.assertTrue(result["verified"])
        act.assert_called_once_with("scan", "click")

    def test_wiztree_selects_requested_drive_before_scan(self):
        initial = {"ok": True, "entries": [
            {"id": "drive", "name": "Drive", "value": "C:", "control_type": "ComboBox", "enabled": True},
            {"id": "scan", "name": "Scan", "control_type": "Button", "enabled": True},
        ]}
        selected = {"ok": True, "entries": [
            {"id": "drive2", "name": "Drive", "value": "G:", "control_type": "ComboBox", "enabled": True},
            {"id": "scan2", "name": "Scan", "control_type": "Button", "enabled": True},
            {"id": "h1", "name": "File Name", "control_type": "Header"},
            {"id": "h2", "name": "Allocated", "control_type": "Header"},
        ]}
        with patch.object(desktop_router.elements, "list_elements", side_effect=[initial, selected, selected]), \
             patch.object(desktop_router.elements, "act_element", return_value={"ok": True}) as act, \
             patch.object(desktop_router.time, "sleep"):
            result = desktop_router._scan_drive("WizTree", "G:")
        self.assertTrue(result["verified"])
        self.assertEqual(act.call_args_list[0].args, ("drive", "select_drive", "G:"))
        self.assertEqual(act.call_args_list[1].args, ("scan2", "click"))

    def test_wiztree_reenumerates_after_a_stale_drive_selector(self):
        def listing(combo_id, value="C:", extras=None):
            return {"ok": True, "entries": [
                {"id": combo_id, "name": "Drive", "value": value, "control_type": "ComboBox", "enabled": True},
                *(extras or []),
            ]}
        initial = listing("drive0")
        after_first = listing("drive1")
        after_second = listing("drive2")
        dropdown = listing("drive2", extras=[
            {"id": "g-item", "name": "[G:] System", "control_type": "ListItem", "enabled": True},
        ])
        selected = listing("drive3", "G:", extras=[
            {"id": "scan3", "name": "Scan", "control_type": "Button", "enabled": True},
        ])
        completed = listing("drive4", "G:", extras=[
            {"id": "scan4", "name": "Scan", "control_type": "Button", "enabled": True},
            {"id": "h1", "name": "File Name", "control_type": "Header"},
            {"id": "h2", "name": "Allocated", "control_type": "Header"},
        ])
        actions = [
            {"ok": False, "message": "stale selector"},
            {"ok": False, "message": "stale selector"},
            {"ok": True}, {"ok": True}, {"ok": True},
        ]
        with patch.object(desktop_router.elements, "list_elements",
                          side_effect=[initial, after_first, after_second, dropdown, selected, completed]), \
             patch.object(desktop_router.elements, "act_element", side_effect=actions) as act, \
             patch.object(desktop_router.time, "sleep"):
            result = desktop_router._scan_drive("WizTree", "G:")
        self.assertTrue(result["verified"])
        self.assertEqual([call.args[0] for call in act.call_args_list],
                         ["drive0", "drive1", "drive2", "g-item", "scan3"])

    def test_status_action_delegates_to_generic_actions(self):
        with patch.object(desktop_router.generic_actions, "status",
                          return_value={"ok": True, "verified": True, "message": "connected"}) as status, \
             patch.object(desktop_router, "_open_app",
                          return_value={"ok": True, "title": "Proton VPN", "message": "Opened 'Proton VPN'."}):
            result = desktop_router.execute(intent_router.DesktopIntent("Proton VPN", ["status"]))
        self.assertTrue(result["verified"])
        status.assert_called_once_with("Proton VPN", "connect")

    def test_disconnect_action_delegates_to_generic_actions(self):
        with patch.object(desktop_router.generic_actions, "run",
                          return_value={"ok": True, "verified": True, "message": "disconnected"}) as run, \
             patch.object(desktop_router, "_open_app",
                          return_value={"ok": True, "title": "Proton VPN", "message": "Opened 'Proton VPN'."}):
            result = desktop_router.execute(intent_router.DesktopIntent("Proton VPN", ["disconnect"]))
        self.assertTrue(result["verified"])
        run.assert_called_once_with("Proton VPN", "disconnect")

    def test_existing_focused_app_is_not_relaunched_or_refocused(self):
        with patch.object(desktop_router.di, "IS_WINDOWS", True), \
             patch.object(desktop_router.ctx, "window_is_open", return_value=(123, "TIDAL")), \
             patch.object(desktop_router.di, "get_active_window_title", return_value="TIDAL"), \
             patch.object(desktop_router.di, "focus_window") as focus, \
             patch.object(desktop_router.os, "startfile", create=True) as launch:
            result = desktop_router._open_app("TIDAL")
        self.assertTrue(result["ok"])
        focus.assert_not_called()
        launch.assert_not_called()

    def test_existing_background_app_is_focused_without_relaunch(self):
        with patch.object(desktop_router.di, "IS_WINDOWS", True), \
             patch.object(desktop_router.ctx, "window_is_open", return_value=(123, "TIDAL")), \
             patch.object(desktop_router.di, "get_active_window_title", return_value="Other app"), \
             patch.object(desktop_router.di, "focus_window", return_value=True) as focus, \
             patch.object(desktop_router.os, "startfile", create=True) as launch:
            result = desktop_router._open_app("TIDAL")
        self.assertTrue(result["ok"])
        focus.assert_called_once_with(123)
        launch.assert_not_called()

    def test_media_action_falls_back_to_generic_actions_when_no_session(self):
        """No Windows media session for TIDAL -> the deterministic on-screen
        fallback (generic_actions, not a TIDAL-specific handler) is tried
        after opening the app."""
        with patch.object(desktop_router.media_session, "control", return_value={
                 "ok": False, "verified": False, "message": "Found 0 matching Windows media sessions for TIDAL; no command was sent."}), \
             patch.object(desktop_router, "_open_app",
                          return_value={"ok": True, "title": "TIDAL", "message": "Opened 'TIDAL'."}), \
             patch.object(desktop_router.generic_actions, "run",
                          return_value={"ok": True, "verified": True, "message": "Activated 'Play'."}) as run, \
             patch.object(desktop_router.time, "sleep"):
            result = desktop_router.execute(intent_router.DesktopIntent("TIDAL", ["play"]))
        self.assertTrue(result["ok"])
        run.assert_called_once_with("TIDAL", "start")

    def test_media_action_skip_transport_has_no_generic_fallback(self):
        with patch.object(desktop_router.media_session, "control", return_value={
                 "ok": False, "verified": False, "message": "Found 0 matching Windows media sessions for TIDAL; no command was sent."}), \
             patch.object(desktop_router, "_open_app") as open_app, \
             patch.object(desktop_router.generic_actions, "run") as run:
            result = desktop_router.execute(intent_router.DesktopIntent("TIDAL", ["next"]))
        self.assertFalse(result["ok"])
        open_app.assert_not_called()
        run.assert_not_called()

    def test_connect_action_delegates_to_generic_actions(self):
        intent = intent_router.DesktopIntent("Proton VPN", ["connect"], "open proton vpn and connect")
        match = {"display": "Proton VPN", "path": r"C:\\Apps\\ProtonVPN.lnk", "kind": "shortcut"}
        with patch.object(desktop_router.di, "IS_WINDOWS", True), \
             patch.object(desktop_router.ctx, "window_is_open", return_value=None), \
             patch.object(desktop_router.app_registry, "resolve_app", return_value=(match, [])), \
             patch.object(desktop_router.os, "startfile", create=True), \
             patch.object(desktop_router, "_wait_for_window", return_value=(123, "Proton VPN")), \
             patch.object(desktop_router.di, "focus_window", return_value=True), \
             patch.object(desktop_router.generic_actions, "run",
                          return_value={"ok": True, "verified": True,
                                        "message": "Activated 'Quick Connect' and verified 'Proton VPN' is now connected."}) as run:
            result = desktop_router.execute(intent)
        self.assertTrue(result["ok"])
        self.assertTrue(result["verified"])
        run.assert_called_once_with("Proton VPN", "connect")

    def test_missing_app_never_substitutes_web(self):
        intent = intent_router.DesktopIntent("Proton VPN", [], "open proton vpn")
        with patch.object(desktop_router.di, "IS_WINDOWS", True), \
             patch.object(desktop_router.ctx, "window_is_open", return_value=None), \
             patch.object(desktop_router.app_registry, "resolve_app", return_value=(None, [])):
            result = desktop_router.execute(intent)
        self.assertFalse(result["ok"])
        self.assertIn("did not open a browser", result["message"])


class SystemMetricsTests(unittest.TestCase):
    def test_parses_nvidia_usage(self):
        parsed = system_metrics._parse_gpu_row("4096, 8192, 37, NVIDIA RTX 4070\n")
        self.assertEqual(parsed["vram_used_mb"], 4096)
        self.assertEqual(parsed["vram_total_mb"], 8192)
        self.assertEqual(parsed["vram_percent"], 50.0)
        self.assertEqual(parsed["gpu_percent"], 37)

    def test_counts_only_model_process_vram(self):
        raw = "llama-server.exe, 3072\nchrome.exe, 512\nllama_server.exe, 1024\n"
        self.assertEqual(system_metrics._parse_model_vram(raw), 4096.0)

    def test_missing_gpu_output_degrades_cleanly(self):
        self.assertEqual(system_metrics._parse_gpu_row(""), {})
        self.assertIsNone(system_metrics._parse_model_vram("chrome.exe, 100"))

    def test_direct_process_vram_is_split_from_system_vram(self):
        gpu = {
            "vram_used_mb": 6000.0, "vram_total_mb": 8192.0,
            "vram_percent": 73.2, "process_vram_mb": 2200.0,
        }
        result = system_metrics._add_vram_breakdown(gpu, "Llama")
        self.assertEqual(result["model_vram_mb"], 2200.0)
        self.assertEqual(result["system_vram_mb"], 3800.0)
        self.assertEqual(result["model_vram_source"], "process")

    def test_windows_fallback_uses_frozen_load_delta(self):
        tracked = {
            "baseline_used_mb": 3000.0, "baseline_total_mb": 8192.0,
            "estimated_mb": 2100.0, "name": "Llama",
        }
        gpu = {
            "vram_used_mb": 5400.0, "vram_total_mb": 8192.0,
            "vram_percent": 65.9, "process_vram_mb": None,
        }
        with patch.dict(system_metrics._MODEL, tracked):
            result = system_metrics._add_vram_breakdown(gpu, "Llama")
        self.assertEqual(result["model_vram_mb"], 2100.0)
        self.assertEqual(result["system_vram_mb"], 3300.0)
        self.assertEqual(result["model_vram_source"], "load_delta")

    def test_windows_gpu_counter_reads_llama_process_only(self):
        fake_psutil = SimpleNamespace(process_iter=lambda _attrs: [
            SimpleNamespace(info={"pid": 120, "name": "chrome.exe"}),
            SimpleNamespace(info={"pid": 456, "name": "llama-server.exe"}),
        ])
        seen_pids = []

        def read_counters(pids):
            seen_pids.append(pids)
            return {"dedicated_mb": 2048.5, "shared_mb": 128.0}

        with patch.object(system_metrics.os, "name", "nt"), \
             patch.object(system_metrics, "psutil", fake_psutil), \
             patch.object(system_metrics, "_COUNTERS", SimpleNamespace(read=read_counters)), \
             patch.dict(system_metrics._LAST_PROCESS), \
             patch.object(system_metrics.subprocess, "run", side_effect=AssertionError("GPU counters must not spawn PowerShell")):
            value = system_metrics._windows_model_vram()
            self.assertEqual(system_metrics._LAST_PROCESS["shared_mb"], 128.0)
        self.assertEqual(value, 2048.5)
        self.assertEqual(seen_pids, [{456}])

    def test_no_loaded_model_assigns_all_used_vram_to_system(self):
        gpu = {
            "vram_used_mb": 4200.0, "vram_total_mb": 8192.0,
            "vram_percent": 51.3, "process_vram_mb": None,
        }
        tracked = {
            "baseline_used_mb": None, "baseline_total_mb": None,
            "estimated_mb": None, "name": None,
        }
        with patch.dict(system_metrics._MODEL, tracked):
            result = system_metrics._add_vram_breakdown(gpu, None)
        self.assertEqual(result["model_vram_mb"], 0.0)
        self.assertEqual(result["system_vram_mb"], 4200.0)
        self.assertEqual(result["model_vram_source"], "none")

    def test_model_load_records_before_after_delta(self):
        tracked = {
            "baseline_used_mb": None, "baseline_total_mb": None,
            "estimated_mb": None, "name": None,
        }
        before = {"vram_used_mb": 3100.0, "vram_total_mb": 8192.0}
        after = {"vram_used_mb": 5300.0, "vram_total_mb": 8192.0}
        with patch.dict(system_metrics._MODEL, tracked), \
             patch.object(system_metrics, "_gpu_sample", side_effect=[before, after]):
            system_metrics.begin_model_load()
            system_metrics.finish_model_load("Llama")
            self.assertEqual(system_metrics._MODEL["estimated_mb"], 2200.0)
            self.assertEqual(system_metrics._MODEL["name"], "Llama")


class ClarificationPlacementTests(unittest.TestCase):
    class _Window:
        def __init__(self):
            self.value = ""

        def geometry(self, value):
            self.value = value

    def test_popup_opens_beside_cursor(self):
        win = self._Window()
        with patch.object(approval, "cursor_work_area", return_value=(0, 0, 1920, 1080)), \
             patch.object(approval, "cursor_position", return_value=(100, 100)):
            approval.place_near_cursor(win, object(), 430, 300)
        self.assertEqual(win.value, "430x300+118+118")

    def test_popup_flips_inside_monitor_edges(self):
        win = self._Window()
        with patch.object(approval, "cursor_work_area", return_value=(1920, 0, 3840, 1040)), \
             patch.object(approval, "cursor_position", return_value=(3800, 1020)):
            approval.place_near_cursor(win, object(), 430, 300)
        self.assertEqual(win.value, "430x300+3352+702")


if __name__ == "__main__":
    unittest.main(verbosity=2)
