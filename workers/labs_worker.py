"""
labs_worker.py - Fx Labs' trainer. Runs in Labs' own Python (PyTorch), never in the app's server (labs.py starts it).

  python labs_worker.py <job.json>

job.json (train): {"run": folder, "template": "gpt"|"cnn", "size": "tiny"|"small"|"medium", "steps": N,
                   "resume": bool, "settings": {"lr", "batch", "seed"}, "data": text file or picture folder}
job.json (generate): {"mode": "generate", "run": folder, "prompt", "length", "temperature", "device"}

Writes into the run folder: metrics.jsonl (a line every few steps, plus validation at each check), samples.jsonl
(what the model makes at each check), samples/*.png (the classifier's held-out pictures), ckpt.pt (weights,
optimizer, step - Resume continues from it) and result.json (its last word: done / stopped / failed).
A file named "stop" in the run folder ends the run after saving the checkpoint.

The GPT is a small decoder-only transformer over characters, in the style of nanoGPT (MIT); the classifier a plain
conv net. Both are written out here - no other library than torch, numpy and pillow.
"""
import json
import math
import os
import random
import sys
import time

import torch
import torch.nn as nn
import torch.nn.functional as F

GPT_SIZES = {"tiny": dict(n_layer=4, n_head=4, n_embd=128, block=128, batch=32, lr=1e-3),
             "small": dict(n_layer=6, n_head=6, n_embd=384, block=256, batch=32, lr=6e-4),
             "medium": dict(n_layer=8, n_head=8, n_embd=512, block=256, batch=24, lr=4e-4)}
CNN_SIZES = {"tiny": dict(px=32, width=24, batch=64, lr=2e-3),
             "small": dict(px=64, width=32, batch=64, lr=1.5e-3),
             "medium": dict(px=96, width=48, batch=48, lr=1e-3)}
IMG_EXT = (".png", ".jpg", ".jpeg", ".webp", ".bmp")


class Run:
    """The run folder: metrics, samples, the stop file, the last word."""

    def __init__(self, folder):
        self.folder = folder
        self.metrics = open(os.path.join(folder, "metrics.jsonl"), "a", encoding="utf-8")
        self.t0 = time.time()

    def log(self, **row):
        self.metrics.write(json.dumps({k: (round(v, 5) if isinstance(v, float) else v) for k, v in row.items()}) + "\n")
        self.metrics.flush()

    def sample(self, **row):
        with open(os.path.join(self.folder, "samples.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def stop_asked(self):
        return os.path.exists(os.path.join(self.folder, "stop"))

    def finish(self, state, message=""):
        with open(os.path.join(self.folder, "result.json"), "w", encoding="utf-8") as f:
            json.dump({"state": state, "message": message}, f)


def lr_at(step, total, peak, warmup):
    """Warm up, then a cosine down to a tenth."""
    if step < warmup:
        return peak * (step + 1) / warmup
    p = min(1.0, (step - warmup) / max(1, total - warmup))
    return peak * (0.1 + 0.9 * 0.5 * (1 + math.cos(math.pi * p)))


# ==============================================================================
# The small GPT
# ==============================================================================
ACTS = {"gelu": nn.GELU, "relu": nn.ReLU, "silu": nn.SiLU}


class Block(nn.Module):
    def __init__(self, n_embd, n_head, dropout, mlp=4, act="gelu"):
        super().__init__()
        self.ln1, self.ln2 = nn.LayerNorm(n_embd), nn.LayerNorm(n_embd)
        self.qkv, self.proj = nn.Linear(n_embd, 3 * n_embd), nn.Linear(n_embd, n_embd)
        self.mlp = nn.Sequential(nn.Linear(n_embd, mlp * n_embd), ACTS[act](), nn.Linear(mlp * n_embd, n_embd), nn.Dropout(dropout))
        self.n_head, self.dropout = n_head, dropout

    def forward(self, x):
        B, T, C = x.shape
        q, k, v = self.qkv(self.ln1(x)).split(C, dim=2)
        q, k, v = (t.view(B, T, self.n_head, C // self.n_head).transpose(1, 2) for t in (q, k, v))
        y = F.scaled_dot_product_attention(q, k, v, is_causal=True, dropout_p=self.dropout if self.training else 0.0)
        x = x + self.proj(y.transpose(1, 2).contiguous().view(B, T, C))
        return x + self.mlp(self.ln2(x))


class GPT(nn.Module):
    def __init__(self, vocab, n_layer, n_head, n_embd, block, dropout=0.1, blocks=None, act="gelu", tie=True):
        super().__init__()
        self.block = block
        self.tok, self.pos = nn.Embedding(vocab, n_embd), nn.Embedding(block, n_embd)
        blocks = blocks or [{"heads": n_head, "mlp": 4}] * n_layer      # each block may have its own heads and MLP size
        self.blocks = nn.ModuleList(Block(n_embd, b["heads"], dropout, b["mlp"], act) for b in blocks)
        self.ln, self.head = nn.LayerNorm(n_embd), nn.Linear(n_embd, vocab, bias=False)
        if tie:
            self.tok.weight = self.head.weight                  # tied, as in GPT-2
        self.apply(lambda m: nn.init.normal_(m.weight, 0, 0.02) if isinstance(m, (nn.Linear, nn.Embedding)) else None)

    def forward(self, idx, targets=None):
        x = self.tok(idx) + self.pos(torch.arange(idx.shape[1], device=idx.device))
        for b in self.blocks:
            x = b(x)
        logits = self.head(self.ln(x))
        loss = None if targets is None else F.cross_entropy(logits.view(-1, logits.size(-1)), targets.view(-1))
        return logits, loss

    @torch.no_grad()
    def generate(self, idx, n, temperature=0.8, top_k=40):
        for _ in range(n):
            logits, _ = self(idx[:, -self.block:])
            logits = logits[:, -1, :] / temperature
            v, _ = torch.topk(logits, min(top_k, logits.size(-1)))
            logits[logits < v[:, [-1]]] = -float("inf")
            idx = torch.cat([idx, torch.multinomial(F.softmax(logits, dim=-1), 1)], dim=1)
        return idx


def weight_sample(mats, k=8):
    """{part: k x k corner of its main weight matrix (out x in)} - real numbers for the network view's wires."""
    out = {}
    for name, w in mats:
        w = w.detach().float()
        if w.dim() == 4:                       # a conv: out x in x 3 x 3 -> the kernel's mean per channel pair
            w = w.mean(dim=(2, 3))
        out[name] = [[round(x, 4) for x in row] for row in w[:k, :k].cpu().tolist()]
    return out


def model_cfg(cfg):
    return dict(blocks=cfg.get("blocks"), act=cfg.get("act", "gelu"), tie=cfg.get("tie", True), dropout=cfg.get("dropout", 0.1))


def group_norms(groups):
    """{part: gradient norm} for named groups of parameters, in one GPU sync - how hard each part is learning right
    now (the network view's heat)."""
    flat = [(name, p.grad) for name, params in groups for p in params if p.grad is not None]
    if not flat:
        return {}
    sq = torch.stack(torch._foreach_norm([g for _, g in flat])).float().pow(2).tolist()
    out = {}
    for (name, _), v in zip(flat, sq):
        out[name] = out.get(name, 0.0) + v
    return {k: round(v ** 0.5, 5) for k, v in out.items()}


def train_gpt(job, run, dev):
    cfg = dict(GPT_SIZES[job["size"]])
    cfg.update({k: v for k, v in job.get("settings", {}).items()
                if k in ("lr", "batch", "n_layer", "n_head", "n_embd", "block", "blocks", "act") and v})
    if "tie" in job.get("settings", {}):
        cfg["tie"] = bool(job["settings"]["tie"])
    if job.get("settings", {}).get("dropout") is not None:
        cfg["dropout"] = float(job["settings"]["dropout"])
    with open(job["data"], encoding="utf-8", errors="replace") as f:
        text = f.read()
    ckpt_path = os.path.join(run.folder, "ckpt.pt")
    ckpt = torch.load(ckpt_path, map_location="cpu", weights_only=False) if job.get("resume") and os.path.exists(ckpt_path) else None
    if ckpt:                                   # the weights decide the shape when it continues
        cfg.update({k: ckpt["cfg"][k] for k in ("n_layer", "n_head", "n_embd", "block", "blocks", "act", "tie") if k in ckpt["cfg"]})
    chars = ckpt["chars"] if ckpt else sorted(set(text))
    stoi = {c: i for i, c in enumerate(chars)}
    data = torch.tensor([stoi.get(c, 0) for c in text], dtype=torch.long)
    cut = int(len(data) * 0.9)
    train, val = data[:cut], data[cut:]
    block = min(cfg["block"], max(16, len(val) // 4))
    model = GPT(len(chars), cfg["n_layer"], cfg["n_head"], cfg["n_embd"], block, **model_cfg(cfg)).to(dev)
    opt = torch.optim.AdamW(model.parameters(), lr=cfg["lr"], betas=(0.9, 0.99), weight_decay=0.1)
    step = 0
    if ckpt:
        model.load_state_dict(ckpt["model"]); opt.load_state_dict(ckpt["opt"]); step = ckpt["step"]
    params = sum(p.numel() for p in model.parameters())
    run.log(event="start", step=step, params=params, vocab=len(chars), tokens=len(data), device=str(dev),
            arch={"n_layer": len(model.blocks), "n_head": cfg["n_head"], "n_embd": cfg["n_embd"], "block": block,
                  "blocks": [{"heads": b.n_head, "mlp": b.mlp[0].out_features // cfg["n_embd"]} for b in model.blocks],
                  "act": cfg.get("act", "gelu"), "tie": cfg.get("tie", True)})
    wmats = lambda: [("embed", model.tok.weight.T)] + [(f"block{i}", b.mlp[0].weight) for i, b in enumerate(model.blocks)] + [("head", model.head.weight)]  # noqa: E731
    groups = [("embed", [model.tok.weight, model.pos.weight])] + [(f"block{i}", list(b.parameters())) for i, b in enumerate(model.blocks)] \
        + [("head", list(model.ln.parameters()))]
    amp = dict(device_type="cuda", dtype=torch.bfloat16) if dev.type == "cuda" else dict(device_type="cpu", enabled=False)

    def batch(src, n):
        ix = torch.randint(len(src) - block - 1, (n,))
        x = torch.stack([src[i:i + block] for i in ix]); y = torch.stack([src[i + 1:i + block + 1] for i in ix])
        return x.to(dev, non_blocking=True), y.to(dev, non_blocking=True)

    def save():
        torch.save({"model": model.state_dict(), "opt": opt.state_dict(), "step": step, "chars": chars,
                    "cfg": {**cfg, "block": block, "vocab": len(chars)}}, ckpt_path + ".tmp")
        os.replace(ckpt_path + ".tmp", ckpt_path)

    def check():
        model.eval()
        with torch.no_grad(), torch.autocast(**amp):
            vl = sum(model(*batch(val, cfg["batch"]))[1].item() for _ in range(20)) / 20
        start = torch.tensor([[stoi.get("\n", 0)]], device=dev)
        out = model.generate(start, 300)[0].tolist()
        model.train()
        run.sample(step=step, text="".join(chars[i] for i in out[1:]))
        run.log(event="weights", step=step, w=weight_sample(wmats()))
        return vl

    total, warmup = job["steps"], min(200, job["steps"] // 10)
    every = max(50, total // 25)
    acc, n, t_last, seen = 0.0, 0, time.time(), 0
    model.train()
    while step < total:
        lr = lr_at(step, total, cfg["lr"], warmup)
        for g in opt.param_groups:
            g["lr"] = lr
        x, y = batch(train, cfg["batch"])
        with torch.autocast(**amp):
            _, loss = model(x, y)
        opt.zero_grad(set_to_none=True)
        loss.backward()
        if (step + 1) % 10 == 0:
            run.log(event="layers", step=step + 1, g=group_norms(groups))
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        opt.step()
        step += 1; acc += loss.item(); n += 1; seen += x.numel()
        if step % 10 == 0 or step == total:
            dt = max(1e-6, time.time() - t_last)
            run.log(step=step, loss=acc / n, lr=lr, rate=seen / dt, unit="chars/s", t=time.time() - run.t0)
            acc, n, t_last, seen = 0.0, 0, time.time(), 0
        if step % every == 0 or step == total:
            run.log(step=step, val_loss=check(), t=time.time() - run.t0)
            save()
        if run.stop_asked():
            save()
            return "stopped", f"Stopped at step {step} - Resume continues from here."
    return "done", ""


def generate(job):
    dev = torch.device("cuda" if job.get("device") != "cpu" and torch.cuda.is_available() else "cpu")
    ckpt = torch.load(os.path.join(job["run"], "ckpt.pt"), map_location="cpu", weights_only=False)
    cfg, chars = ckpt["cfg"], ckpt["chars"]
    model = GPT(cfg["vocab"], cfg["n_layer"], cfg["n_head"], cfg["n_embd"], cfg["block"], **model_cfg(cfg)).to(dev)
    model.load_state_dict(ckpt["model"]); model.eval()
    stoi = {c: i for i, c in enumerate(chars)}
    prompt = job.get("prompt") or "\n"
    idx = torch.tensor([[stoi.get(c, 0) for c in prompt]], device=dev)
    out = model.generate(idx, int(job.get("length", 400)), temperature=float(job.get("temperature", 0.8)))[0].tolist()
    unknown = sorted({c for c in prompt if c not in stoi})
    print(json.dumps({"ok": True, "prompt": prompt, "text": "".join(chars[i] for i in out[len(prompt):]),
                      "step": ckpt["step"], "unknown": unknown, "device": str(dev)}, ensure_ascii=False))


# ==============================================================================
# The picture classifier
# ==============================================================================
def conv(cin, cout):
    return nn.Sequential(nn.Conv2d(cin, cout, 3, padding=1, bias=False), nn.BatchNorm2d(cout), nn.ReLU(inplace=True))


def default_stages(w):
    return [{"ch": w, "convs": 2}, {"ch": 2 * w, "convs": 2}, {"ch": 4 * w, "convs": 2}, {"ch": 8 * w, "convs": 1}]


class CNN(nn.Module):
    def __init__(self, n_classes, w, stages=None, dropout=0.3):
        super().__init__()
        stages = stages or default_stages(w)            # the same layers as before when no stages are given
        layers, cin = [], 3
        for i, s in enumerate(stages):
            if i:
                layers.append(nn.MaxPool2d(2))
            for _ in range(s["convs"]):
                layers.append(conv(cin, s["ch"])); cin = s["ch"]
        self.body = nn.Sequential(*layers, nn.AdaptiveAvgPool2d(1), nn.Flatten(), nn.Dropout(dropout), nn.Linear(cin, n_classes))
        self.stage_of = [i for i, s in enumerate(stages) for _ in range(s["convs"])]

    def forward(self, x):
        return self.body(x)


def load_pictures(folder, px, seed):
    """Every picture, resized to px x px, held in memory (a few thousand pictures is a few hundred MB at most)."""
    import numpy as np
    from PIL import Image
    classes = sorted(d for d in os.listdir(folder) if os.path.isdir(os.path.join(folder, d))
                     and any(f.lower().endswith(IMG_EXT) for f in os.listdir(os.path.join(folder, d))))
    items = [(os.path.join(folder, c, f), i) for i, c in enumerate(classes)
             for f in sorted(os.listdir(os.path.join(folder, c))) if f.lower().endswith(IMG_EXT)]
    random.Random(seed).shuffle(items)
    found = len(items)
    # Held in RAM: a budget of ~4 GB of pictures (3 x px x px bytes each) - 325,000 at 64 px - not a fixed count.
    items = items[:max(1000, int(4e9 // (3 * px * px)))]
    load_pictures.found = found
    xs, ys, paths = [], [], []
    for p, y in items:
        try:
            im = Image.open(p).convert("RGB").resize((px, px), Image.BILINEAR)
        except OSError:
            continue
        xs.append(np.asarray(im, dtype=np.uint8)); ys.append(y); paths.append(p)
    x = torch.from_numpy(np.stack(xs)).permute(0, 3, 1, 2).contiguous()
    return x, torch.tensor(ys), paths, classes


def train_cnn(job, run, dev):
    cfg = dict(CNN_SIZES[job["size"]])
    cfg.update({k: v for k, v in job.get("settings", {}).items() if k in ("lr", "batch", "px", "width", "stages") and v})
    if job.get("settings", {}).get("dropout") is not None:
        cfg["dropout"] = float(job["settings"]["dropout"])
    ckpt_path = os.path.join(run.folder, "ckpt.pt")
    ckpt = torch.load(ckpt_path, map_location="cpu", weights_only=False) if job.get("resume") and os.path.exists(ckpt_path) else None
    if ckpt:                                   # the weights decide the shape when it continues
        cfg.update({k: ckpt["cfg"][k] for k in ("px", "width", "stages") if k in ckpt["cfg"]})
    seed = int(job.get("settings", {}).get("seed") or 0)
    x, y, paths, classes = load_pictures(job["data"], cfg["px"], seed)
    if len(classes) < 2 or len(x) < 20:
        return "failed", "The folder needs at least two kinds of pictures and 20 pictures in all."
    n_val = max(len(classes) * 4, len(x) // 7)
    xv, yv, pv = x[:n_val], y[:n_val], paths[:n_val]
    xt, yt = x[n_val:], y[n_val:]
    mean = (xt.float() / 255).mean(dim=(0, 2, 3), keepdim=True).to(dev)
    std = (xt.float() / 255).std(dim=(0, 2, 3), keepdim=True).clamp_min(1e-3).to(dev)
    prep = lambda b: ((b.to(dev, non_blocking=True).float() / 255) - mean) / std      # noqa: E731
    model = CNN(len(classes), cfg["width"], cfg.get("stages"), cfg.get("dropout", 0.3)).to(dev)
    opt = torch.optim.AdamW(model.parameters(), lr=cfg["lr"], weight_decay=5e-4)
    step = 0
    if ckpt:
        model.load_state_dict(ckpt["model"]); opt.load_state_dict(ckpt["opt"]); step = ckpt["step"]
    run.log(event="start", step=step, params=sum(p.numel() for p in model.parameters()), classes=classes,
            train=len(xt), val=len(xv), found=getattr(load_pictures, "found", len(x)), device=str(dev),
            arch={"px": cfg["px"], "width": cfg["width"], "stages": cfg.get("stages") or default_stages(cfg["width"])})
    convs = [m for m in model.body if isinstance(m, nn.Sequential)]
    # heat per stage (the network view draws stages): every conv of a stage counts into it
    groups = [(f"stage{model.stage_of[i]}", list(c.parameters())) for i, c in enumerate(convs)] + [("head", list(model.body[-1].parameters()))]
    first = {}
    for i, c in enumerate(convs):
        first.setdefault(model.stage_of[i], c[0].weight)
    wmats = lambda: [(f"stage{s}", w) for s, w in first.items()] + [("head", model.body[-1].weight)]  # noqa: E731
    amp = dict(device_type="cuda", dtype=torch.bfloat16) if dev.type == "cuda" else dict(device_type="cpu", enabled=False)
    os.makedirs(os.path.join(run.folder, "samples"), exist_ok=True)

    def augment(b):
        if random.random() < 0.5:
            b = b.flip(3)
        pad = cfg["px"] // 8
        b = F.pad(b, (pad, pad, pad, pad), mode="reflect")
        i, j = random.randint(0, 2 * pad), random.randint(0, 2 * pad)
        return b[:, :, i:i + cfg["px"], j:j + cfg["px"]]

    def save():
        torch.save({"model": model.state_dict(), "opt": opt.state_dict(), "step": step, "classes": classes,
                    "cfg": cfg, "mean": mean.cpu(), "std": std.cpu()}, ckpt_path + ".tmp")
        os.replace(ckpt_path + ".tmp", ckpt_path)

    def check():
        from PIL import Image
        model.eval()
        losses, hits, preds, confs = [], 0, [], []
        per = {c: [0, 0] for c in classes}
        with torch.no_grad(), torch.autocast(**amp):
            for i in range(0, len(xv), 256):
                logits = model(prep(xv[i:i + 256])).float()
                losses.append(F.cross_entropy(logits, yv[i:i + 256].to(dev), reduction="sum").item())
                p = logits.softmax(-1)
                c, k = p.max(-1)
                preds += k.tolist(); confs += c.tolist()
        for t, k in zip(yv.tolist(), preds):
            per[classes[t]][1] += 1
            if t == k:
                hits += 1; per[classes[t]][0] += 1
        model.train()
        # Twelve held-out pictures with what it thinks they are - its mistakes first, then right ones.
        misses = [i for i, (t, k) in enumerate(zip(yv.tolist(), preds)) if t != k]
        missed = set(misses)
        pick = (misses[:12] + [i for i in range(min(len(xv), 24)) if i not in missed])[:12]
        shown = []
        for i in pick:
            name = f"v{i}.png"
            if not os.path.exists(os.path.join(run.folder, "samples", name)):
                Image.open(pv[i]).convert("RGB").resize((96, 96)).save(os.path.join(run.folder, "samples", name))
            shown.append({"img": name, "truth": classes[yv[i]], "guess": classes[preds[i]], "conf": round(confs[i], 3)})
        run.sample(step=step, pictures=shown, per_class={c: round(a / max(1, n), 3) for c, (a, n) in per.items()})
        run.log(event="weights", step=step, w=weight_sample(wmats()))
        return sum(losses) / len(xv), hits / len(xv)

    total, warmup = job["steps"], min(100, job["steps"] // 10)
    every = max(50, total // 25)
    acc, hit, n, t_last, seen = 0.0, 0, 0, time.time(), 0
    model.train()
    while step < total:
        lr = lr_at(step, total, cfg["lr"], warmup)
        for g in opt.param_groups:
            g["lr"] = lr
        ix = torch.randint(len(xt), (cfg["batch"],))
        xb, yb = augment(prep(xt[ix])), yt[ix].to(dev)
        with torch.autocast(**amp):
            logits = model(xb)
            loss = F.cross_entropy(logits.float(), yb)
        opt.zero_grad(set_to_none=True)
        loss.backward()
        if (step + 1) % 10 == 0:
            run.log(event="layers", step=step + 1, g=group_norms(groups))
        opt.step()
        step += 1; acc += loss.item(); hit += (logits.argmax(-1) == yb).sum().item(); n += 1; seen += len(ix)
        if step % 10 == 0 or step == total:
            dt = max(1e-6, time.time() - t_last)
            run.log(step=step, loss=acc / n, acc=hit / (n * cfg["batch"]), lr=lr, rate=seen / dt, unit="pictures/s",
                    t=time.time() - run.t0)
            acc, hit, n, t_last, seen = 0.0, 0, 0, time.time(), 0
        if step % every == 0 or step == total:
            vl, va = check()
            run.log(step=step, val_loss=vl, val_acc=va, t=time.time() - run.t0)
            save()
        if run.stop_asked():
            save()
            return "stopped", f"Stopped at step {step} - Resume continues from here."
    return "done", ""


# ==============================================================================
# Trace: one real pass through a trained checkpoint, for the network view
# ==============================================================================
# Each column is a layer; its nodes are the units that fired hardest on this input, with their real values.
# Each wire is a real contribution: activation of the unit before × the weight path to the unit after (the direct
# path through the residual stream / the summed 3x3 kernel; layer norms' gains included, their scaling not).
K = 6


def _top(vec, k=K):
    idx = vec.abs().topk(min(k, vec.numel())).indices.tolist()
    return idx, [round(float(vec[i]), 4) for i in idx]


def trace_gpt(job):
    ckpt = torch.load(os.path.join(job["run"], "ckpt.pt"), map_location="cpu", weights_only=False)
    cfg, chars = ckpt["cfg"], ckpt["chars"]
    model = GPT(cfg["vocab"], cfg["n_layer"], cfg["n_head"], cfg["n_embd"], cfg["block"], **model_cfg(cfg))
    model.load_state_dict(ckpt["model"]); model.eval()
    stoi = {c: i for i, c in enumerate(chars)}
    prompt = (job.get("prompt") or "\n")[-cfg["block"]:]
    idx = torch.tensor([[stoi.get(c, 0) for c in prompt]])
    hidden, att = {}, {}
    hooks = []
    for bi, b in enumerate(model.blocks):
        hooks.append(b.mlp[1].register_forward_hook(lambda m, i, o, bi=bi: hidden.__setitem__(bi, o[0, -1].detach())))

        def qkv_hook(m, i, o, bi=bi, b=b):
            C = o.shape[-1] // 3
            q, k, _ = o[0].split(C, dim=-1)
            hd = C // b.n_head
            q = q[-1].view(b.n_head, hd); k = k.view(-1, b.n_head, hd).transpose(0, 1)
            att[bi] = torch.softmax((k @ q.unsqueeze(-1)).squeeze(-1) / hd ** 0.5, dim=-1).mean(0).detach()   # last position → every position, heads averaged
        hooks.append(b.qkv.register_forward_hook(qkv_hook))
    with torch.no_grad():
        logits, _ = model(idx)
    for h in hooks:
        h.remove()
    probs = logits[0, -1].softmax(-1)
    T = len(prompt)
    shown = list(range(max(0, T - 8), T))                  # the last 8 characters are the input column
    cols = [{"id": "embed", "label": "Input", "nodes": [{"name": repr(prompt[p])[1:-1] or "·", "value": round(float(att[0][p]), 4) if 0 in att else 0} for p in shown], "edges": None}]
    prev_idx, prev_vals, prev_out = None, None, None
    for bi, b in enumerate(model.blocks):
        a = hidden[bi]
        ids, vals = _top(a)
        w_in = b.mlp[0].weight * b.ln2.weight                # (mlp*d) x d
        if prev_idx is None:                                 # from the characters: how much this block's attention reads each
            edges = [[round(float(att[bi][p]) * (1 if vals[j] >= 0 else -1), 4) for p in shown] for j in range(len(ids))]
        else:
            V = w_in[ids] @ prev_out[:, prev_idx]            # direct path: earlier neuron → residual → this neuron
            edges = [[round(float(V[j, i]) * prev_vals[i], 4) for i in range(len(prev_idx))] for j in range(len(ids))]
        heads_look = [{"char": repr(prompt[p])[1:-1] or "·", "pos": p, "w": round(float(att[bi][p]), 3)} for p in att[bi].topk(min(3, T)).indices.tolist()]
        cols.append({"id": f"block{bi}", "label": f"Block {bi + 1}", "nodes": [{"name": f"n{i}", "value": v} for i, v in zip(ids, vals)],
                     "edges": edges, "looks": heads_look, "units": a.numel(), "active": round(float((a.abs() > 0.1).float().mean()), 3)})
        prev_idx, prev_vals, prev_out = ids, vals, b.mlp[2].weight
    top = probs.topk(min(K, probs.numel()))
    out_ids = top.indices.tolist()
    V = (model.head.weight[out_ids] * model.ln.weight) @ prev_out[:, prev_idx]
    cols.append({"id": "head", "label": "Next char", "nodes": [{"name": repr(chars[c])[1:-1] or "·", "value": round(float(p), 4)} for c, p in zip(out_ids, top.values.tolist())],
                 "edges": [[round(float(V[j, i]) * prev_vals[i], 4) for i in range(len(prev_idx))] for j in range(len(out_ids))]})
    print(json.dumps({"ok": True, "kind": "gpt", "prompt": prompt, "cols": cols, "step": ckpt["step"]}, ensure_ascii=False))


def trace_cnn(job):
    from PIL import Image
    import numpy as np
    ckpt = torch.load(os.path.join(job["run"], "ckpt.pt"), map_location="cpu", weights_only=False)
    cfg, classes = ckpt["cfg"], ckpt["classes"]
    model = CNN(len(classes), cfg["width"], cfg.get("stages"), cfg.get("dropout", 0.3))
    model.load_state_dict(ckpt["model"]); model.eval()
    im = Image.open(job["picture"]).convert("RGB").resize((cfg["px"], cfg["px"]), Image.BILINEAR)
    x = torch.from_numpy(np.asarray(im, dtype=np.uint8)).permute(2, 0, 1).float().unsqueeze(0) / 255
    x = (x - ckpt["mean"]) / ckpt["std"]
    convs = [m for m in model.body if isinstance(m, nn.Sequential)]
    outs = {}
    hooks = [c.register_forward_hook(lambda m, i, o, ci=ci: outs.__setitem__(ci, o[0].mean(dim=(1, 2)).detach())) for ci, c in enumerate(convs)]
    with torch.no_grad():
        probs = model(x)[0].softmax(-1)
    for h in hooks:
        h.remove()
    stage_of = model.stage_of
    first = {s: next(ci for ci, t in enumerate(stage_of) if t == s) for s in set(stage_of)}
    last = {s: max(ci for ci, t in enumerate(stage_of) if t == s) for s in set(stage_of)}
    rgb = x[0].mean(dim=(1, 2))
    cols = [{"id": "in", "label": "Picture", "nodes": [{"name": c, "value": round(float(v), 3)} for c, v in zip("RGB", rgb)], "edges": None}]
    prev_idx, prev_vals = [0, 1, 2], [float(v) for v in rgb]
    for s in sorted(set(stage_of)):
        a = outs[last[s]]
        ids, vals = _top(a)
        M = None                                             # the stage as one linear map: its convs' summed kernels × batch norm's scale, chained
        for ci in range(first[s], last[s] + 1):
            conv, bn = convs[ci][0], convs[ci][1]
            Wc = conv.weight.sum(dim=(2, 3)) * (bn.weight / (bn.running_var + bn.eps).sqrt()).unsqueeze(1)
            M = Wc if M is None else Wc @ M
        edges = [[round(float(M[j, i]) * pv, 4) for i, pv in zip(prev_idx, prev_vals)] for j in ids]
        cols.append({"id": f"stage{s}", "label": f"Stage {s + 1}", "nodes": [{"name": f"f{i}", "value": v} for i, v in zip(ids, vals)], "edges": edges,
                     "units": a.numel(), "active": round(float((a > 0.05).float().mean()), 3)})
        prev_idx, prev_vals = ids, vals
    head = model.body[-1].weight
    order = probs.argsort(descending=True).tolist()[:K]
    cols.append({"id": "head", "label": "Kind", "nodes": [{"name": classes[c], "value": round(float(probs[c]), 4)} for c in order],
                 "edges": [[round(float(head[c, i]) * pv, 4) for i, pv in zip(prev_idx, prev_vals)] for c in order]})
    print(json.dumps({"ok": True, "kind": "cnn", "picture": os.path.basename(job["picture"]), "cols": cols, "step": ckpt["step"]}, ensure_ascii=False))


def main():
    with open(sys.argv[1], encoding="utf-8") as f:
        job = json.load(f)
    if job.get("mode") == "trace":
        try:
            (trace_gpt if job["template"] == "gpt" else trace_cnn)(job)
        except Exception as e:                                # noqa: BLE001 - the app shows it as it is
            print(json.dumps({"ok": False, "message": f"{type(e).__name__}: {e}"[:500]}), flush=True)
        return
    if job.get("mode") in ("generate", "generate_lora", "port"):
        if job["mode"] == "generate":
            generate(job)
            return
        import labs_finetune
        try:
            (labs_finetune.generate if job["mode"] == "generate_lora" else labs_finetune.port)(job)
        except Exception as e:                                # noqa: BLE001 - the app shows it as it is
            print(json.dumps({"ok": False, "message": f"{type(e).__name__}: {e}"[:500]}), flush=True)
        return
    run = Run(job["run"])
    seed = int(job.get("settings", {}).get("seed") or 0)
    torch.manual_seed(seed + int(time.time()) % 100000 if job.get("resume") else seed)
    random.seed(seed)
    dev = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    if dev.type == "cuda":
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        # Not cudnn.benchmark: its search cost the first step 17 s on the classifier (RTX 3060 Ti) and the
        # small convs were no faster after it (0.23 s vs 0.11 s for 19 steps).
        torch.backends.cudnn.benchmark = False
    try:
        if job["template"] == "lora":
            import labs_finetune
            state, message = labs_finetune.train_lora(job, run, dev)
        else:
            state, message = (train_gpt if job["template"] == "gpt" else train_cnn)(job, run, dev)
    except torch.cuda.OutOfMemoryError:
        state, message = "failed", "It ran out of GPU memory - pick a smaller size, or close a game or video."
    except Exception as e:                                    # noqa: BLE001 - the run shows it as it is
        state, message = "failed", f"{type(e).__name__}: {e}"[:500]
    run.finish(state, message)


if __name__ == "__main__":
    main()
