"""OpenAI chat-completions tool-calling provider (+ vLLM alias).

Self-contained implementation of ``ToolCallingProvider`` for any
endpoint that speaks OpenAI's chat-completions API with the ``tools``
parameter — including the official OpenAI API, vLLM, TGI, SGLang,
LM Studio, and others.

Provider-specific concerns encapsulated here:

- Tool definitions translated from the canonical
  ``{name, description, input_schema}`` form to OpenAI's
  ``{type: "function", function: {...}}`` wrapper.
- Tool-call arguments are JSON STRINGS in OpenAI's wire format (not
  dicts); we parse them back into dicts on the way in.
- Assistant messages with ``tool_calls`` use OpenAI's specific shape
  (``content: null`` accepted when ``tool_calls`` are present).
- Tool result messages use ``role: "tool"`` with ``tool_call_id``.

No prompt-caching is implemented here — OpenAI's caching is automatic
and server-side based on prefix hashing, so there's no client-side
opt-in to handle.

This file imports the ``openai`` SDK lazily inside ``__init__`` so the
package itself can be imported in environments where the SDK isn't
installed.
"""

from __future__ import annotations

import json
import time
from typing import Any

from .base import (
    Message,
    ModelTurn,
    ToolCall,
    ToolCallingProvider,
    ToolSchema,
)

# ---------------------------------------------------------------------------
# Translation helpers (OpenAI-specific wire format)
# ---------------------------------------------------------------------------


def _to_openai_tools(tools: list[ToolSchema]) -> list[dict[str, Any]]:
    """Wrap canonical ``{name, description, input_schema}`` tool defs
    in OpenAI's ``{type: "function", function: {...}}`` envelope."""
    out: list[dict[str, Any]] = []
    for t in tools:
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
                },
            }
        )
    return out


def _to_openai_messages(
    system: str,
    messages: list[Message],
) -> list[dict[str, Any]]:
    """Translate the LCD message list into OpenAI chat-completion shape.

    OpenAI expects: optional system message up front; user/assistant
    with strings (assistants may add ``tool_calls`` at the top level
    with JSON-STRING arguments); tool outputs as ``role=tool`` with
    ``tool_call_id``.
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
            msg["content"] = text  # OpenAI accepts null when tool_calls is set
            tcs = []
            for tc in m.get("tool_calls", []) or []:
                tcs.append(
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc.get("arguments", {})),
                        },
                    }
                )
            if tcs:
                msg["tool_calls"] = tcs
            # Carry reasoning_content back if present (parity with
            # deepseek provider).  vLLM tolerates the extra field.
            pdata = m.get("provider_data") or {}
            rc = pdata.get("reasoning_content")
            if rc:
                msg["reasoning_content"] = rc
            out.append(msg)
            continue
        if role == "tool":
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m["tool_call_id"],
                    "content": m["content"],
                }
            )
            continue
    return out


def _extract_response_headers(resp: Any) -> dict[str, str]:
    """Best-effort header capture from an OpenAI response object."""
    for attr in ("response", "_response", "http_response", "raw_response"):
        obj = getattr(resp, attr, None)
        if obj is None:
            continue
        headers = getattr(obj, "headers", None)
        if headers is None:
            continue
        try:
            return {str(k).lower(): str(v) for k, v in dict(headers).items()}
        except Exception:  # noqa: BLE001
            continue
    return {}


def _safe_model_dump(resp: Any) -> dict[str, Any]:
    """JSON-serializable dump of an OpenAI / OpenAI-compat response.

    The SDK exposes pydantic ``model_dump(mode='json')``; falls back
    to best-effort dict conversion if the SDK version doesn't have
    that method.  Empty dict on any serialization failure (debug log
    is forensic, never load-bearing).
    """
    try:
        return resp.model_dump(mode="json")
    except Exception:  # noqa: BLE001
        try:
            return {k: v for k, v in vars(resp).items() if not k.startswith("_")}
        except Exception:  # noqa: BLE001
            return {}


def _extract_usage(usage_obj: Any) -> dict[str, Any]:
    """Pull usage fields from an OpenAI response Usage object."""
    if usage_obj is None:
        return {}
    out: dict[str, Any] = {}
    for attr in (
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "cached_tokens",
        "reasoning_tokens",
    ):
        v = getattr(usage_obj, attr, None)
        if v is not None:
            out[attr] = v
    # Map to canonical input/output names too for cross-provider parity.
    if "prompt_tokens" in out:
        out["input_tokens"] = out["prompt_tokens"]
    if "completion_tokens" in out:
        out["output_tokens"] = out["completion_tokens"]
    return out


# ---------------------------------------------------------------------------
# OpenAI provider
# ---------------------------------------------------------------------------


class OpenAIToolProvider(ToolCallingProvider):
    """OpenAI chat-completions tool-calling provider.

    Constructor parameters:

    - ``model``: model identifier (e.g. ``"gpt-4o"``, ``"gpt-5"``).
    - ``api_key``: API key, or read from ``OPENAI_API_KEY``.
    - ``base_url``: optional override for OpenAI-compatible endpoints
      (vLLM, TGI, LM Studio).  Default hits ``api.openai.com``.
    - ``max_tokens``: per-turn output cap.
    - ``temperature``: sampling temperature.
    """

    name = "openai"

    def __init__(
        self,
        model: str = "gpt-4o",
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        parallel_tool_calls: bool | None = None,
    ):
        import os

        try:
            import openai
        except ImportError as e:
            raise RuntimeError("pip install openai") from e
        key = api_key or os.environ.get("OPENAI_API_KEY") or "sk-dummy"
        # parallel_tool_calls: None = endpoint default; False = force one
        # tool_call per turn (avoids the cascade-failure pattern thinking
        # models exhibit when batching). vLLM with the hermes parser honours
        # this; not all endpoints do.
        if parallel_tool_calls is None:
            env = os.environ.get("PARALLEL_TOOL_CALLS")
            if env is not None:
                parallel_tool_calls = env.lower() not in ("0", "false", "no")
        self._parallel_tool_calls = parallel_tool_calls
        # max_retries=5 (SDK default is 2): each request to a reasoning model
        # can take 15-30 min, so a transient blip during a long thinking
        # window must not cost a whole attempt. The SDK retries on
        # 408/409/429/5xx + ConnectionError + APITimeoutError.
        # Timeout + retries are critical for thinking models on hard puzzles.
        # Default openai SDK timeout is 600s; for Qwen3-Thinking with
        # max-tokens=131072 a single turn can legitimately take 30-90 min
        # of generation. Allow override via env vars.
        request_timeout = float(os.environ.get("REQUEST_TIMEOUT_SECONDS", "14400"))  # 4h default
        max_retries = int(os.environ.get("REQUEST_MAX_RETRIES", "2"))
        client_kwargs: dict[str, Any] = {
            "api_key": key,
            "max_retries": max_retries,
            "timeout": request_timeout,
        }
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = openai.OpenAI(**client_kwargs)
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature

    def provider_config_for_log(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

    def respond(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSchema],
    ) -> ModelTurn:
        oai_msgs = _to_openai_messages(system, messages)
        oai_tools = _to_openai_tools(tools)
        t_start = time.time()
        create_kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "tools": oai_tools,
            "messages": oai_msgs,
            "seed": getattr(self, "_request_seed", 0),
        }
        if self._parallel_tool_calls is not None:
            create_kwargs["parallel_tool_calls"] = self._parallel_tool_calls
        resp = self._client.chat.completions.create(**create_kwargs)
        wallclock = time.time() - t_start

        choice = resp.choices[0]
        msg = choice.message
        text = msg.content or ""
        # vLLM with --reasoning-parser populates msg.reasoning_content
        # for thinking models (Qwen3-Thinking, deepseek-r1-distill, etc.).
        # The OpenAI public API doesn't expose this field, but the SDK
        # round-trips arbitrary attributes via getattr — same pattern as
        # providers/deepseek.py.  Capturing it is REQUIRED for paper-grade
        # forensics: without it the assistant turn shows tool_calls only,
        # losing the entire chain-of-thought.
        reasoning_content = getattr(msg, "reasoning_content", None) or ""
        tool_calls: list[ToolCall] = []
        for tc in msg.tool_calls or []:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            tool_calls.append(
                ToolCall(
                    id=tc.id,
                    name=tc.function.name,
                    arguments=args if isinstance(args, dict) else {},
                )
            )
        return ModelTurn(
            text=text,
            tool_calls=tool_calls,
            stop_reason=choice.finish_reason or "",
            raw=resp,
            raw_dict=_safe_model_dump(resp),
            usage=_extract_usage(getattr(resp, "usage", None)),
            wallclock_seconds=wallclock,
            response_headers=_extract_response_headers(resp),
            # Carry reasoning_content forward so the JSONL log preserves
            # the model's chain-of-thought.  vLLM doesn't require it on
            # subsequent requests (unlike DeepSeek's API which 400s without
            # it), but capturing it on output is the load-bearing fix.
            provider_data={"reasoning_content": reasoning_content} if reasoning_content else {},
        )


# ---------------------------------------------------------------------------
# vLLM alias
# ---------------------------------------------------------------------------


class VLLMToolProvider(OpenAIToolProvider):
    """Thin alias for a vLLM server speaking OpenAI-compat tool-use.

    Same as ``OpenAIToolProvider`` but defaults the base URL to
    ``http://localhost:8000/v1`` and the API key to ``"vllm"``.  vLLM
    must be launched with ``--enable-auto-tool-choice`` and an
    appropriate ``--tool-call-parser`` (e.g. ``hermes`` for Qwen,
    ``llama3_json`` for Llama 3.x).
    """

    name = "vllm"

    def __init__(
        self,
        model: str = "meta-llama/Llama-3.1-8B-Instruct",
        api_key: str = "vllm",
        base_url: str = "http://localhost:8000/v1",
        max_tokens: int = 4096,
        temperature: float = 0.0,
    ):
        super().__init__(
            model=model,
            api_key=api_key,
            base_url=base_url,
            max_tokens=max_tokens,
            temperature=temperature,
        )
