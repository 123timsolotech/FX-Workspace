"""
voice_natural.py
================
The natural-sounding voice and the better listeners, through sherpa-onnx (one ONNX runtime,
CPU, no other Python dependencies). voice.py decides when each is used.

  Natural voice    Kokoro-82M (StyleTTS 2 + ISTFTNet, Apache 2.0) - the open model closest to
                   commercial assistants in blind listening tests (TTS Arena Elo ~1065, within
                   ~60 of the best open models, which all need a large GPU). 24 kHz, 50+ voices.
  Accurate         NVIDIA Parakeet TDT 0.6B v2 (int8): far fewer mistakes than Whisper Base on
  listener         English, with punctuation and capitals; ~0.2 s for a sentence on the CPU.
  Live captions    a small streaming Zipformer (20M): shows your words while you are still
  (secondary       talking and notices the end of a sentence; its text is only a preview - the
  listener)        accurate listener (or Whisper) makes the words that are sent.

The models are downloaded by voice.setup() into data/voice/sherpa/<pack>/. Nothing here
touches the network; audio stays in memory.
"""
from __future__ import annotations

import array
import math
import os
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SHERPA_DIR = os.path.join(BASE_DIR, "data", "voice", "sherpa")
_REL = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
# pack -> what "Set up voice" downloads (a .tar.bz2 that unpacks to data/voice/sherpa/<dir>/)
# Full precision is the default: measured on a Ryzen 5 3600 it makes speech 2.9x faster than real time
# (6 threads), while the int8 copy is 1.4x SLOWER than real time - int8 needs AVX-512 VNNI to be quick,
# which older CPUs lack. "Compact" is for newer CPUs or small disks.
PACKS = {
    "kokoro": {"label": "Natural voice (Kokoro, 50+ voices)", "dir": "kokoro-multi-lang-v1_0", "check": "model.onnx",
               "url": f"{_REL}/tts-models/kokoro-multi-lang-v1_0.tar.bz2", "mb": 334},
    "kokoro-compact": {"label": "Natural voice, compact (Kokoro int8 - smaller, but slow on CPUs without AVX-512)",
                       "dir": "kokoro-int8-multi-lang-v1_0", "check": "model.int8.onnx",
                       "url": f"{_REL}/tts-models/kokoro-int8-multi-lang-v1_0.tar.bz2", "mb": 126},
    "parakeet": {"label": "Accurate listener (NVIDIA Parakeet)", "dir": "sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8",
                 "check": "encoder.int8.onnx", "url": f"{_REL}/asr-models/sherpa-onnx-nemo-parakeet-tdt-0.6b-v2-int8.tar.bz2", "mb": 460},
    "live": {"label": "Live captions (streaming listener)", "dir": "sherpa-onnx-streaming-zipformer-en-20M-2023-02-17",
             "check": "tokens.txt", "url": f"{_REL}/asr-models/sherpa-onnx-streaming-zipformer-en-20M-2023-02-17.tar.bz2", "mb": 122},
}
# Kokoro v1.0 speakers (sid). Grades are the model card's (A best); the list shows the good ones first.
VOICES = {
    "af_heart": (3, "Heart - warm, natural (US)", "A"),
    "af_bella": (2, "Bella - bright, expressive (US)", "A-"),
    "af_nicole": (6, "Nicole - soft, close (US)", "B-"),
    "af_sarah": (9, "Sarah - clear (US)", "C+"),
    "af_aoede": (1, "Aoede - calm (US)", "C+"),
    "af_kore": (5, "Kore - steady (US)", "C+"),
    # sherpa-onnx order: 11 adam, 12 echo, 13 eric, 14 fenrir, 15 liam, 16 michael, 17 onyx, 18 puck, 19 santa
    "am_michael": (16, "Michael - relaxed (US, male)", "C+"),
    "am_fenrir": (14, "Fenrir - deep (US, male)", "C+"),
    "am_puck": (18, "Puck - upbeat (US, male)", "C+"),
    "am_echo": (12, "Echo - neutral (US, male)", "D"),
    "bf_emma": (21, "Emma - British", "B-"),
    "bf_isabella": (22, "Isabella - British", "C"),
    "bm_george": (26, "George - British, male", "C"),
    "bm_fable": (25, "Fable - British, male", "C"),
    # The rest of Kokoro's English speakers: rougher, but some like them.
    "af_alloy": (0, "Alloy - even (US)", "C"),
    "af_nova": (7, "Nova - crisp (US)", "C"),
    "af_sky": (10, "Sky - light (US)", "C-"),
    "af_jessica": (4, "Jessica - casual (US)", "D"),
    "af_river": (8, "River - mellow (US)", "D"),
    "am_eric": (13, "Eric - plain (US, male)", "D"),
    "am_liam": (15, "Liam - young (US, male)", "D"),
    "am_onyx": (17, "Onyx - low (US, male)", "D"),
    "am_santa": (19, "Santa - jolly (US, male)", "D-"),
    "am_adam": (11, "Adam - flat (US, male)", "F+"),
    "bf_alice": (20, "Alice - British", "D"),
    "bf_lily": (23, "Lily - British", "D"),
    "bm_lewis": (27, "Lewis - British, male", "D+"),
    "bm_daniel": (24, "Daniel - British, male", "D"),
}


def available() -> bool:
    try:
        import sherpa_onnx  # noqa: F401
        return True
    except Exception:
        return False


def pack_dir(pack: str) -> str:
    return os.path.join(SHERPA_DIR, PACKS[pack]["dir"])


def installed(pack: str) -> bool:
    return os.path.isfile(os.path.join(pack_dir(pack), PACKS[pack]["check"]))


def _threads(share: float = 0.5) -> int:
    return max(1, min(8, int((os.cpu_count() or 4) * share)))


# ==============================================================================
# Audio helpers (no numpy: array + math are plenty for a sentence of audio)
# ==============================================================================
def polish(samples, sample_rate: int, trim: bool = True) -> array.array:
    """Silence trimmed at both ends (a short natural margin kept), 8 ms fades so joins never click,
    and a steady level (peak at about -1.5 dBFS), as 16-bit PCM."""
    x = array.array("f", samples)
    n = len(x)
    if not n:
        return array.array("h")
    if trim:
        thr, win = 0.012, max(1, sample_rate // 100)
        first = next((i for i in range(0, n, win) if max(abs(v) for v in x[i:i + win]) > thr), 0)
        last = next((i for i in range(n - win, -1, -win) if max(abs(v) for v in x[i:i + win]) > thr), n - win)
        keep_front, keep_back = int(sample_rate * 0.03), int(sample_rate * 0.06)
        x = x[max(0, first - keep_front):min(n, last + win + keep_back)]
        n = len(x)
    peak = max((abs(v) for v in x), default=0.0) or 1.0
    gain = min(4.0, 0.84 / peak)
    fade = min(n // 2, int(sample_rate * 0.008))
    out = array.array("h", bytes(2 * n))
    for i in range(n):
        g = gain
        if i < fade:
            g *= i / fade
        elif i >= n - fade:
            g *= (n - 1 - i) / fade
        v = x[i] * g
        out[i] = 32767 if v >= 1 else -32768 if v <= -1 else int(v * 32767)
    return out


def wav_bytes(pcm: array.array, sample_rate: int) -> bytes:
    import io
    import wave
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def pcm16_to_float(data: bytes) -> list:
    a = array.array("h")
    a.frombytes(data[: len(data) // 2 * 2])
    return [v / 32768.0 for v in a]


def wav_to_float16k(wav: bytes) -> list:
    """A 16 kHz mono 16-bit WAV (what the page sends) -> float samples."""
    import io
    import wave
    with wave.open(io.BytesIO(wav)) as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            raise ValueError("Expected 16 kHz mono 16-bit audio.")
        return pcm16_to_float(w.readframes(w.getnframes()))


# ==============================================================================
# The natural voice
# ==============================================================================
class Kokoro:
    def __init__(self):
        self.tts = None
        self.pack = ""
        self.lock = threading.Lock()

    def _load(self, pack: str):
        import sherpa_onnx as so
        d = pack_dir(pack)
        model = os.path.join(d, PACKS[pack]["check"])
        lex = ",".join(os.path.join(d, f) for f in ("lexicon-us-en.txt", "lexicon-zh.txt") if os.path.isfile(os.path.join(d, f)))
        cfg = so.OfflineTtsConfig(
            model=so.OfflineTtsModelConfig(
                kokoro=so.OfflineTtsKokoroModelConfig(model=model, voices=os.path.join(d, "voices.bin"),
                                                      tokens=os.path.join(d, "tokens.txt"),
                                                      data_dir=os.path.join(d, "espeak-ng-data"),
                                                      dict_dir=os.path.join(d, "dict") if os.path.isdir(os.path.join(d, "dict")) else "",
                                                      lexicon=lex),
                num_threads=_threads(0.5), provider="cpu"),
            max_num_sentences=1)
        self.tts, self.pack = so.OfflineTts(cfg), pack

    def speak(self, text: str, voice: str, speed: float, pack: str = "kokoro") -> tuple[array.array, int]:
        sid = VOICES.get(voice, VOICES["af_heart"])[0]
        with self.lock:
            if self.tts is None or self.pack != pack:
                self._load(pack)
            audio = self.tts.generate(text, sid=sid, speed=float(speed))
        return polish(audio.samples, audio.sample_rate), audio.sample_rate

    def unload(self):
        with self.lock:
            self.tts, self.pack = None, ""


kokoro = Kokoro()


# ==============================================================================
# The accurate listener
# ==============================================================================
def tidy(text: str) -> str:
    """Parakeet's small habits: "made$215" -> "made $215", doubled spaces."""
    import re
    text = re.sub(r"(?<=[A-Za-z])([$€£])(?=\d)", r" \1", str(text))
    return " ".join(text.split())


class Accurate:
    def __init__(self):
        self.rec = None
        self.lock = threading.Lock()

    def transcribe(self, samples16k: list) -> str:
        import sherpa_onnx as so
        with self.lock:
            if self.rec is None:
                d = pack_dir("parakeet")
                self.rec = so.OfflineRecognizer.from_transducer(
                    encoder=os.path.join(d, "encoder.int8.onnx"), decoder=os.path.join(d, "decoder.int8.onnx"),
                    joiner=os.path.join(d, "joiner.int8.onnx"), tokens=os.path.join(d, "tokens.txt"),
                    num_threads=_threads(0.5), model_type="nemo_transducer", decoding_method="greedy_search")
            s = self.rec.create_stream()
            # A little silence after the speech lets the model finish the last word.
            s.accept_waveform(16000, list(samples16k) + [0.0] * 4800)
            self.rec.decode_stream(s)
            return tidy(s.result.text or "")

    def unload(self):
        with self.lock:
            self.rec = None


accurate = Accurate()


# ==============================================================================
# Live captions: the secondary, streaming listener
# ==============================================================================
class Live:
    """One stream per utterance (sid). feed() returns what it has heard so far and whether it thinks
    the sentence ended; the page shows the words and may stop recording early."""
    MAX_STREAMS = 4

    def __init__(self):
        self.rec = None
        self.streams: dict = {}           # sid -> [stream, last_used, text]
        self.lock = threading.Lock()

    def _load(self):
        import sherpa_onnx as so
        d = pack_dir("live")
        name = lambda part: next((os.path.join(d, f) for f in sorted(os.listdir(d))       # noqa: E731
                                  if f.startswith(part) and f.endswith(".int8.onnx")), None) or \
            next(os.path.join(d, f) for f in sorted(os.listdir(d)) if f.startswith(part) and f.endswith(".onnx"))
        self.rec = so.OnlineRecognizer.from_transducer(
            tokens=os.path.join(d, "tokens.txt"), encoder=name("encoder"), decoder=name("decoder"), joiner=name("joiner"),
            num_threads=_threads(0.25), sample_rate=16000, feature_dim=80, decoding_method="greedy_search",
            enable_endpoint_detection=True, rule1_min_trailing_silence=2.0, rule2_min_trailing_silence=0.7,
            rule3_min_utterance_length=25)

    def feed(self, sid: str, samples16k: list) -> dict:
        with self.lock:
            if self.rec is None:
                self._load()
            now = time.time()
            for k in [k for k, v in self.streams.items() if now - v[1] > 60]:
                self.streams.pop(k, None)
            entry = self.streams.get(sid)
            if entry is None:
                if len(self.streams) >= self.MAX_STREAMS:
                    self.streams.pop(min(self.streams, key=lambda k: self.streams[k][1]))
                entry = self.streams[sid] = [self.rec.create_stream(), now, ""]
                # A second of silence first: this small model drops the first word or two without it
                # (measured: "...ly nightfall" with 0.4 s, "after early nightfall" with 1 s).
                entry[0].accept_waveform(16000, [0.0] * 16000)
            stream = entry[0]
            entry[1] = now
            if samples16k:
                stream.accept_waveform(16000, samples16k)
            while self.rec.is_ready(stream):
                self.rec.decode_stream(stream)
            text = (self.rec.get_result(stream) or "").strip().lower()
            entry[2] = text
            return {"partial": text, "endpoint": bool(text) and self.rec.is_endpoint(stream)}

    def end(self, sid: str):
        with self.lock:
            self.streams.pop(sid, None)

    def unload(self):
        with self.lock:
            self.rec, self.streams = None, {}


live = Live()


def unload_all():
    kokoro.unload()
    accurate.unload()
    live.unload()
