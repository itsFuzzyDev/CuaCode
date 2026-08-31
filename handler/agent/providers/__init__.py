"""The provider registry. One module per wire dialect -- ollama, openai
chat-completions, anthropic messages -- with the shared plumbing in base.py.

A registry key is the provider you pick in settings; the class behind it is
whichever wire dialect that provider speaks. Everything that used to live in
providers.py is importable straight off this package, unchanged."""

import copy

from handler.agent.providers.base import (
    Delta, CallAssembler, usage_of, attachment_note, append_user_text, _daemon_up)
from handler.agent.providers.ollama import Ollama
from handler.agent.providers.openai import OpenAI
from handler.agent.providers.anthropic import Anthropic

PROVIDERS = {
    "ollama":     Ollama(),
    "openai":     OpenAI("gpt-5.6-sol"),
    "anthropic":  Anthropic(),
    "nvidia":     OpenAI("minimaxai/minimax-m3", "https://integrate.api.nvidia.com/v1", "NVIDIA_API_KEY"),
    "openrouter": OpenAI("poolside/laguna-s-2.1:free", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", vision=False), # if you have money you can use better models.
    "deepseek":   OpenAI("DeepSeek-V4-Flash-Vision-Exp", "https://api.deepseek.com", "DEEPSEEK_API_KEY", vision=True),
    "together":   OpenAI("MiniMaxAI/MiniMax-M3", "https://api.together.xyz/v1", "TOGETHER_API_KEY"),
    "qubrain":    OpenAI("glm-5.2", "https://qubrain.org/v1", "QB_API_KEY")
    # You can install local providers (including ollama local, litellm, all as long as they follow OpenAI schema )
    #- the Ollama class though is set to CLOUD ONLY, if youd like to set ollama local or local models you can use openai schema on the localhost )
}

def get(name: str):
    p = PROVIDERS.get(name)
    if p is None: raise ValueError(f"unknown provider: {name!r} (have {sorted(PROVIDERS)})")
    return p

# Instance state that must not be inherited by a copy, and what it resets to.
# The client is rebuilt rather than shared because it is cheap to rebuild and
# the key it was constructed with may not be this run's.
_FRESH = {"_c": None, "_thinking": [], "_key": "", "_host": ""}

def new(name: str):
    """An instance of the same provider that shares nothing mutable.

    The registry holds one instance per provider and they carry per-turn state
    on themselves -- Anthropic hands its thinking blocks from stream() to
    assistant_message() through self._thinking, and every class caches a
    client. That is fine while one loop runs at a time, and is a data race the
    moment two do: agent A's thinking blocks, signature and all, land in agent
    B's assistant message and the request is rejected for a signature that does
    not match the turn it is attached to.

    Copied rather than reconstructed because the openai class carries its
    identity in constructor arguments -- base_url, key_env, default_model,
    vision -- and there is no registry of what each entry was built with.
    """
    p = copy.copy(get(name))
    for attr, val in _FRESH.items():
        if hasattr(p, attr): setattr(p, attr, list(val) if isinstance(val, list) else val)
    return p