"""Offline Hebrew speech: Phonikud diacritics/IPA and the Shaul Piper voice.

CPU only. All models, including the tokenizer, are installed by voice.setup;
inference never calls Hugging Face or downloads anything implicitly.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import threading

DIRECTORY = Path(__file__).resolve().parent / 'data' / 'voice' / 'hebrew'
ASSETS = (
    ('phonikud-1.0.int8.onnx', 'thewh1teagle/phonikud-onnx', 'phonikud-1.0.int8.onnx', 308),
    ('shaul.onnx', 'thewh1teagle/phonikud-tts-checkpoints', 'shaul.onnx', 64),
    ('model.config.json', 'thewh1teagle/phonikud-tts-checkpoints', 'model.config.json', .01),
    ('tokenizer.json', 'dicta-il/dictabert-large-char-menaked', 'tokenizer.json', .03),
)
_lock = threading.Lock()
_diacritics = _speaker = None


def available():
    return all(importlib.util.find_spec(name) is not None
               for name in ('phonikud', 'phonikud_onnx', 'piper_onnx', 'onnxruntime'))


def ready():
    return available() and all((DIRECTORY / name).is_file() and (DIRECTORY / name).stat().st_size > 0
                               for name, *_ in ASSETS)


def downloads():
    return [(f'hebrew-{i}', f'Hebrew: {name}', f'https://huggingface.co/{repo}/resolve/main/{remote}',
             str(DIRECTORY / name), mb, 'file')
            for i, (name, repo, remote, mb) in enumerate(ASSETS)
            if not (DIRECTORY / name).is_file() or (DIRECTORY / name).stat().st_size == 0]


def _load():
    global _diacritics, _speaker
    if _diacritics is not None and _speaker is not None:
        return
    if not ready():
        raise RuntimeError('Hebrew speech is not set up. Install requirements.txt and use Settings > Voice > Set up voice.')
    import onnxruntime as ort
    from tokenizers import Tokenizer
    from phonikud_onnx import Phonikud
    from phonikud_onnx.model import OnnxModel
    from piper_onnx import Piper

    def session(name):
        options = ort.SessionOptions()
        options.intra_op_num_threads = max(2, min(6, (os.cpu_count() or 4) - 2))
        options.inter_op_num_threads = 1
        return ort.InferenceSession(str(DIRECTORY / name), sess_options=options, providers=['CPUExecutionProvider'])

    # phonikud-onnx 1.0.6's constructor always fetches a tokenizer online.
    # Initialize its pinned adapter from the explicitly installed local file.
    model = OnnxModel.__new__(OnnxModel)
    model.tokenizer = Tokenizer.from_file(str(DIRECTORY / 'tokenizer.json'))
    model.max_context_length = 2046
    model.session = session('phonikud-1.0.int8.onnx')
    model.input_names = [x.name for x in model.session.get_inputs()]
    model.output_names = [x.name for x in model.session.get_outputs()]
    diacritics = Phonikud.__new__(Phonikud)
    diacritics.model = model
    # Piper 1.0.6 opens JSON with Windows' ANSI encoding and initializes espeak
    # even for raw IPA. This voice needs neither: read its Unicode map as UTF-8
    # and feed Phonikud phonemes directly to the pinned ONNX adapter.
    speaker = Piper.__new__(Piper)
    speaker.config = json.loads((DIRECTORY / 'model.config.json').read_text(encoding='utf-8'))
    speaker.sample_rate = speaker.config['audio']['sample_rate']
    speaker.phoneme_id_map = speaker.config['phoneme_id_map']
    speaker._voices = speaker.config.get('speaker_id_map')
    speaker.sess = session('shaul.onnx')
    speaker.sess_inputs_names = [x.name for x in speaker.sess.get_inputs()]
    _diacritics, _speaker = diacritics, speaker


def speak(text, rate=1.0):
    with _lock:
        _load()
        from phonikud import phonemize
        phonemes = phonemize(_diacritics.add_diacritics(text))
        return _speaker.create(phonemes, is_phonemes=True, length_scale=1 / rate)


def unload():
    global _diacritics, _speaker
    with _lock:
        _diacritics = _speaker = None
