"""
labs_ncm_worker.py
==================
Runs in the Labs Python (ncm_lab.py starts it): HazyResearch's Next-Concept Model on TinyStories writes one story and
prints every level's text as one JSON line, coarse to fine - the reveal. Built from the repo's scripts/eval_shard.py:
the same config, checkpoint files, VQ-VAE tokenizer and generate() call; only the per-level decode is ours (the repo
decodes the last level only).

  python labs_ncm_worker.py <repo dir> <checkpoint dir> <seed>
"""
import json
import os
import sys
import time


def say(**kw):
    print(json.dumps(kw), flush=True)


def main():
    repo, ck, seed = sys.argv[1], sys.argv[2], int(sys.argv[3])
    sys.path.insert(0, repo)
    os.chdir(repo)
    import torch
    from omegaconf import OmegaConf
    from tokenization.vqvae_tokenizer import VQVAETokenizer
    from models.next_concept import NextConceptTransformer

    if not torch.cuda.is_available():
        say(ok=False, message="NCM needs the NVIDIA GPU, and Labs' PyTorch can't see it.")
        return
    t_load = time.time()
    cfg = OmegaConf.load(os.path.join(repo, "config", "eval", "tinystories.yaml"))
    g = cfg.generation
    cfg.tokenizer.vqvae.checkpoint_path = os.path.join(ck, cfg.checkpoints.tokenizer)
    if "bpe_vocab" in cfg.checkpoints and "bpe_merges" in cfg.checkpoints:
        cfg.tokenizer.pre_tokenizer.vocab_file_path = os.path.join(ck, cfg.checkpoints.bpe_vocab)
        cfg.tokenizer.pre_tokenizer.merges_file_path = os.path.join(ck, cfg.checkpoints.bpe_merges)
    tk = VQVAETokenizer.from_config(cfg.tokenizer)
    tk.vqvae = tk.vqvae.cuda().eval()
    net = NextConceptTransformer(vqvae_local=tk.vqvae, config=cfg.model).cuda()
    sd = torch.load(os.path.join(ck, cfg.checkpoints.generator), map_location="cpu", weights_only=False)
    net.load_state_dict(sd["model"], strict=True)
    net.eval()
    eos, pad = tk.pre_tokenizer.eos_token_id, tk.pre_tokenizer.pad_token_id
    say(loaded=round(time.time() - t_load, 1), levels=len(tk.vqvae.vq.levels))

    def text(ids):
        if eos in ids:
            ids = ids[:ids.index(eos)]
        return tk.pre_tokenizer.decode([i for i in ids if i not in (eos, pad)], skip_special_tokens=True)

    torch.manual_seed(seed)
    with torch.no_grad():
        x = torch.full((1, 1), eos, device="cuda")
        torch.cuda.synchronize()
        t0 = time.time()
        codes = net.generate(None, x, temperature=g.get("temperature", 1.0), top_k=g.get("top_k"),
                             top_p=g.get("top_p"), cfg_scale=g.get("cfg_scale", 0.0), cfg_null_eos=eos)
        torch.cuda.synchronize()
        seconds = time.time() - t0
        _, zl = tk.vqvae.vq.idx_to_zhats(codes, return_zlevels=True)
        for i, z in enumerate(zl):
            try:
                ids = tk.vqvae.decoder(z).argmax(-1)[0].tolist()
                say(level=i + 1, of=len(zl), text=text(ids))
            except Exception as e:                       # a level the decoder can't read on its own
                say(level=i + 1, of=len(zl), text="", note=str(e)[:160])
    say(ok=True, seconds=round(seconds, 3))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        say(ok=False, message=f"{type(e).__name__}: {str(e)[:300]}")
