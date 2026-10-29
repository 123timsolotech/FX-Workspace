"""Voice (voice.py): settings degrade safely, setup downloads only what's missing and never
unpacks outside its folder, silence isn't sent as words, and a voice turn asks the model for
a spoken-style answer. The engines themselves are not run here."""
import io
import os
import re
from json import dumps as json_dumps
import tempfile
import unittest
import zipfile
from unittest.mock import patch

import main  # noqa: F401  (wires the app together)
import core.config as config
import core.state as shared
import voice

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SettingsTests(unittest.TestCase):
    def test_defaults_and_cleaning(self):
        for k, v in voice.DEFAULT_SETTINGS.items():
            self.assertEqual(config.DEFAULT_SETTINGS[k], v, k)
        out = voice.clean_settings({"voice_model": "huge", "voice_voice": "../../x", "voice_lang": "english; rm",
                                    "voice_rate": 9, "voice_conversation": "yes", "voice_breath": -1, "voice_volume": "loud",
                                    "voice_patience": "forever", "voice_sensitivity": 5, "voice_auto_langs": ["he", "xx", "en", "es", "he"],
                                    "voice_multi_model": "large"})
        self.assertEqual((out["voice_model"], out["voice_voice"], out["voice_lang"], out["voice_rate"], out["voice_conversation"]),
                         ("base.en", "en_US-lessac-medium", "en", 1.6, True))
        self.assertEqual((out["voice_breath"], out["voice_volume"], out["voice_patience"], out["voice_sensitivity"], out["voice_multi_model"]),
                         (0.0, 1.0, "normal", "normal", "small"))
        self.assertEqual(out["voice_auto_langs"], ["he", "es"])            # known, not English, once each
        for lang in ("he", "es", "auto"):
            self.assertEqual(voice.clean_settings({"voice_lang": lang})["voice_lang"], lang)
        self.assertNotIn("ja", voice.LANGS)                                 # no voice that runs here
        self.assertNotIn("ko", voice.LANGS)


class TranscriptReviewTests(unittest.TestCase):
    def test_clean_turns_do_not_wait_and_critical_words_do(self):
        self.assertFalse(voice.transcript_review("Tell me a story", "en")["required"])
        for text, lang in (("Track FL123 near Haifa", "en"), ("שלח 12 הודעות", "he"),
                           ("Отправь 12 сообщений", "ru")):
            self.assertTrue(voice.transcript_review(text, lang)["required"], (text, lang))
        self.assertTrue(voice.transcript_review("Where is it", "en", [{"avg_logprob": -1.1}])["required"])


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        for name, sub in (("VOICE_DIR", ""), ("WHISPER_DIR", "whisper"), ("PIPER_DIR", "piper")):
            p = patch.object(voice, name, os.path.join(self.dir.name, sub))
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(voice.vn, "SHERPA_DIR", os.path.join(self.dir.name, "sherpa"))
        p.start()
        self.addCleanup(p.stop)
        p = patch.object(voice.vh, "DIRECTORY", __import__('pathlib').Path(self.dir.name) / "hebrew")
        p.start()
        self.addCleanup(p.stop)

    def test_the_natural_setup_by_default(self):
        items = voice.plan({})
        self.assertEqual([(i[0], i[5]) for i in items], [("kokoro", "tar"), ("parakeet", "tar"), ("live", "tar")])
        self.assertEqual(round(sum(i[4] for i in items)), 916)
        # Compact Kokoro, no live captions, Whisper instead of Parakeet: only what's chosen.
        keys = [i[0] for i in voice.plan({"voice_natural_compact": True, "voice_live": False, "voice_listener": "whisper"})]
        self.assertEqual(keys, ["kokoro-compact", "whisper", "model"])

    def test_plan_lists_only_what_is_missing(self):
        fast = {"voice_engine": "fast", "voice_listener": "whisper", "voice_live": False}
        keys = [i[0] for i in voice.plan(fast)]
        self.assertEqual(keys, ["whisper", "model", "piper", "voice", "voice-config"])
        self.assertEqual(round(sum(i[4] for i in voice.plan(fast))), 252)          # the ~250 MB the page shows
        os.makedirs(os.path.join(voice.WHISPER_DIR, "bin"))
        open(os.path.join(voice.WHISPER_DIR, "bin", "whisper-server.exe"), "wb").close()
        os.makedirs(os.path.join(voice.WHISPER_DIR, "models"))
        with open(os.path.join(voice.WHISPER_DIR, "models", "ggml-base.en.bin"), "wb") as f:
            f.write(b"\0" * 2_000_000)
        self.assertEqual([i[0] for i in voice.plan(fast)], ["piper", "voice", "voice-config"])
        self.assertTrue(voice.status(fast)["models"]["base.en"])
        self.assertIn("small.en", [i[3].split("ggml-")[-1][:-4] for i in voice.plan({**fast, "voice_model": "small.en"}) if i[0] == "model"])

    def test_every_download_is_https_from_known_hosts(self):
        for item in voice.plan({"voice_gpu": True}) + voice.plan({"voice_engine": "fast", "voice_listener": "whisper"}) + \
                voice.plan({"voice_lang": "auto", "voice_auto_langs": list(voice.LANGS)}):
            self.assertRegex(item[2], r"^https://(github\.com|huggingface\.co)/")

    def test_another_language_adds_the_multilingual_model_and_its_voice(self):
        with patch.object(voice.vn, "available", lambda: True), patch.object(voice.vn, "installed", lambda p: True):
            keys = [i[0] for i in voice.plan({"voice_lang": "es"})]
            self.assertEqual(keys, ["whisper", "model-multi", "piper", "voice-es", "voice-es-config"])
            self.assertIn("ggml-small.bin", [i[2] for i in voice.plan({"voice_lang": "es"}) if i[0] == "model-multi"][0])
            self.assertIn("ggml-base.bin", [i[2] for i in voice.plan({"voice_lang": "es", "voice_multi_model": "base"}) if i[0] == "model-multi"][0])
            # Hebrew's voice needs an add-on: it's heard (the model), not downloaded as a voice piper.exe can't load.
            self.assertEqual([i[0] for i in voice.plan({"voice_lang": "he"})],
                             ["whisper", "model-multi", "hebrew-0", "hebrew-1", "hebrew-2", "hebrew-3"])
            keys = [i[0] for i in voice.plan({"voice_lang": "auto", "voice_auto_langs": ["he", "fr"]})]
            self.assertEqual(keys, ["whisper", "model-multi", "hebrew-0", "hebrew-1", "hebrew-2", "hebrew-3", "piper", "voice-fr", "voice-fr-config"])
            self.assertEqual([i[0] for i in voice.plan({})], [])             # English only: nothing more

    def test_languages_status(self):
        st = voice.status({"voice_lang": "he"})
        self.assertFalse(st["hearing"])                                     # the multilingual model isn't here
        self.assertIn("he", st["langs"])
        self.assertFalse(st["catalog"]["langs"]["he"]["addon"])
        self.assertFalse(st["catalog"]["langs"]["es"]["addon"])

    def test_a_zip_cannot_write_outside_its_folder(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("../../evil.exe", b"x")
        data = buf.getvalue()

        def fake_download(url, dest, on_bytes):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "wb") as f:
                f.write(data)
        with patch.object(voice, "_download", fake_download):
            voice._setup.update(state="running", done_mb=0, total_mb=1)
            voice._run_setup([("piper", "Speech", "https://github.com/x.zip", voice.PIPER_DIR, 1, True)])
        self.assertEqual(voice._setup["state"], "error")
        self.assertIn("Unsafe path", voice._setup["error"])
        self.assertFalse(os.path.exists(os.path.join(self.dir.name, "evil.exe")))
        voice._setup.update(state="idle", error="")


class HearingTests(unittest.TestCase):
    def _transcribe(self, text):
        class Resp:
            status_code = 200

            def json(self):
                return {"text": text}

        class Client:
            def __init__(self, *a, **k): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def post(self, *a, **k): return Resp()
        with patch.object(voice._whisper, "ensure", lambda model, gpu=False: None), patch.object(voice.netlayer, "local_client", Client), \
                patch.object(voice, "listener_in_use", lambda s: "whisper"), patch.object(voice, "_whisper_http", None):
            return voice.transcribe(b"R" * 5000, {"voice_listener": "whisper"})["text"]

    def test_silence_is_not_words(self):
        for noise in ("[BLANK_AUDIO]", " (music) ", "[silence]", ""):
            self.assertEqual(self._transcribe(noise), "", noise)
        self.assertEqual(self._transcribe(" What's the\n weather? "), "What's the weather?")

    def _hear(self, settings, answers):
        """transcribe() with whisper's answers scripted: [(language asked for, reply)]."""
        asked, models = [], []

        def once(wav, language):
            asked.append(language)
            return answers[len(asked) - 1]
        with patch.object(voice._whisper, "ensure", lambda model, gpu=False: models.append(model)), patch.object(voice, "_whisper_once", once):
            got = voice.transcribe(b"R" * 5000, settings)
        return got, asked, models

    def test_another_language_is_heard_by_the_multilingual_model(self):
        got, asked, models = self._hear({"voice_lang": "es"}, [{"text": " Hola, ¿qué tal? ", "language": "spanish"}])
        self.assertEqual((got["text"], got["lang"], asked, models), ("Hola, ¿qué tal?", "es", ["es"], ["small"]))

    def test_detecting_uses_the_language_it_decoded_in(self):
        # This whisper build's detected_language/probabilities are flat noise; "language" is what it used.
        s = {"voice_lang": "auto", "voice_auto_langs": ["he"]}
        got, asked, _ = self._hear(s, [{"text": "Hi there", "language": "english", "detected_language": "croatian",
                                        "language_probabilities": {"hr": 0.0101, "en": 0.01, "he": 0.0099}}])
        self.assertEqual((got["lang"], asked), ("en", ["auto"]))
        got, _, _ = self._hear(s, [{"text": "שלום", "language": "hebrew"}])
        self.assertEqual(got["lang"], "he")
        # Heard as a language that wasn't chosen: with one chosen besides English, it's that one - asked again.
        got, asked, _ = self._hear(s, [{"text": "salam", "language": "arabic"}, {"text": "שלום", "language": "hebrew"}])
        self.assertEqual((got["text"], got["lang"], asked), ("שלום", "he", ["auto", "he"]))
        got, asked, _ = self._hear({"voice_lang": "auto", "voice_auto_langs": ["he", "es"]},
                                   [{"text": "x", "language": "welsh"}, {"text": "Hello", "language": "english"}])
        self.assertEqual((got["lang"], asked), ("en", ["auto", "en"]))

    def test_the_reply_is_spoken_in_the_language_heard(self):
        got = {}

        def fake(engine, text, name, rate):
            got.update(engine=engine, text=text, name=name)
            return b"RIFF"
        s = {"voice_lang": "auto", "voice_auto_langs": ["es"]}
        with patch.object(voice, "_piper_wav", fake), patch.object(voice, "lang_voice_ready", lambda c: True):
            voice.speak("¡Hola! Sube un **5%** 🚀", s, lang="es")
        self.assertEqual((got["engine"], got["name"], got["text"]), (voice._piper_lang, "es_ES-davefx-medium", "¡Hola! Sube un 5%"))
        self.assertEqual(voice.reply_lang(voice.clean_settings(s), "fr"), "en")          # not chosen: English
        self.assertEqual(voice.reply_lang(voice.clean_settings({"voice_lang": "de"}), ""), "de")
        with patch.object(voice.vh, "speak", return_value=([0.0] * 100, 22050)) as hebrew:
            self.assertTrue(voice.speak("שלום", {"voice_lang": "he"}).startswith(b"RIFF"))
            hebrew.assert_called_once_with("שלום", 1.0)
        self.assertIn("Answer in Spanish", voice.voice_header(s, "es"))
        self.assertIn("Answer in English", voice.voice_header(s, "en"))

    def test_tiny_recordings_are_skipped(self):
        self.assertEqual(voice.transcribe(b"x" * 10, {})["text"], "")

    def test_nothing_speaks_when_not_set_up(self):
        with patch.object(voice, "piper_exe", lambda: None):
            with self.assertRaises(RuntimeError):
                voice._piper.speak("hello", "en_US-lessac-medium", 1.0)


class NaturalSpeechTests(unittest.TestCase):
    def test_text_is_said_the_way_a_person_would(self):
        import speech_text as st
        cases = {
            "Nvidia made $215.94B in FY2026, up +65.5% YoY.": "Nvidia made 215.94 billion dollars in fiscal 2026, up 65.5 percent year over year.",
            "It fell -8.9% on 2026-03-30.": "It fell 8.9 percent on March 30, 2026.",
            "Mistral-7B runs at 63.5 tok/s with 8GB, e.g. vs. Qwen.": "Mistral-7B runs at 63.5 tokens a second with 8 gigabytes, for example, versus Qwen.",
            "P/E is 45.9x; cost 1,200,000 or €3.5M (2022-2026).": "P E is 45.9 times; cost 1200000 or 3.5 million euros (2022 to 2026).",
            "Q3FY24 🚀 results -> great": "Q3 fiscal 24 results to great",
        }
        for src, want in cases.items():
            self.assertEqual(st.normalize(src), want, src)

    def test_audio_is_trimmed_faded_and_levelled(self):
        import math
        import voice_natural as vn
        sr = 24000
        tone = [0.0] * sr + [0.3 * math.sin(i / 5) for i in range(sr)] + [0.0] * sr      # 1 s silence, 1 s tone, 1 s silence
        pcm = vn.polish(tone, sr)
        self.assertLess(len(pcm), 1.2 * sr)                     # the silences are trimmed (a short margin kept)
        self.assertEqual((pcm[0], pcm[-1]), (0, 0))             # faded in and out: no click at the joins
        self.assertGreater(max(abs(v) for v in pcm), 0.8 * 32767)   # levelled to about -1.5 dBFS

    def test_the_other_engine_fills_in(self):
        with patch.object(voice, "natural_ready", lambda s: False), patch.object(voice, "piper_exe", lambda: "piper.exe"), \
                patch.object(voice, "piper_voice", lambda v: "v.onnx"):
            self.assertEqual(voice.speaker_in_use(voice.clean_settings({})), "fast")
        with patch.object(voice, "natural_ready", lambda s: True):
            self.assertEqual(voice.speaker_in_use(voice.clean_settings({"voice_engine": "fast"})),
                             "fast" if voice.piper_exe() and voice.piper_voice("en_US-lessac-medium") else "natural")

    def test_speak_normalises_before_the_voice(self):
        got = {}

        def fake(text, voice_name, rate, pack):
            got.update(text=text, voice=voice_name, pack=pack)
            import array
            return array.array("h", [0] * 10), 24000
        with patch.object(voice, "speaker_in_use", lambda s: "natural"), patch.object(voice.vn.kokoro, "speak", fake):
            voice.speak("Up +5% in FY2026.", {"voice_natural": "am_michael"})
        self.assertEqual(got, {"text": "Up 5 percent in fiscal 2026.", "voice": "am_michael", "pack": "kokoro"})

    def test_transcripts_are_tidied(self):
        import voice_natural as vn
        self.assertEqual(vn.tidy("NVIDIA made$215 billion  now"), "NVIDIA made $215 billion now")

    def test_voices_map_to_kokoro_speakers(self):
        import voice_natural as vn
        self.assertEqual({k: v[0] for k, v in vn.VOICES.items() if k in ("af_heart", "am_fenrir", "am_michael", "am_puck", "bf_emma")},
                         {"af_heart": 3, "am_fenrir": 14, "am_michael": 16, "am_puck": 18, "bf_emma": 21})


class TurnTests(unittest.TestCase):
    def test_a_voice_turn_asks_for_a_spoken_answer(self):
        import inspect
        import agent.models as models
        import agent.tasks as tasks
        src = inspect.getsource(models.build_messages)
        self.assertIn('shared.app_state.get("turn_voice")', src)
        self.assertIn("voice.voice_header(", src)
        self.assertIn("No tables, code blocks", voice.voice_header({}))
        self.assertIn('"turn_voice"', inspect.getsource(tasks._run_turn_in_chat))          # this turn's only
        self.assertIn('"turn_voice_lang"', inspect.getsource(tasks._run_turn_in_chat))
        self.assertIn('shared.app_state.get("turn_voice_lang")', src)                       # the reply's language
        self.assertFalse(shared.ChatRequest(message="hi").voice)
        self.assertTrue(shared.ChatRequest(message="hi", voice=True).voice)
        self.assertEqual(shared.ChatRequest(message="hi", voice=True, voice_lang="he").voice_lang, "he")


class PageTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(HERE, "index.html"), encoding="utf-8") as f:
            self.html = f.read()
        self.js = self.html[self.html.index("const Voice = (() => {"):self.html.index("// Settings > Appearance: show or hide one panel")]

    def test_entry_points_and_theme_variants(self):
        self.assertIn('id="voice-home"', self.html)
        self.assertIn('id="voice-btn"', self.html)
        for fn in ("drawSphere", "drawTerminal", "drawCorona", "drawAurora", "drawStar"):
            self.assertIn(f"function {fn}(", self.js)
        self.assertIn("motionStill()", self.js)
        self.assertIn("(e.key || '').toLowerCase() === 'v'", self.js)

    def test_natural_delivery(self):
        # Gapless: pieces scheduled on the audio clock one breath apart, not started on "ended".
        self.assertIn("node.start(at)", self.js)
        self.assertIn("nextAt = at + q.audio.duration + BREATH", self.js)
        # A quick first piece (may end at a comma), then longer pieces for intonation.
        self.assertIn("const minLen = first ? (dx.replying ? 8 : 18) : dx.replying ? 40 : 110", self.js)
        self.assertIn("(?<=.{40})[,;:](?=\\s)", self.js)
        # Live captions: the secondary listener, a preview only, which may end the turn early.
        self.assertIn("/api/voice/live?sid=", self.js)
        self.assertIn("d.endpoint && r.ms > 650", self.js)
        self.assertIn("function chime(kind)", self.js)

    def test_the_stage_is_the_orb_a_status_and_a_slim_bar(self):
        build = self.js[self.js.index("function build() {"):self.js.index("function paintBar()")]
        self.assertNotIn("vs-hint", build)                                   # no instructions on screen
        self.assertIn('class="vs-type"', build)                              # the text box lives in the bar
        self.assertIn('<div class="vs-bar">', build)
        self.assertIn("idle: 'Ready', listening: 'Listening'", self.js)       # one word for what it's doing
        css = self.html[self.html.index(".voice-stage {"):self.html.index("html.voice-on .idle-screen")]
        self.assertIn(".voice-stage:not(.captions) .vs-cap.ai { display: none; }", css)
        self.assertIn('What I heard', build)                            # your transcript is always visible
        self.assertIn("width: min(70vmin, 620px, 57vh)", css)
        self.assertIn("height: 42px", css)
        # Settings > Voice's new controls reach the page's behaviour.
        for key in ("voice_breath", "voice_volume", "voice_patience", "voice_sensitivity", "voice_captions"):
            self.assertIn(f"set().{key}", self.js, key)
        self.assertIn("lang: turnLang", self.js)                            # the reply's language goes with each piece
        self.assertIn("/[\\p{L}\\p{N}]/u", self.js)                          # Hebrew text is spoken too, not only a-z

    def test_voice_approvals_are_strict(self):
        # The approval shows above the bar; Enter/Esc or the buttons answer it; a spoken answer must be the
        # whole phrase, "approve" never counts from a barge-in, and only voice_ok requests are sent as voice.
        self.assertIn("function approval(p)", self.js)
        self.assertIn("Voice.approval(p);", self.html)
        self.assertIn("if (c === 'yes' && ask.voice_ok && !barged)", self.js)
        self.assertIn("|| Voice.active) return;", self.html)                 # the chat card's Enter doesn't fire in voice mode
        self.assertNotRegex(self.js, r"enqueue\([^)]*[Aa]pprove")             # it never says the word itself
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        fn = self.js[self.js.index("  function command(text) {"):self.js.index("    return '';\n  }", self.js.index("  function command(text) {")) + len("    return '';\n  }")]
        cases = {"Approve.": "yes", "approve it": "yes", "Yes, approve.": "yes", "allow": "yes", "אשר": "yes",
                 "Cancel.": "no", "no": "no", "stop": "no", "Deny it": "no", "לא": "no",
                 "I don't approve of this": "", "approve the deletion and then delete everything": "", "yes": "",
                 "okay": "", "sure": "", "approval": "", "improve": "", "": ""}
        script = fn + "\nconst cases = " + json_dumps(cases) + ";\nconst bad = Object.entries(cases).filter(([t, w]) => command(t) !== w);\n" \
            "console.log(JSON.stringify(bad));"
        out = subprocess.run([node, "-e", script], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(out.stdout.strip(), "[]", out.stderr)

    def test_privacy_of_the_microphone(self):
        self.assertIn("echoCancellation: true", self.js)
        self.assertNotRegex(self.js, r"localStorage|indexedDB")
        # only preferences persist here: settings, and where MossadGPT's voice panel is docked - never audio or words
        self.assertEqual(re.findall(r"store\.set\([^;]+", self.js), ["store.set('voice.dock', dock)", "store.set('settings', S.settings)"])
        self.assertIn("stream.getTracks().forEach(t => t.stop())", self.js)          # the mic is released on close
        self.assertIn("voice: true", self.html)


if __name__ == "__main__":
    unittest.main()
