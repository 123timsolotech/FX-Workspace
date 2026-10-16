"""
labs_finetune.py - Fx Labs' fine-tuning, in Labs' own Python (labs_worker.py loads it for "lora" runs).

QLoRA on a Qwen 3.5 chat model: the base is loaded in 4 bits (bitsandbytes NF4), small trainable LoRA matrices are
added to every linear layer of the language model (PEFT), and only those learn - from chat examples, on the
assistant's answers only. The base is downloaded once to Labs' bases\\ folder (Hugging Face, through the app's route).

Measured on this PC (RTX 3060 Ti 8 GB, Qwen3.5-0.8B): without the flash-linear-attention kernels a training step
took minutes (the Gated DeltaNet layers fell back to reference PyTorch); with them (triton-windows) 1.8 s for
2 x 512 tokens. Labs' Setup installs them.

Port (mode "port"): the adapter is merged into the base in bf16 on the CPU, converted with llama.cpp's own
convert_hf_to_gguf.py (the version matching the app's llama-server) and quantized with its llama-quantize - the
result loads in Workspace like any other model. Proven end to end here: a merged 0.8B became a 774 MB Q8_0 GGUF
that llama-server b11541 ran.
"""
import json
import math
import os
import random
import re
import subprocess
import sys
import time

import torch

MAX_NEW = 512
# Which part of a Qwen 3.5 layer a linear belongs to (the network view's toggles): full attention, the Gated
# DeltaNet's projections, its two small gates, the MLP.
KINDS = {"q_proj": "attn", "k_proj": "attn", "v_proj": "attn", "o_proj": "attn",
         "in_proj_qkv": "delta", "in_proj_z": "delta", "out_proj": "delta",
         "in_proj_a": "gates", "in_proj_b": "gates",
         "gate_proj": "mlp", "up_proj": "mlp", "down_proj": "mlp"}
_LAYER = re.compile(r"\.layers\.(\d+)\.")


def part_of(name):
    """(layer, kind) of a module name, or None - for the LoRA targets and the per-layer heat."""
    m = _LAYER.search(name)
    kind = KINDS.get(name.rsplit(".", 1)[-1]) or KINDS.get(name.split(".lora_")[0].rsplit(".", 1)[-1])
    return (int(m.group(1)), kind) if m and kind and "visual" not in name else None


def lora_targets(model, targets, layers):
    """Exact module names to adapt: the kinds switched on, in the layers chosen (None = all) - a list, so PEFT never
    matches anything else and the trainable count equals what the network view showed."""
    import bitsandbytes as bnb
    keep = {k for k, on in (targets or {}).items() if on}
    sel = set(layers) if layers is not None else None
    out = []
    for name, mod in model.named_modules():
        if isinstance(mod, (bnb.nn.Linear4bit, torch.nn.Linear)):
            p = part_of(name)
            if p and p[1] in keep and (sel is None or p[0] in sel):
                out.append(name)
    return out


def layer_heat(model, which="grad"):
    """{"<layer>.<kind>": norm} over the LoRA B matrices: their gradients (how hard each part learns this step) or
    their weights (how much it has learned so far). B starts at zero, so it carries the signal from the first step."""
    items = [(part_of(n), p.grad if which == "grad" else p.detach()) for n, p in model.named_parameters() if "lora_B" in n]
    items = [(pt, t) for pt, t in items if pt and t is not None]
    if not items:
        return {}
    sq = torch.stack(torch._foreach_norm([t for _, t in items])).float().pow(2).tolist()
    out = {}
    for (pt, _), v in zip(items, sq):
        key = f"{pt[0]}.{pt[1]}"
        out[key] = out.get(key, 0.0) + v
    return {k: round(v ** 0.5, 6) for k, v in out.items()}


def say(**kw):
    """A line the app reads (stage, progress, the result)."""
    print(json.dumps(kw, ensure_ascii=False), flush=True)


def ensure_base(job, log=None):
    """The base model's files in job["base_dir"] - downloaded from job["repo"] the first time."""
    folder = job["base_dir"]
    if os.path.exists(os.path.join(folder, "config.json")) and any(f.endswith(".safetensors") for f in os.listdir(folder)):
        return folder
    from huggingface_hub import snapshot_download
    if log:
        log(event="stage", text=f"Downloading {job.get('base_label') or job['repo']} (once)")
    snapshot_download(job["repo"], local_dir=folder, allow_patterns=["*.json", "*.safetensors", "*.txt", "*.jinja", "*.model"])
    return folder


def load_examples(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                ex = json.loads(line)
                if ex.get("messages") and ex["messages"][-1].get("role") == "assistant":
                    out.append(ex["messages"])
    return out


def encode(tok, messages, max_len):
    """Token ids and labels: only the last answer is learned (everything before it is -100)."""
    kw = {"tokenize": False, "enable_thinking": False}
    prompt = tok.apply_chat_template(messages[:-1], add_generation_prompt=True, **kw)
    full = tok.apply_chat_template(messages, **kw)
    p_ids = tok(prompt, add_special_tokens=False).input_ids
    f_ids = tok(full, add_special_tokens=False).input_ids
    if f_ids[:len(p_ids)] != p_ids:                     # a template that rewrites earlier turns: learn the whole
        p_ids = []
    f_ids = f_ids[:max_len]
    labels = [-100] * min(len(p_ids), len(f_ids)) + f_ids[len(p_ids):]
    return f_ids, labels[:len(f_ids)]


def answer_loss(model, ids, labels, dev):
    """Cross-entropy on the answer's tokens only. The output layer has ~248k words: scoring all 1024 positions of an
    example costs ~1 GB (plus its gradient) and pushed an 8 GB card into shared memory; the answer is a few hundred."""
    inner = model.get_base_model() if hasattr(model, "get_base_model") else model
    x = torch.tensor([ids], device=dev)
    hidden = inner.model(input_ids=x).last_hidden_state[0]           # (T, H)
    y = torch.tensor(labels[1:], device=dev)
    keep = (y != -100).nonzero(as_tuple=True)[0]
    if not len(keep):
        return None
    logits = inner.lm_head(hidden[:-1][keep]).float()
    return torch.nn.functional.cross_entropy(logits, y[keep])


def load_model(base, adapter=None, trainable=False):
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig
    from peft import PeftModel
    tok = AutoTokenizer.from_pretrained(base)
    bnb = BitsAndBytesConfig(load_in_4bit=True, bnb_4bit_quant_type="nf4", bnb_4bit_compute_dtype=torch.bfloat16,
                             bnb_4bit_use_double_quant=True)
    model = AutoModelForCausalLM.from_pretrained(base, quantization_config=bnb, dtype=torch.bfloat16, device_map={"": 0})
    if adapter:
        model = PeftModel.from_pretrained(model, adapter, is_trainable=trainable)
    return tok, model


@torch.no_grad()
def answer(tok, model, messages, max_new=MAX_NEW, temperature=0.0):
    model.eval()
    prompt = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, enable_thinking=False)
    ids = tok(prompt, return_tensors="pt", add_special_tokens=False).to(model.device)
    kw = {"do_sample": True, "temperature": temperature, "top_p": 0.9} if temperature > 0 else {"do_sample": False}
    # Stop at the end of the turn (<|im_end|>), not only at the end-of-text token - else it runs on into a new turn.
    stops = [t for t in {tok.eos_token_id, tok.convert_tokens_to_ids("<|im_end|>")} if isinstance(t, int) and t >= 0]
    out = model.generate(**ids, max_new_tokens=max_new, pad_token_id=tok.eos_token_id, eos_token_id=stops, **kw)
    return tok.decode(out[0][ids.input_ids.shape[1]:], skip_special_tokens=True).strip()


def train_lora(job, run, dev):
    from peft import LoraConfig, get_peft_model
    if dev.type != "cuda":
        return "failed", "Fine-tuning needs the GPU (PyTorch can't see it)."
    s = job.get("settings") or {}
    lr, accum = float(s.get("lr") or 2e-4), int(s.get("batch") or 8)
    max_len, rank = int(s.get("max_len") or 1024), int(s.get("rank") or 16)
    examples = load_examples(job["data"])
    if len(examples) < 2:
        return "failed", "The dataset needs at least 2 chat examples."
    rnd = random.Random(int(s.get("seed") or 0))
    rnd.shuffle(examples)
    n_val = max(1, min(30, len(examples) // 10)) if len(examples) >= 10 else 1
    val, train = examples[:n_val], examples[n_val:] or examples
    base = ensure_base(job, run.log)
    run.log(event="stage", text="Loading the model in 4 bits")
    adapter_dir = os.path.join(run.folder, "adapter")
    ckpt_path = os.path.join(run.folder, "ckpt.pt")
    resume = job.get("resume") and os.path.exists(os.path.join(adapter_dir, "adapter_config.json"))
    tok, model = load_model(base, adapter_dir if resume else None, trainable=True)
    # Gradient checkpointing without prepare_model_for_kbit_training: that turns the embeddings and the output layer
    # (248k words x hidden) to fp32 - most of the memory on these models.
    model.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
    model.enable_input_require_grads()
    model.config.use_cache = False
    if not resume:
        names = lora_targets(model, s.get("targets") or {"attn": 1, "delta": 1, "mlp": 1, "gates": 1}, s.get("layers"))
        if not names:
            return "failed", "No part of the network was chosen to fine-tune."
        model = get_peft_model(model, LoraConfig(r=rank, lora_alpha=int(s.get("alpha_mult") or 2) * rank, lora_dropout=0.05,
                                                 target_modules=names, task_type="CAUSAL_LM"))
    params = [p for p in model.parameters() if p.requires_grad]
    opt = torch.optim.AdamW(params, lr=lr, weight_decay=0.0)
    step = 0
    if resume and os.path.exists(ckpt_path):
        ck = torch.load(ckpt_path, map_location="cpu", weights_only=False)
        opt.load_state_dict(ck["opt"]); step = ck["step"]
    trainable = sum(p.numel() for p in params)
    run.log(event="start", step=step, params=sum(p.numel() for p in model.parameters()), trainable=trainable,
            train=len(train), val=len(val), base=job.get("base_label"), device="cuda")
    enc_train = [encode(tok, m, max_len) for m in train]
    enc_val = [encode(tok, m, max_len) for m in val]
    base_answers_path = os.path.join(run.folder, "base_answers.json")

    def save():
        model.save_pretrained(adapter_dir)
        torch.save({"opt": opt.state_dict(), "step": step}, ckpt_path + ".tmp")
        os.replace(ckpt_path + ".tmp", ckpt_path)

    def check():
        model.eval()
        with torch.no_grad():
            tot, n = 0.0, 0
            for ids, labels in enc_val:
                loss = answer_loss(model, ids, labels, dev)
                if loss is not None:
                    tot += loss.item(); n += 1
        # Up to 6 held-out questions, spread over the whole held-out set (the same ones every check).
        shown = [val[round(i * (len(val) - 1) / 5)] for i in range(6)] if len(val) > 6 else val
        run.log(event="learned", step=step, g=layer_heat(model, "weight"))
        # The original model's answers once (adapter off) - the yardstick every later check is shown against.
        try:
            with open(base_answers_path, encoding="utf-8") as f:
                base_ans = json.load(f)
        except (OSError, ValueError):
            with model.disable_adapter():
                base_ans = [answer(tok, model, m[:-1], 256) for m in shown]
            with open(base_answers_path, "w", encoding="utf-8") as f:
                json.dump(base_ans, f, ensure_ascii=False)
        tuned = [answer(tok, model, m[:-1], 256) for m in shown]
        model.train()
        run.sample(step=step, pairs=[{"prompt": m[-2]["content"] if len(m) > 1 else "", "expected": m[-1]["content"],
                                      "base": b, "tuned": t} for m, b, t in zip(shown, base_ans, tuned)])
        return tot / max(1, n)

    total = int(job["steps"])
    warmup = min(20, max(1, total // 10))
    every = max(10, total // 10)
    model.train()
    if step == 0:
        run.log(step=0, val_loss=check(), t=time.time() - run.t0)
    while step < total:
        cur = lr * min(1.0, (step + 1) / warmup) * (0.1 + 0.9 * 0.5 * (1 + math.cos(math.pi * min(1.0, step / max(1, total)))))
        for g in opt.param_groups:
            g["lr"] = cur
        t0, loss_sum, toks, done = time.time(), 0.0, 0, 0
        for _ in range(accum):
            ids, labels = enc_train[rnd.randrange(len(enc_train))]
            loss = answer_loss(model, ids, labels, dev)
            if loss is None:
                continue
            (loss / accum).backward()
            loss_sum += loss.item(); toks += len(ids); done += 1
        run.log(event="layers", step=step + 1, g=layer_heat(model, "grad"))
        torch.nn.utils.clip_grad_norm_(params, 1.0)
        opt.step(); opt.zero_grad(set_to_none=True)
        step += 1
        run.log(step=step, loss=loss_sum / max(1, done), lr=cur, rate=toks / max(1e-6, time.time() - t0), unit="tokens/s",
                t=time.time() - run.t0, mem=round(torch.cuda.max_memory_allocated() / 2**30, 2))
        if step % every == 0 or step == total:
            run.log(step=step, val_loss=check(), t=time.time() - run.t0)
            save()
        if run.stop_asked():
            save()
            return "stopped", f"Stopped at step {step} - Resume continues from here."
    return "done", ""


def generate(job):
    """Try it for a fine-tune: the fine-tuned answer, and the original model's beside it."""
    base = ensure_base(job)
    tok, model = load_model(base, os.path.join(job["run"], "adapter"))
    msgs = [{"role": "user", "content": job.get("prompt") or "Hello"}]
    tuned = answer(tok, model, msgs, int(job.get("length") or 300), float(job.get("temperature") or 0.0))
    with model.disable_adapter():
        original = answer(tok, model, msgs, int(job.get("length") or 300), float(job.get("temperature") or 0.0))
    say(ok=True, prompt=job.get("prompt") or "", text=tuned, original=original)


def port(job):
    """Adapter + base -> one model -> GGUF -> quantized GGUF at job["out"]. Prints stage lines, then the result."""
    from transformers import AutoModelForCausalLM, AutoTokenizer
    from peft import PeftModel
    work = job["work"]
    os.makedirs(work, exist_ok=True)
    merged, f16 = os.path.join(work, "merged"), os.path.join(work, "model-bf16.gguf")
    say(stage="Merging the fine-tune into the model")
    model = AutoModelForCausalLM.from_pretrained(ensure_base(job), dtype=torch.bfloat16, device_map={"": "cpu"},
                                                 low_cpu_mem_usage=True)
    model = PeftModel.from_pretrained(model, os.path.join(job["run"], "adapter")).merge_and_unload()
    model.save_pretrained(merged, safe_serialization=True)
    AutoTokenizer.from_pretrained(ensure_base(job)).save_pretrained(merged)
    del model
    say(stage="Converting to GGUF")
    env = {**os.environ, "PYTHONPATH": os.path.join(job["convert_dir"], "gguf-py"), "PYTHONIOENCODING": "utf-8"}
    r = subprocess.run([sys.executable, os.path.join(job["convert_dir"], "convert_hf_to_gguf.py"), merged, "--outfile", f16,
                        "--outtype", "bf16"], capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
    if r.returncode != 0:
        say(ok=False, message="Converting failed: " + ((r.stderr or r.stdout).strip().splitlines() or ["no output"])[-1][:300])
        return
    say(stage=f"Quantizing ({job['quant']})")
    r = subprocess.run([job["quantize"], f16, job["out"], job["quant"]], capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    if r.returncode != 0 or not os.path.exists(job["out"]):
        say(ok=False, message="Quantizing failed: " + ((r.stderr or r.stdout).strip().splitlines() or ["no output"])[-1][:300])
        return
    say(ok=True, path=job["out"], mb=round(os.path.getsize(job["out"]) / 2**20))
