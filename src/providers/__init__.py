"""Pluggable LLM backends for the unified runner (interactive + bulk modes).

Every provider implements ``ToolCallingProvider.respond(...)`` against
the LCD interface defined in ``base.py``.  Adding a new provider is a
matter of (1) writing a single file under this package, (2)
implementing the abstract methods, and (3) re-exporting it from this
``__init__`` + adding a name to ``get_provider``.  No other module
needs to change — neither the Episode loop nor the bulk-mode
completion path knows or cares which backend it's talking to.

Fully interactive mode calls ``provider.respond(system, history, tools)``
per turn.  N-attempts bulk mode calls ``provider.complete(prompt, system)``,
a concrete convenience method on the ABC that wraps ``respond()`` with
one user message and empty tools.  Same provider instance, same SDK
plumbing, same per-provider config (``reasoning_effort``,
``thinking_enabled``, etc.) — bulk and interactive paths just differ
in how they shape the conversation.

To add a new provider, see ``base.py``'s ``ToolCallingProvider``
docstring.  The existing files (``anthropic.py``, ``openai.py``,
``deepseek.py``, ``claude_cli.py``) are reference implementations.
"""

from .base import (
    Message,
    ModelTurn,
    ToolCall,
    ToolCallingProvider,
    ToolSchema,
)

# Provider implementations — each is self-contained and importable
# without forcing its SDK dependency until __init__ runs.
from .anthropic import AnthropicToolProvider
from .claude_cli import ClaudeCLIToolProvider
from .deepseek import DeepSeekToolProvider
from .openai import OpenAIToolProvider, VLLMToolProvider


__all__ = [
    # LCD types
    "Message",
    "ModelTurn",
    "ToolCall",
    "ToolCallingProvider",
    "ToolSchema",
    # Concrete providers
    "AnthropicToolProvider",
    "ClaudeCLIToolProvider",
    "DeepSeekToolProvider",
    "OpenAIToolProvider",
    "VLLMToolProvider",
    "get_provider",
]


def get_provider(name: str, model: str, **kwargs):
    """Instantiate a provider by short name.

    Returns a ``ToolCallingProvider``.  Fully interactive mode callers use
    ``provider.respond(...)``; n-attempts bulk mode callers use
    ``provider.complete(...)``.  Both methods are on the same class.

    Recognised names (case-insensitive aliases in parens):
      anthropic | claude         -> AnthropicToolProvider
      openai    | chatgpt | gpt  -> OpenAIToolProvider
      vllm      | vllm-server    -> VLLMToolProvider
      deepseek                   -> DeepSeekToolProvider
      lmstudio  | lm-studio      -> OpenAIToolProvider with localhost defaults
      claude-cli                 -> ClaudeCLIToolProvider

    Provider-specific kwargs:
      effort                  (anthropic, deepseek)  - reasoning effort
      adaptive_thinking       (anthropic)            - default True
      reasoning_effort        (deepseek)             - alias for effort
      base_url                (openai, vllm, etc.)   - endpoint override
      api_key                 (all)                  - falls back to env
      max_tokens, temperature (all)                  - per-turn caps
    """
    name = name.lower()
    if name in ("anthropic", "claude"):
        return AnthropicToolProvider(model=model, **kwargs)
    if name in ("openai", "chatgpt", "gpt"):
        return OpenAIToolProvider(model=model, **kwargs)
    if name in ("lmstudio", "lm-studio", "local"):
        kwargs.setdefault("base_url", "http://localhost:1234/v1")
        kwargs.setdefault("api_key", "lm-studio")
        return OpenAIToolProvider(model=model, **kwargs)
    if name in ("vllm", "vllm-openai", "vllm-server"):
        return VLLMToolProvider(model=model, **kwargs)
    if name == "deepseek":
        # DeepSeek's thinking-mode arg is named differently from
        # Anthropic's; accept either name and translate.  Mirror
        # runner.py's mapping: low/medium → high, xhigh → max.
        eff = kwargs.pop("reasoning_effort", None)
        if eff is None:
            eff = kwargs.pop("effort", None)
        if eff in ("low", "medium"):
            eff = "high"
        elif eff == "xhigh":
            eff = "max"
        if eff and eff != "off":
            kwargs["reasoning_effort"] = eff
        return DeepSeekToolProvider(model=model, **kwargs)
    if name in ("claude-cli", "claude_cli"):
        kwargs = {k: v for k, v in kwargs.items()
                  if k not in ("api_key", "base_url")}
        return ClaudeCLIToolProvider(model=model, **kwargs)
    raise ValueError(f"unknown provider: {name!r}")
