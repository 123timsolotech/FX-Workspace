"""Prompt variants and settings saves; stores are temporary and model calls are scripted."""
from __future__ import annotations

import copy
import hashlib
import pathlib
import tempfile
import unittest
from contextlib import ExitStack
from unittest import mock

import system_messages as sm
import agent.models as models
import agent.prompts as prompts
import agent.tasks as tasks
import core.config as config
import core.state as shared


class VariantTests(unittest.TestCase):
    def test_default_and_bad_stored_values(self):
        self.assertEqual(config.DEFAULT_SETTINGS['system_message_variant'], 'compact')
        self.assertEqual(config.clean_agent_settings({})['system_message_variant'], 'compact')
        for value in (None, [], 'missing'):
            self.assertEqual(sm.clean_settings({'system_message_variant': value})['system_message_variant'], 'compact')
        for value in ('', '   ', 'x' * (sm.MAX_CUSTOM_CHARS + 1)):
            cleaned = sm.clean_settings({'system_message_variant': 'custom', 'system_message_custom': value})
            self.assertEqual(cleaned['system_message_variant'], 'compact')
            self.assertEqual(cleaned['system_message_custom'], value)  # editable; never truncated silently

    def test_original_stays_identical_for_each_engine(self):
        self.assertEqual(sm.resolve({'system_message_variant': 'original'}, True), prompts.SYSTEM_PROMPT_COMPACT)
        self.assertEqual(sm.resolve({'system_message_variant': 'original'}, False), prompts.SYSTEM_PROMPT)

    def test_known_placeholders_only_and_unicode(self):
        settings = {'system_message_variant': 'custom', 'assistant_name': '  נועה\n助手  ',
                    'system_message_custom': '{NAME}: {"answer": 42} {other} {PERSONA_BLOCK} {ROLE_BLOCK}'}
        self.assertEqual(sm.resolve(settings, True), 'נועה 助手: {"answer": 42} {other}  ')
        self.assertEqual(sm.clean_name('  '), 'Assistant')
        self.assertEqual(sm.clean_name('a' * 60), 'a' * 40)

    def test_legacy_fallback_argument_cannot_replace_the_selected_message(self):
        for variant in ('compact', 'detailed', 'custom'):
            settings = {'system_message_variant': variant, 'system_message_custom': 'My custom base.'}
            self.assertEqual(sm.resolve(settings, True, prompts.SYSTEM_PROMPT_COMPACT), sm.resolve(settings, True))
        self.assertEqual(sm.resolve({'system_message_variant': 'original'}, True, 'Explicit base'), 'Explicit base')

    def test_catalog_and_presets_have_corrected_operating_rules(self):
        catalog = sm.catalog()
        self.assertEqual([p['id'] for p in catalog['presets']], list(sm.VARIANTS))
        for preset in catalog['presets'][:2]:
            for text in (preset['local_text'], preset['cloud_text']):
                self.assertIn('Claims about computer changes', text)
                self.assertIn('Retry a transient failure only when justified', text)
                self.assertIn('append=true', text)
                self.assertIn('fetch_page', text)
                self.assertIn('Answer side questions briefly and continue', text)
                self.assertNotIn('roblox\\_\\*', text)


class StateTests(unittest.TestCase):
    def setUp(self):
        previous = dict(shared.app_state)
        self.addCleanup(lambda: (shared.app_state.clear(), shared.app_state.update(previous)))
        shared.app_state.update(settings=copy.deepcopy(config.DEFAULT_SETTINGS), turn_persona='', turn_tools=set(),
                                raw_mode=False, design_mode=False, context_summary='', task_context='', turn_voice=False,
                                turn_instructions='', turn_command_note='', turn_attachments=None,
                                conversation=[{'role': 'system', 'content': 'placeholder'}, {'role': 'user', 'content': 'hello'}],
                                mode=config.MODE_LOCAL, current_user_request='hello', turn_policy={'mode': 'none'})
        shared.app_state.pop('turn_system_message', None)
        self.stack = self.enterContext(ExitStack())
        self.stack.enter_context(mock.patch('tools.schemas.tools_for_current_turn', return_value=[]))
        self.stack.enter_context(mock.patch('skills.index', return_value=''))
        self.stack.enter_context(mock.patch('core.observer._activity_line', return_value=''))
        self.stack.enter_context(mock.patch('context.render_live_block', return_value='LIVE'))

    def test_both_engines_use_the_selected_base(self):
        for variant in ('compact', 'detailed', 'custom'):
            shared.app_state['settings'].update(system_message_variant=variant, system_message_custom='Custom base {NAME}',
                                               assistant_name='Robin')
            for mode in (config.MODE_LOCAL, config.MODE_CLOUD):
                shared.app_state['mode'] = mode
                actual = models.build_messages()[0]['content']
                import tools.schemas as schemas
                expected = prompts.for_tools(sm.resolve(shared.app_state['settings'], mode == config.MODE_LOCAL),
                                             [], schemas.TOOL_NAMES)
                self.assertTrue(actual.startswith(expected))
                self.assertNotIn('{NAME}', actual)

    def test_persona_is_added_once_and_after_the_base(self):
        shared.app_state.update(turn_persona='Speak like a friendly librarian.')
        shared.app_state['settings'].update(system_message_variant='custom', system_message_custom='Base {PERSONA_BLOCK}')
        for local in (False, True):
            text = models.system_prompt(local)
            self.assertEqual(text.count('Speak like a friendly librarian.'), 1)
            self.assertTrue(text.startswith('Base '))
            self.assertIn('never overrides safety, honesty', text)

    def test_custom_tool_restrictions_are_not_removed_by_builtin_filtering(self):
        text = 'Never call delete_path. Never call execute_command. Answer as {NAME}.'
        shared.app_state['settings'].update(system_message_variant='custom', system_message_custom=text)
        self.assertEqual(models.system_prompt(True), text.replace('{NAME}', 'Assistant'))

    def test_voice_uses_the_same_base_and_retains_its_language_header(self):
        shared.app_state['settings'].update(system_message_variant='custom', system_message_custom='Voice and text base.')
        for lang in ('en', 'he', 'ru'):
            shared.app_state.update(turn_voice=True, turn_voice_lang=lang)
            with mock.patch('voice.voice_header', return_value=f'VOICE {lang}') as header:
                actual = models.build_messages()
            self.assertEqual(actual[0]['content'], 'Voice and text base.')
            self.assertIn(f'VOICE {lang}', actual[-1]['content'])
            header.assert_called_once_with(shared.app_state['settings'], lang)

    def test_raw_and_design_keep_their_own_behavior(self):
        shared.app_state['settings'].update(system_message_variant='custom', system_message_custom='Custom base.')
        shared.app_state['raw_mode'] = True
        self.assertEqual(models.system_prompt(True), '')
        self.assertEqual(models.build_messages(), [{'role': 'user', 'content': 'hello'}])
        shared.app_state.update(raw_mode=False, design_mode=True)
        self.assertEqual(models.system_prompt(True), prompts.DESIGN_PROMPT)

    def test_context_accounting_measures_the_effective_message(self):
        import agent.context_window as context_window
        shared.app_state['settings'].update(system_message_variant='custom', system_message_custom='Base')
        with mock.patch('llama_local.model_context_limit', return_value=16384), \
             mock.patch.object(models, '_local_reply_tokens', return_value=2048), \
             mock.patch.object(context_window, '_tools_tokens', return_value=0):
            short = context_window.status('scripted-model', True)
            shared.app_state['settings']['system_message_custom'] = 'X' * 4000
            long = context_window.status('scripted-model', True)
        self.assertGreater(long['fixed'] - short['fixed'], 900)
        self.assertEqual(long['used'] - short['used'], long['fixed'] - short['fixed'])
        self.assertLessEqual(long['history_limit'], short['history_limit'])
        shared.app_state['context_summary'] = 'An earlier decision.'
        self.assertTrue(models.system_prompt(True).startswith('X' * 4000))
        self.assertIn('An earlier decision.', models.system_prompt(True))

    def _scripted_turn(self, execute):
        for target in ('_select_request_backend', '_keep_attachments', '_save_run'):
            self.stack.enter_context(mock.patch.object(tasks, target, return_value=None))
        self.stack.enter_context(mock.patch('agent.sessions.mark_pending'))
        self.stack.enter_context(mock.patch('agent.sessions.record_turn', return_value=None))
        self.stack.enter_context(mock.patch('agent.sessions.public_chat', return_value=None))
        self.stack.enter_context(mock.patch('agent.routine_runner.offer_candidate', return_value=None))
        self.stack.enter_context(mock.patch('image_gen.wants_image', return_value=False))
        self.stack.enter_context(mock.patch('auto_tools.pick', return_value=[]))
        for target in ('_after_answer', '_with_care'):
            self.stack.enter_context(mock.patch.object(tasks, target, side_effect=lambda req, result: result))
        for target in ('_with_timelines', '_with_images', '_with_charts'):
            self.stack.enter_context(mock.patch.object(tasks, target, side_effect=lambda result: result))
        self.stack.enter_context(mock.patch.object(tasks.agent, 'run', side_effect=execute))
        return tasks._run_turn_in_chat(shared.ChatRequest(message='hello', voice=True, voice_lang='en'))

    def test_saving_during_a_turn_applies_only_to_the_next_turn(self):
        seen = []
        def execute(*args, **kwargs):
            seen.append(models.system_prompt(True))
            shared.app_state['settings'].update(system_message_variant='custom', system_message_custom='Next turn only.')
            seen.append(models.system_prompt(True))
            return {'status': 'stopped', 'message': 'Scripted turn.'}
        self._scripted_turn(execute)
        self.assertEqual(seen[0], seen[1])
        self.assertNotIn('turn_system_message', shared.app_state)
        self.assertEqual(models.system_prompt(True), 'Next turn only.')

    def test_a_failed_turn_clears_the_prompt_snapshot(self):
        def fail(*args, **kwargs):
            raise RuntimeError('Scripted failure')
        with self.assertRaisesRegex(RuntimeError, 'Scripted failure'):
            self._scripted_turn(fail)
        self.assertNotIn('turn_system_message', shared.app_state)


class ApiTests(unittest.TestCase):
    def setUp(self):
        StateTests.setUp(self)
        from fastapi.testclient import TestClient
        import api.server as server
        self.tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.path = pathlib.Path(self.tmp) / 'settings.json'
        self.stack.enter_context(mock.patch.object(config, 'SETTINGS_PATH', str(self.path)))
        self.stack.enter_context(mock.patch.object(server.llm, '_current_model', return_value='scripted-model'))
        self.stack.enter_context(mock.patch.object(server.event_log, 'log_action'))
        self.assertTrue(config.save_settings(shared.app_state['settings']))
        # No context manager: do not start app background services against user stores.
        self.client = TestClient(server.app, base_url='http://127.0.0.1:8000')

    def test_catalog_and_custom_save_reload(self):
        response = self.client.get('/api/system-messages')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['default'], 'compact')
        page = self.client.get('/')
        self.assertEqual(page.status_code, 200)
        self.assertIn('<script src="/system-messages.js"></script>', page.text)
        script = self.client.get('/system-messages.js')
        self.assertEqual(script.status_code, 200)
        self.assertIn('const SystemMessageSettings', script.text)
        response = self.client.post('/api/settings', json={'system_message_variant': 'custom',
                                    'system_message_custom': 'Hello from {NAME}.', 'assistant_name': 'Robin'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(config.load_settings()['system_message_custom'], 'Hello from {NAME}.')
        self.assertEqual(models.system_prompt(True), 'Hello from Robin.')

    def test_bad_saves_change_neither_disk_nor_state(self):
        before = self.path.read_bytes()
        for fields in ({'system_message_variant': 'unknown'}, {'system_message_variant': None},
                       {'system_message_variant': 'custom', 'system_message_custom': '   '},
                       {'system_message_custom': 'x' * (sm.MAX_CUSTOM_CHARS + 1)},
                       {'system_message_custom': None}, {'assistant_name': 'x' * 41}):
            with self.subTest(fields=list(fields)):
                response = self.client.post('/api/settings', json=fields)
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(shared.app_state['settings']['system_message_variant'], 'compact')

    def test_failed_atomic_save_preserves_previous_selection_and_file(self):
        before = self.path.read_bytes()
        previous = shared.app_state['settings']
        with mock.patch('atomic_io.replace', side_effect=OSError('Scripted disk failure')):
            response = self.client.post('/api/settings', json={'system_message_variant': 'detailed'})
        self.assertEqual(response.status_code, 500, response.text)
        self.assertIs(shared.app_state['settings'], previous)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(list(pathlib.Path(self.tmp).glob('.settings-*.tmp')), [])

    def test_context_and_effort_disk_failures_leave_previous_choices_active(self):
        for fields in ({'context_auto_compress': '80'}, {'effort': 'max'}, {'prompt_prep': 'plan'}):
            with self.subTest(fields=fields):
                before = self.path.read_bytes()
                previous = shared.app_state['settings']
                with mock.patch('atomic_io.replace', side_effect=OSError('Scripted disk failure')):
                    response = self.client.post('/api/settings', json=fields)
                self.assertEqual(response.status_code, 500, response.text)
                self.assertIs(shared.app_state['settings'], previous)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(list(pathlib.Path(self.tmp).glob('.settings-*.tmp')), [])

    def test_context_and_model_specific_effort_choices_persist_after_confirmation(self):
        for choice in ('off', '80', '90', '95', '97'):
            response = self.client.post('/api/settings', json={'context_auto_compress': choice})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(config.load_settings()['context_auto_compress'], choice)
        with mock.patch.object(tasks, '_queue_effort_change'):
            response = self.client.post('/api/settings', json={'effort': 'max', 'prompt_prep': 'plan',
                                      'for_mode': 'local', 'for_model': 'next-model'})
        self.assertEqual(response.status_code, 200, response.text)
        saved = config.load_settings()
        self.assertEqual(saved['effort_by_model']['next-model'], 'max')
        self.assertEqual(saved['prompt_prep_by_model']['next-model'], 'plan')

    def test_invalid_compression_thresholds_do_not_change_state_or_disk(self):
        before = self.path.read_bytes()
        previous = shared.app_state['settings']
        for value in (None, '0', '100', 'unknown'):
            response = self.client.post('/api/settings', json={'context_auto_compress': value})
            self.assertEqual(response.status_code, 422, response.text)
            self.assertIs(shared.app_state['settings'], previous)
            self.assertEqual(self.path.read_bytes(), before)

    def test_switching_presets_preserves_the_custom_draft(self):
        fields = {'system_message_variant': 'original', 'system_message_custom': 'Saved draft.'}
        self.assertEqual(self.client.post('/api/settings', json=fields).status_code, 200)
        self.assertEqual(self.client.post('/api/settings', json={'system_message_variant': 'compact'}).status_code, 200)
        self.assertEqual(config.load_settings()['system_message_custom'], 'Saved draft.')

    def test_old_settings_load_the_new_default_without_losing_existing_choices(self):
        self.path.write_text('{"effort": "extra", "response_mode": "raw", "user_name": "Alex"}', encoding='utf-8')
        loaded = config.load_settings()
        self.assertEqual(loaded['system_message_variant'], 'compact')
        self.assertEqual((loaded['effort'], loaded['response_mode'], loaded['user_name']), ('extra', 'raw', 'Alex'))


class PageTests(unittest.TestCase):
    def test_entrypoint_and_html_copies(self):
        root = pathlib.Path(__file__).parent
        html = (root / 'index.html').read_bytes()
        self.assertEqual(hashlib.sha256(html).digest(), hashlib.sha256((root / 'static/index.html').read_bytes()).digest())
        self.assertNotIn(b'\n', html.replace(b'\r\n', b''))
        self.assertFalse(html.startswith(b'\xef\xbb\xbf'))
        self.assertIn(b'<script src="/system-messages.js"></script>', html)
        self.assertIn(b"['systemmessage', 'System message']", html)
        self.assertIn(b'systemmessage: renderSystemMessage', html)


if __name__ == '__main__':
    unittest.main()
