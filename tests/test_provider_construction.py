"""Provider-parity smoke: every shipped provider can be imported, has
the expected LCD attributes, and can be constructed without making
network calls. Skipped per-provider if the upstream SDK is not
installed.
"""

from __future__ import annotations

import importlib

import pytest


def _import_or_skip(module: str):
    try:
        return importlib.import_module(module)
    except ImportError as e:
        pytest.skip(f"{module} not installed ({e})")


def test_base_abc_loads():
    from providers.base import ModelTurn, ToolCall, ToolCallingProvider  # noqa: F401


def test_anthropic_provider_attrs():
    _import_or_skip("anthropic")
    from providers.anthropic import AnthropicToolProvider

    p = AnthropicToolProvider(model="claude-sonnet-4-6", api_key="test-fake-key")
    assert p.name == "anthropic"
    assert p.model == "claude-sonnet-4-6"
    cfg = p.provider_config_for_log()
    assert cfg["name"] == "anthropic"


def test_openai_provider_attrs():
    _import_or_skip("openai")
    from providers.openai import OpenAIToolProvider, VLLMToolProvider

    p = OpenAIToolProvider(model="gpt-test", api_key="test-fake-key")
    assert p.name == "openai"
    assert p.model == "gpt-test"
    v = VLLMToolProvider(
        model="meta-llama/Llama-3.1-8B-Instruct",
        base_url="http://localhost:8000/v1",
        api_key="vllm",
    )
    assert v.model.endswith("Llama-3.1-8B-Instruct")


def test_deepseek_provider_attrs():
    _import_or_skip("openai")  # DeepSeek shares the OpenAI SDK
    from providers.deepseek import DeepSeekToolProvider

    p = DeepSeekToolProvider(model="deepseek-v4-pro", api_key="test-fake-key")
    assert p.name == "deepseek"
    assert p.model == "deepseek-v4-pro"


def test_claude_cli_provider_attrs():
    import shutil

    if not shutil.which("claude"):
        pytest.skip("claude CLI binary not on PATH")
    from providers.claude_cli import ClaudeCLIToolProvider

    p = ClaudeCLIToolProvider(model="claude-opus-4-7")
    assert p.name == "claude-cli"
    assert p.model == "claude-opus-4-7"


def test_provider_factory_dispatch():
    from providers import get_provider

    _import_or_skip("anthropic")
    p = get_provider("anthropic", model="claude-sonnet-4-6", api_key="test-fake-key")
    assert p.name == "anthropic"


def test_unknown_provider_raises():
    from providers import get_provider

    with pytest.raises((KeyError, ValueError, RuntimeError)):
        get_provider("not-a-real-provider", model="x")
