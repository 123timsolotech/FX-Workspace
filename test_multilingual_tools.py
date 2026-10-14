import unittest
from unittest.mock import patch

import intent_router
import model_caps
from test_model_caps import _Isolated, PROPS_NATIVE


class LanguageAuthorityTests(unittest.TestCase):
    def test_explicit_commands_receive_tools(self):
        for text in ('Переименуй модель в Roblox Studio', 'Скопируй интерфейс на десять моделей',
                     'Можешь переименовать модель?', 'שנה את שם המודל', 'תעתיק את הממשק לעשרה מודלים',
                     'אתה יכול לשכפל את המודל?'):
            self.assertEqual(intent_router.classify_turn(text)['mode'], 'action', text)

    def test_questions_and_negations_never_authorize_mutation(self):
        for text in ('Как удалить модель?', 'Не удаляй модель', 'איך למחוק את המודל?',
                     'אל תמחק את המודל', 'האם כדאי לשנות את השם?', 'Привет', 'שלום'):
            self.assertFalse(intent_router.classify_turn(text)['allow_mutation'], text)

    def test_read_requests_remain_read_only(self):
        for text in ('Покажи список моделей', 'בדוק את הסקריפט'):
            self.assertEqual(intent_router.classify_turn(text)['mode'], 'read')


class TemplateRefreshTests(_Isolated):
    def test_new_template_clears_previous_refusal_on_same_server(self):
        model_caps.detect('gemma', 'http://x', lambda *_: PROPS_NATIVE, loaded=True)
        model_caps.update('gemma', tool_protocol='text')
        updated = dict(PROPS_NATIVE, chat_template='corrected tool template')
        record = model_caps.detect('gemma', 'http://x', lambda *_: updated, loaded=True)
        self.assertEqual(record['tool_protocol'], 'native')


class MCPVisibilityTests(unittest.TestCase):
    def test_ninth_and_later_tools_are_not_silently_hidden(self):
        import main
        from tools import schemas
        tools = [{'type': 'function', 'function': {'name': f'mcp__studio__tool_{i}',
                 'description': 'Description ' * 70,
                 'parameters': {'type': 'object', 'properties': {'code': {'type': 'string'}}}}}
                 for i in range(15)]
        import core.state as shared
        import skills
        # Studio's MCP tools come with the roblox skill (D50): a Studio request, in a fresh chat.
        with patch.object(schemas, '_local_context_window', return_value=16384), \
             patch.object(schemas, 'mcp_schemas', return_value=tools), \
             patch.dict(shared.app_state, {'current_user_request': 'stack parts in Roblox Studio', 'chat_skills': [],
                                           'turn_skills': None, 'chat_skill_seen': {}, 'chat_turn_no': 0}):
            offered = schemas.tools_for_mode(True, 'action')
        selected = [t for t in offered if t['function']['name'].startswith('mcp__studio__')]
        self.assertEqual(len(selected), 15)
        self.assertEqual(selected[-1]['function']['parameters'], tools[-1]['function']['parameters'])
        self.assertGreater(len(tools[-1]['function']['description']), 320)  # no global schema mutation


class ThreeLanguageTests(unittest.TestCase):
    def test_detected_language_is_preserved_for_reply(self):
        import voice
        settings = {'voice_lang': 'auto', 'voice_auto_langs': ['ru', 'he']}
        for lang, words in [('en', 'Hello there'), ('ru', 'Добрый вечер'), ('he', 'שלום לך')]:
            with patch.object(voice._whisper, 'ensure'), patch.object(voice, '_whisper_once',
                  return_value={'text': words, 'language': lang}):
                result = voice.transcribe(b'x' * 5000, settings)
            self.assertEqual((result['text'], result['lang']), (words, lang))
            self.assertEqual(voice.reply_lang(voice.clean_settings(settings), result['lang']), lang)

