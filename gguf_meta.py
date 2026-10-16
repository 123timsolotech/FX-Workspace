"""
gguf_meta.py
============
Reads the metadata header of a GGUF model file (llama.cpp's format) - the
architecture, layer and head counts, head sizes and trained context - without
loading the model or any library. diagnostics.py uses it to work out how much
VRAM a model needs at a given context size (weights + KV cache).

  read(path) -> {"arch", "layers", "heads", "kv_heads" (int or per-layer list),
                 "key_len", "value_len", "embed", "trained_ctx", "sliding_window",
                 "full_attention_interval", "expert_count", "raw": {key: value}}
Large arrays (the tokenizer's vocabulary) are skipped, not read.
"""
from __future__ import annotations

import struct

_SCALARS = {0: "<B", 1: "<b", 2: "<H", 3: "<h", 4: "<I", 5: "<i", 6: "<f", 7: "<?", 10: "<Q", 11: "<q", 12: "<d"}
_MAX_KEEP = 4096             # array items kept (per-layer lists are small; vocabularies are not)


class GGUFError(Exception):
    pass


def _read(f, fmt):
    size = struct.calcsize(fmt)
    data = f.read(size)
    if len(data) != size:
        raise GGUFError("The file ends inside its header.")
    return struct.unpack(fmt, data)[0]


def _string(f) -> str:
    n = _read(f, "<Q")
    if n > 1 << 24:
        raise GGUFError("A string in the header is implausibly long.")
    return f.read(n).decode("utf-8", errors="replace")


def _skip_strings(f, count: int) -> None:
    """Step over `count` length-prefixed strings (a 250,000-word vocabulary) through 1 MB reads: one small read and a
    seek per word was ~0.2 s a model, all of it holding the server while it started."""
    chunk = 1 << 20
    buf, pos = f.read(chunk), 0
    for _ in range(count):
        if pos + 8 > len(buf):
            buf, pos = buf[pos:] + f.read(chunk), 0
            if len(buf) < 8:
                raise GGUFError("The file ends inside its header.")
        n = struct.unpack_from("<Q", buf, pos)[0]
        if n > 1 << 24:
            raise GGUFError("A string in the header is implausibly long.")
        pos += 8 + n
        if pos > len(buf):                               # the string runs past what is buffered
            f.seek(pos - len(buf), 1)
            buf, pos = f.read(chunk), 0
    f.seek(pos - len(buf), 1)                            # back to just after the last string


def _value(f, vtype):
    if vtype in _SCALARS:
        return _read(f, _SCALARS[vtype])
    if vtype == 8:
        return _string(f)
    if vtype == 9:
        itype, count = _read(f, "<I"), _read(f, "<Q")
        if itype == 8 and count > _MAX_KEEP:             # a vocabulary: skipped, not kept
            _skip_strings(f, count)
            return None
        if itype == 8:                                   # strings: skip what isn't kept
            out = []
            for i in range(count):
                n = _read(f, "<Q")
                if i < _MAX_KEEP and count <= _MAX_KEEP:
                    out.append(f.read(n).decode("utf-8", errors="replace"))
                else:
                    f.seek(n, 1)
            return out if count <= _MAX_KEEP else None
        if itype in _SCALARS:
            size = struct.calcsize(_SCALARS[itype])
            if count > _MAX_KEEP:
                f.seek(size * count, 1)
                return None
            return [_read(f, _SCALARS[itype]) for _ in range(count)]
        raise GGUFError(f"Nested arrays (type {itype}) are not supported.")
    raise GGUFError(f"Unknown value type {vtype}.")


def read(path: str) -> dict:
    with open(path, "rb") as f:
        if f.read(4) != b"GGUF":
            raise GGUFError("Not a GGUF file.")
        version = _read(f, "<I")
        if version < 2:
            raise GGUFError(f"GGUF version {version} is too old to read.")
        _read(f, "<Q")                                   # tensor count
        kv_count = _read(f, "<Q")
        raw = {}
        for _ in range(kv_count):
            key = _string(f)
            raw[key] = _value(f, _read(f, "<I"))
    arch = str(raw.get("general.architecture") or "")
    g = lambda name: raw.get(f"{arch}.{name}")
    heads = g("attention.head_count")
    embed = g("embedding_length")
    key_len = g("attention.key_length") or (embed // heads if isinstance(heads, int) and heads and embed else None)
    return {"arch": arch, "layers": g("block_count"), "heads": heads, "kv_heads": g("attention.head_count_kv") or heads,
            "key_len": key_len, "value_len": g("attention.value_length") or key_len, "embed": embed,
            "trained_ctx": g("context_length"), "sliding_window": g("attention.sliding_window"),
            "full_attention_interval": g("full_attention_interval"), "expert_count": g("expert_count"),
            "name": raw.get("general.name"), "raw": raw}
