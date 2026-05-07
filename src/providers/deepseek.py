"""DeepSeek tool-calling provider — V4 Pro / V4 Flash.

Self-contained implementation of ``ToolCallingProvider`` for the
DeepSeek API (https://api-docs.deepseek.com/).  Targets V4 Pro with
thinking mode by default; works with V4 Flash by passing
``model="deepseek-v4-flash"``.

The DeepSeek API is OpenAI-compatible, so transport is via the
``openai`` Python SDK with a custom base URL.  Provider-specific
concerns encapsulated here:

- **Thinking mode** — DeepSeek V4 Pro returns a separate
  ``reasoning_content`` field on each assistant message containing
  the chain-of-thought.  Enabled via ``reasoning_effort="high"``
  (or ``"max"``) plus ``extra_body={"thinking": {"type": "enabled"}}``.
- **`reasoning_content` round-trip is REQUIRED** — when the
  conversation contains tool calls, you MUST pass `reasoning_content`
  back on subsequent requests or the API returns a 400.  We carry
  it through opaque ``ModelTurn.provider_data["reasoning_content"]``
  and re-attach it to the assistant message in our message
  translator.
- **Pricing as of May 2026** (75% discount until 2026-05-31
  15:59 UTC, regular prices in parens):
    - V4 Pro:   input $0.435/MT ($1.74),  output $0.87/MT ($3.48),
                cache hit $0.003625/MT ($0.0145)
    - V4 Flash: input $0.14/MT,  output $0.28/MT,
                cache hit $0.0028/MT
  Cache hits are server-side automatic (prefix-matched); no
  client-side cache_control opt-in is needed (unlike Anthropic).
- **1M context** on both models.

Inherits transport plumbing from ``OpenAIToolProvider`` to keep the
SDK-version + auth concerns in one place; only the request shape
+ response parsing + message translation differ.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .base import (
    Message,
    ModelTurn,
    ToolCall,
    ToolSchema,
)
from .openai import OpenAIToolProvider, _to_openai_tools


def _to_deepseek_messages(
    system: str,
    messages: list[Message],
) -> list[dict[str, Any]]:
    """OpenAI-compat translator that ALSO carries ``reasoning_content``
    forward on assistant messages (read from opaque provider_data).

    Skipping this on tool-calling conversations causes DeepSeek to
    return a 400.  Same wire shape as OpenAI otherwise.
    """
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})
    for m in messages:
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
            continue
        if role == "assistant":
            msg: dict[str, Any] = {"role": "assistant"}
            text = m.get("text", "") or None
            msg["content"] = text
            tcs = []
            for tc in m.get("tool_calls", []) or []:
                tcs.append({
                    "id": tc["id"],
                    "type": "function",
                    "function": {
                        "name": tc["name"],
                        "arguments": json.dumps(tc.get("arguments", {})),
                    },
                })
            if tcs:
                msg["tool_calls"] = tcs
            # Carry reasoning_content forward — required by DeepSeek
            # when the conversation contains tool calls.
            pdata = m.get("provider_data") or {}
            rc = pdata.get("reasoning_content")
            if rc:
                msg["reasoning_content"] = rc
            out.append(msg)
            continue
        if role == "tool":
            out.append({
                "role": "tool",
                "tool_call_id": m["tool_call_id"],
                "content": m["content"],
            })
            continue
    return out


def _extract_usage(usage_obj: Any) -> dict[str, Any]:
    """Pull DeepSeek-shape usage fields plus map to canonical names."""
    if usage_obj is None:
        return {}
    out: dict[str, Any] = {}
    for attr in ("prompt_tokens", "completion_tokens", "total_tokens",
                 "prompt_cache_hit_tokens", "prompt_cache_miss_tokens",
                 "reasoning_tokens"):
        v = getattr(usage_obj, attr, None)
        if v is not None:
            out[attr] = v
    # Canonical aliases for cross-provider parity in the per-puzzle log
    if "prompt_tokens" in out:
        out["input_tokens"] = out["prompt_tokens"]
    if "completion_tokens" in out:
        out["output_tokens"] = out["completion_tokens"]
    if "prompt_cache_hit_tokens" in out:
        out["cache_read_input_tokens"] = out["prompt_cache_hit_tokens"]
    return out


class DeepSeekToolProvider(OpenAIToolProvider):
    """DeepSeek V4 Pro / V4 Flash tool-calling provider.

    Constructor parameters:

    - ``model``: ``"deepseek-v4-pro"`` (default) or ``"deepseek-v4-flash"``.
    - ``api_key``: API key, or read from ``DEEPSEEK_API_KEY``.
    - ``base_url``: defaults to ``https://api.deepseek.com``; override
      to point at a proxy or self-hosted endpoint.
    - ``max_tokens``: per-turn output cap (default 64000 — DeepSeek's
      models have 1M context but a more modest output cap; tune up
      if your provider tier allows more).
    - ``temperature``: sampling temperature.
    - ``reasoning_effort``: ``"high"`` (default) or ``"max"``.  Lower
      values are auto-mapped to ``"high"`` by DeepSeek for
      compatibility.  ``None`` disables thinking mode entirely.
    - ``thinking_enabled``: explicitly turn the ``thinking`` extra
      body parameter on (default True).  Required for V4 Pro to
      think; harmless on V4 Flash.
    """

    name = "deepseek"

    def __init__(
        self,
        model: str = "deepseek-v4-pro",
        api_key: str | None = None,
        base_url: str = "https://api.deepseek.com",
        max_tokens: int = 64000,
        temperature: float = 0.0,
        reasoning_effort: str | None = "high",
        thinking_enabled: bool = True,
    ):
        import os
        # Try DEEPSEEK_API_KEY first, fall back to OPENAI_API_KEY for
        # callers who pass via --api-key directly.
        key = api_key or os.environ.get("DEEPSEEK_API_KEY")
        if not key:
            raise RuntimeError(
                "DEEPSEEK_API_KEY not set; pass api_key=... or set the env var."
            )
        # Use OpenAIToolProvider's __init__ for the SDK plumbing.  It
        # opens the openai.OpenAI client with our base_url + key.
        super().__init__(
            model=model, api_key=key, base_url=base_url,
            max_tokens=max_tokens, temperature=temperature,
        )
        self.reasoning_effort = (
            reasoning_effort if reasoning_effort and reasoning_effort.lower() != "off"
            else None
        )
        self.thinking_enabled = thinking_enabled

    def provider_config_for_log(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "reasoning_effort": self.reasoning_effort,
            "thinking_enabled": self.thinking_enabled,
        }

    def respond(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSchema],
    ) -> ModelTurn:
        ds_msgs = _to_deepseek_messages(system, messages)
        ds_tools = _to_openai_tools(tools)

        create_kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            tools=ds_tools,
            messages=ds_msgs,
        )
        if self.reasoning_effort:
            create_kwargs["reasoning_effort"] = self.reasoning_effort
        extra_body: dict[str, Any] = {}
        if self.thinking_enabled:
            extra_body["thinking"] = {"type": "enabled"}
        if extra_body:
            create_kwargs["extra_body"] = extra_body

        t_start = time.time()
        resp = self._client.chat.completions.create(**create_kwargs)
        wallclock = time.time() - t_start

        choice = resp.choices[0]
        msg = choice.message
        text = msg.content or ""
        # reasoning_content lives on the SDK message as a non-standard
        # attribute (pydantic model_dump exposes it; getattr is safe).
        reasoning_content = getattr(msg, "reasoning_content", None) or ""
        tool_calls: list[ToolCall] = []
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(ToolCall(
                id=tc.id,
                name=tc.function.name,
                arguments=args if isinstance(args, dict) else {},
            ))

        from .openai import _safe_model_dump
        return ModelTurn(
            text=text,
            tool_calls=tool_calls,
            stop_reason=choice.finish_reason or "",
            raw=resp,
            raw_dict=_safe_model_dump(resp),
            usage=_extract_usage(getattr(resp, "usage", None)),
            wallclock_seconds=wallclock,
            response_headers={},  # OpenAI SDK doesn't expose easily; skip
            # Round-trip reasoning_content through provider_data — read
            # back by _to_deepseek_messages on the next call.  Required
            # to avoid 400s on tool-calling conversations.
            provider_data={"reasoning_content": reasoning_content},
        )
