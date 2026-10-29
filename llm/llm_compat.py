"""Compatibility wrapper around LiteLLM with a direct local fallback.

Some LiteLLM installs expose ``completion`` at ``litellm.main`` but not at the
package root.  Local llama.cpp traffic should not depend on LiteLLM at all, so
an OpenAI-compatible client is used when the LiteLLM entry point is missing.
"""
from __future__ import annotations

import importlib


def _litellm_completion():
    try:
        module = importlib.import_module("litellm")
        module.telemetry = False
        module.drop_params = True
        fn = getattr(module, "completion", None)
        if callable(fn):
            return fn
    except Exception:
        pass
    try:
        fn = getattr(importlib.import_module("litellm.main"), "completion", None)
        return fn if callable(fn) else None
    except Exception:
        return None


def _is_local(kwargs: dict) -> bool:
    base = str(kwargs.get("api_base") or kwargs.get("base_url") or "")
    return bool(base) and any(host in base for host in ("127.0.0.1", "localhost", "::1"))


def _direct_openai(kwargs: dict):
    from openai import OpenAI

    payload = dict(kwargs)
    base_url = payload.pop("api_base", None) or payload.pop("base_url", None)
    api_key = payload.pop("api_key", None) or "sk-local-key"
    payload.pop("timeout", None)
    model = str(payload.get("model") or "")
    if model.startswith("openai/"):
        payload["model"] = model[len("openai/"):]
    client = OpenAI(base_url=base_url, api_key=api_key, timeout=kwargs.get("timeout", 300))
    return client.chat.completions.create(**payload)


def completion(**kwargs):
    fn = _litellm_completion()
    if callable(fn):
        if _is_local(kwargs):
            return fn(**kwargs)
        # A cloud model: through the network layer - its "models" route (litellm gets it as proxy
        # variables), the kill switch and the connection log; the guard blocks a direct connection.
        import netlayer
        with netlayer.model_call(str(kwargs.get("model") or "")):
            return fn(**kwargs)
    if _is_local(kwargs):
        return _direct_openai(kwargs)
    raise RuntimeError(
        "LiteLLM is installed without a usable completion() API. Run run.bat "
        "to repair the environment, or reinstall from requirements.txt."
    )

