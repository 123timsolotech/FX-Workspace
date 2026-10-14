"""
workers/tts_worker.py - a spoken line for a video clip. Runs in the Chatterbox environment
(G:\\ClankerLocalCpp\\Audio\\tts-venv), started by video_audio.py.

    python tts_worker.py <job.json>

job: {"text", "out" (wav path), "language" (en default; he, ja, ru, ... use the multilingual model),
      "voice" (optional wav of a voice to imitate, 5-10 s), "emotion" (0.25 calm .. 1.0 very expressive)}

Chatterbox (Resemble AI, MIT) - its outputs carry Resemble's inaudible Perth watermark. The model downloads into
HF_HOME on first use. Prints "DONE <seconds of speech>".
"""
import json
import sys

import numpy as np
import soundfile as sf
import torch


def main() -> None:
    with open(sys.argv[1], encoding="utf-8") as f:
        job = json.load(f)
    text = str(job.get("text") or "").strip()
    lang = str(job.get("language") or "en")
    emotion = max(0.25, min(1.5, float(job.get("emotion") or 0.5)))
    # More emotion needs less pull toward the reference pace (the authors' advice: cfg ~0.3 for expressive lines).
    cfg = 0.5 if emotion <= 0.6 else 0.3
    print("STAGE loading", flush=True)
    if lang == "en":
        from chatterbox.tts import ChatterboxTTS
        model = ChatterboxTTS.from_pretrained(device="cuda")
        kw = {}
    else:
        from chatterbox.mtl_tts import ChatterboxMultilingualTTS
        model = ChatterboxMultilingualTTS.from_pretrained(device="cuda")
        kw = {"language_id": lang}
    print("STAGE speaking", flush=True)
    with torch.inference_mode():
        wav = model.generate(text, audio_prompt_path=job.get("voice") or None, exaggeration=emotion,
                             cfg_weight=cfg, **kw)
    audio = wav.squeeze(0).float().cpu().numpy().astype(np.float32)
    sf.write(job["out"], audio, model.sr)
    print(f"DONE {len(audio) / model.sr:.2f}", flush=True)


if __name__ == "__main__":
    main()
