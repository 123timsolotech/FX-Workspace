"""Exercise the actual UI controller's asynchronous lifecycle without a real microphone."""
import pathlib
import shutil
import subprocess
import unittest


class VoiceRegressionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for UI regressions')
    def test_context_and_effort_controls(self):
        root = pathlib.Path(__file__).parent
        result = subprocess.run([shutil.which('node'), '--test', str(root / 'fixtures/review/context_effort_ui.test.js')],
                                cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for UI regressions')
    def test_system_message_editor(self):
        root = pathlib.Path(__file__).parent
        result = subprocess.run([shutil.which('node'), '--test', str(root / 'fixtures/review/system_messages_ui.test.js')],
                                cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for UI regressions')
    def test_async_voice_controller_and_preview_policy(self):
        root = pathlib.Path(__file__).parent
        result = subprocess.run([shutil.which('node'), '--test', str(root / 'fixtures/review/voice_ui.test.js')],
                                cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for UI regressions')
    def test_interactive_replies(self):
        """D50: formulas compile without eval, charts and choices render, a widget is sealed off."""
        root = pathlib.Path(__file__).parent
        result = subprocess.run([shutil.which('node'), '--test', str(root / 'fixtures/review/interactive.test.js')],
                                cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is required for UI regressions')
    def test_globe_follows_the_assistant(self):
        """MossadGPT: world_monitor results and each spoken sentence move the globe; your own moves pause that."""
        root = pathlib.Path(__file__).parent
        result = subprocess.run([shutil.which('node'), '--test', str(root / 'fixtures/review/globe.test.js')],
                                cwd=root, capture_output=True, text=True, encoding='utf-8', timeout=40)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == '__main__':
    unittest.main()
