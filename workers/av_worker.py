"""
workers/av_worker.py - sound for a video clip. Runs in the MMAudio environment (G:\\ClankerLocalCpp\\Audio\\mm-venv),
started by video_audio.py with the MMAudio source folder as its working directory (its weights live there).

    python av_worker.py <job.json>

job: {"video", "out", "prompt", "negative", "seed", "steps", "variant",
      "voice": optional wav path, "voice_start": seconds, "music": optional audio path,
      "sfx_gain", "voice_gain", "music_gain"}

1. MMAudio watches the clip and makes matching sound (effects, ambience) for its exact length.
2. A voice line (made by tts_worker.py) is placed at voice_start; music, if given, under everything.
   Effects and music duck under the voice (an envelope follower on the voice), then the mix is
   peak-normalised to -1 dBFS.
3. The video stream is copied unchanged into a new .webm with the mix as Opus audio (PyAV - its wheel
   carries ffmpeg's libraries; no ffmpeg install).

Progress goes to stdout as "STAGE <name>" lines; the result as "DONE <path>".
"""
import json
import sys
from fractions import Fraction

import av
import numpy as np
import torch

torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.allow_tf32 = True
OUT_RATE = 48000                      # Opus


def stage(name: str) -> None:
    print(f"STAGE {name}", flush=True)


def load_audio(path: str, rate: int) -> np.ndarray:
    """Any audio file -> mono float32 at `rate` (PyAV decodes and resamples)."""
    out = []
    with av.open(path) as c:
        rs = av.AudioResampler(format="fltp", layout="mono", rate=rate)
        for frame in c.decode(audio=0):
            for f in rs.resample(frame):
                out.append(f.to_ndarray()[0])
        for f in rs.resample(None):
            out.append(f.to_ndarray()[0])
    return np.concatenate(out).astype(np.float32) if out else np.zeros(0, np.float32)


def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    if src == dst or not len(x):
        return x.astype(np.float32)
    n = int(round(len(x) * dst / src))
    return np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x).astype(np.float32)


def envelope(x: np.ndarray, rate: int, win_s: float = 0.08) -> np.ndarray:
    """A smoothed loudness envelope (0..1) - where the voice speaks."""
    if not len(x):
        return x
    k = max(1, int(rate * win_s))
    e = np.sqrt(np.convolve(x * x, np.ones(k) / k, mode="same"))
    return e / (e.max() or 1.0)


def make_sfx(job: dict, duration: float) -> tuple:
    from mmaudio.eval_utils import all_model_cfg, generate, load_video
    from mmaudio.model.flow_matching import FlowMatching
    from mmaudio.model.networks import get_my_mmaudio
    from mmaudio.model.utils.features_utils import FeaturesUtils

    stage("loading")
    cfg = all_model_cfg[job.get("variant") or "large_44k_v2"]
    cfg.download_if_needed()
    seq = cfg.seq_cfg
    dtype = torch.bfloat16
    with torch.inference_mode():
        net = get_my_mmaudio(cfg.model_name).to("cuda", dtype).eval()
        net.load_weights(torch.load(cfg.model_path, map_location="cuda", weights_only=True))
        feats = FeaturesUtils(tod_vae_ckpt=cfg.vae_path, synchformer_ckpt=cfg.synchformer_ckpt, enable_conditions=True,
                              mode=cfg.mode, bigvgan_vocoder_ckpt=cfg.bigvgan_16k_path, need_vae_encoder=False)
        feats = feats.to("cuda", dtype).eval()
        stage("watching")
        info = load_video(job["video"], duration)
        seq.duration = info.duration_sec
        net.update_seq_lengths(seq.latent_seq_len, seq.clip_seq_len, seq.sync_seq_len)
        rng = torch.Generator(device="cuda")
        rng.manual_seed(int(job.get("seed") or 42))
        fm = FlowMatching(min_sigma=0, inference_mode="euler", num_steps=int(job.get("steps") or 25))
        stage("sound")
        audio = generate(info.clip_frames.unsqueeze(0), info.sync_frames.unsqueeze(0), [job.get("prompt") or ""],
                         negative_text=[job.get("negative") or "music, speech, talking"], feature_utils=feats, net=net,
                         fm=fm, rng=rng, cfg_strength=float(job.get("cfg") or 4.5))
    return audio.float().cpu()[0].numpy().mean(axis=0) if audio.dim() == 3 else audio.float().cpu()[0].numpy(), seq.sampling_rate


def mux(video_in: str, out: str, mix: np.ndarray) -> None:
    """Copy the video stream, add the mix as Opus."""
    with av.open(video_in) as src, av.open(out, "w", format="webm") as dst:
        vin = src.streams.video[0]
        vout = dst.add_stream_from_template(vin)
        aout = dst.add_stream("libopus", rate=OUT_RATE, layout="stereo")
        for pkt in src.demux(vin):
            if pkt.dts is None:
                continue
            pkt.stream = vout
            dst.mux(pkt)
        stereo = np.stack([mix, mix]).astype(np.float32)
        frame_len = 960
        for i in range(0, stereo.shape[1], frame_len):
            chunk = np.ascontiguousarray(stereo[:, i:i + frame_len])
            frame = av.AudioFrame.from_ndarray(chunk, format="fltp", layout="stereo")
            frame.sample_rate = OUT_RATE
            frame.pts = i
            frame.time_base = Fraction(1, OUT_RATE)
            for p in aout.encode(frame):
                dst.mux(p)
        for p in aout.encode(None):
            dst.mux(p)


def main() -> None:
    with open(sys.argv[1], encoding="utf-8") as f:
        job = json.load(f)
    with av.open(job["video"]) as c:
        v = c.streams.video[0]
        duration = float(v.frames / v.average_rate) if v.frames and v.average_rate else float(c.duration or 0) / 1e6
    duration = max(0.5, duration)
    sfx, sr = make_sfx(job, duration)
    stage("mixing")
    n = int(round(duration * OUT_RATE))
    sfx = np.pad(resample(sfx, sr, OUT_RATE), (0, n))[:n] * float(job.get("sfx_gain", 1.0))
    mix = sfx.copy()
    duck = np.zeros(n, np.float32)
    if job.get("voice"):
        voice = load_audio(job["voice"], OUT_RATE) * float(job.get("voice_gain", 1.0))
        start = int(float(job.get("voice_start") or 0) * OUT_RATE)
        lane = np.zeros(n, np.float32)
        piece = voice[: max(0, n - start)]
        lane[start:start + len(piece)] = piece
        duck = envelope(lane, OUT_RATE)
        mix = sfx * (1 - 0.65 * duck) + lane
    if job.get("music"):
        music = load_audio(job["music"], OUT_RATE)
        music = np.pad(music, (0, n))[:n] * float(job.get("music_gain", 0.35))
        mix = mix + music * (1 - 0.6 * duck)
    peak = float(np.abs(mix).max() or 1.0)
    mix = (mix / peak * 0.89).astype(np.float32)                 # -1 dBFS
    stage("saving")
    mux(job["video"], job["out"], mix)
    print(f"DONE {job['out']}", flush=True)


if __name__ == "__main__":
    main()
