"""Anthropic Messages-API tool-calling provider.

Self-contained implementation of ``ToolCallingProvider`` for the
Anthropic SDK (https://docs.anthropic.com/).  Targets Opus 4.7+ by
default (adaptive thinking, output_config.effort, no manual thinking
budget, summarized thinking display, 1-hour ephemeral cache TTL).

Anthropic-specific concepts encapsulated here:

- **Adaptive thinking** (``thinking={type: "adaptive"}``) plus
  ``output_config={effort: ...}`` — Opus 4.7's only thinking mode.
- **Summarized thinking display** — thinking blocks come back with
  empty ``thinking`` field unless ``display: "summarized"`` is set.
- **Prompt caching with 1h TTL** — three ephemeral breakpoints
  (system, tools, sliding conversation suffix) all on 1-hour TTL
  to avoid cache rebake disasters when deep-think turns exceed 5min.
- **Streaming required** — Anthropic refuses non-streaming requests
  whose worst-case generation could exceed 10 minutes.
- **Temperature rejected** — Opus 4.7 returns 400 for any non-default
  temperature; we omit it entirely when adaptive thinking is on.
- **Thinking blocks round-trip** — the API verifies cryptographic
  signatures on thinking blocks passed back in subsequent calls.
  We carry these in ``ModelTurn.provider_data["thinking_blocks"]``
  (opaque to Episode) and prepend them to the assistant content
  blocks on the next call's translation.
- **Stream-event side log** (optional) — when constructed with
  ``stream_event_log_path``, structural streaming events are written
  to a JSONL side file for forensics.

This file imports the ``anthropic`` SDK lazily inside ``__init__`` so
the package itself can be imported in environments where the SDK
isn't installed (e.g. unit tests, OpenAI-only deployments).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .base import (
    Message,
    ModelTurn,
    ToolCall,
    ToolCallingProvider,
    ToolSchema,
)


# ---------------------------------------------------------------------------
# SDK-shape helpers (Anthropic-specific)
# ---------------------------------------------------------------------------

def _extract_usage(usage_obj: Any) -> dict[str, Any]:
    """Pull token + cache accounting out of an Anthropic SDK Usage object.

    The SDK's Usage type has shifted over releases — extract permissively
    so we don't lose anything when a new field appears.  Returns an
    empty dict when the response carries no usage (rare; some error
    paths).
    """
    if usage_obj is None:
        return {}
    out: dict[str, Any] = {}
    flat_attrs = (
        "input_tokens",
        "output_tokens",
        "cache_read_input_tokens",
        "cache_creation_input_tokens",
        "server_tool_use_input_tokens",
        "server_tool_use_output_tokens",
    )
    for attr in flat_attrs:
        v = getattr(usage_obj, attr, None)
        if v is not None:
            out[attr] = v
    cc = getattr(usage_obj, "cache_creation", None)
    if cc is not None:
        out["cache_creation"] = {
            "ephemeral_5m_input_tokens": getattr(cc, "ephemeral_5m_input_tokens", None),
            "ephemeral_1h_input_tokens": getattr(cc, "ephemeral_1h_input_tokens", None),
        }
    try:
        for k in dir(usage_obj):
            if k.startswith("_") or k in flat_attrs or k == "cache_creation":
                continue
            v = getattr(usage_obj, k, None)
            if isinstance(v, (int, float, str, bool)):
                out.setdefault(f"_extra_{k}", v)
    except Exception:  # noqa: BLE001
        pass
    return out


def _extract_response_headers(stream_or_resp: Any) -> dict[str, str]:
    """Best-effort capture of HTTP response headers across SDK versions."""
    candidates = ("response", "http_response", "_response", "raw_response",
                  "_raw_response", "request_response")
    for attr in candidates:
        obj = getattr(stream_or_resp, attr, None)
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
    """JSON-serializable dump of an Anthropic response object."""
    try:
        return resp.model_dump(mode="json")
    except Exception:  # noqa: BLE001
        try:
            return {k: v for k, v in vars(resp).items() if not k.startswith("_")}
        except Exception:  # noqa: BLE001
            return {}


def _serialize_stream_event(ev: Any) -> dict[str, Any]:
    """Best-effort JSON-friendly snapshot of a streaming event."""
    try:
        if hasattr(ev, "model_dump"):
            return ev.model_dump(mode="json")
    except Exception:  # noqa: BLE001
        pass
    try:
        return {k: v for k, v in vars(ev).items() if not k.startswith("_")}
    except Exception:  # noqa: BLE001
        return {"repr": str(ev)}


def _to_anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Translate the LCD message list into Anthropic Messages-API shape.

    LCD assistant messages may carry ``provider_data["thinking_blocks"]``
    — we prepend those (verbatim, with signatures intact) to the
    assistant content blocks per Anthropic's content-order contract for
    extended thinking.
    """
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(messages):
        m = messages[i]
        role = m["role"]
        if role == "user":
            out.append({"role": "user", "content": m["content"]})
            i += 1
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            # Thinking blocks (from provider_data) MUST come first.
            pdata = m.get("provider_data") or {}
            for tb in pdata.get("thinking_blocks", []) or []:
                blocks.append(dict(tb))
            text = m.get("text", "")
            if text:
                blocks.append({"type": "text", "text": text})
            for tc in m.get("tool_calls", []) or []:
                blocks.append({
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["name"],
                    "input": tc.get("arguments", {}),
                })
            if not blocks:
                blocks = [{"type": "text", "text": ""}]
            out.append({"role": "assistant", "content": blocks})
            i += 1
            continue
        if role == "tool":
            tool_blocks = []
            while i < len(messages) and messages[i]["role"] == "tool":
                tm = messages[i]
                tool_blocks.append({
                    "type": "tool_result",
                    "tool_use_id": tm["tool_call_id"],
                    "content": tm["content"],
                    "is_error": tm.get("is_error", False),
                })
                i += 1
            # Absorb an immediately-following user message into the same
            # turn — Anthropic requires strict user/assistant alternation
            # and the Episode loop naturally emits [tool_result,
            # next_observation] back-to-back.
            if i < len(messages) and messages[i]["role"] == "user":
                um = messages[i]
                uc = um["content"]
                if isinstance(uc, str):
                    tool_blocks.append({"type": "text", "text": uc})
                else:
                    tool_blocks.append({"type": "text",
                                        "text": json.dumps(uc, default=str)})
                i += 1
            out.append({"role": "user", "content": tool_blocks})
            continue
        i += 1
    return out


# ---------------------------------------------------------------------------
# The provider class
# ---------------------------------------------------------------------------

class AnthropicToolProvider(ToolCallingProvider):
    """Anthropic Messages-API provider for Opus 4.7 + later.

    Constructor parameters:

    - ``model``: model alias, e.g. ``"claude-opus-4-7"``.
    - ``api_key``: OAuth/API key, or read from ``ANTHROPIC_API_KEY``.
    - ``max_tokens``: per-turn output cap (default 128000, the Opus 4.7
      ceiling).  Includes thinking + visible content combined.
    - ``temperature``: sampling temperature.  IGNORED when adaptive
      thinking is on (the API forces 1.0 in that case and would 400
      on non-default values).
    - ``effort``: ``"low" | "medium" | "high" | "xhigh" | "max"`` —
      ``output_config.effort``.  Controls thinking depth and overall
      token spend.  None or ``"off"`` omits the parameter.  Anthropic
      recommends ``xhigh`` as the starting point for coding/agentic
      work; ``max`` is for "genuinely frontier problems" and adds
      significant cost for relatively small quality gains.
    - ``adaptive_thinking``: enable ``thinking={type: "adaptive"}``.
      Required on Opus 4.7 — manual thinking is rejected with 400.
    - ``stream_event_log_path``: optional path to a JSONL file where
      structural streaming events are appended for forensics.
    """

    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-opus-4-7",
        api_key: str | None = None,
        max_tokens: int = 128000,
        temperature: float = 1.0,
        effort: str | None = "max",
        adaptive_thinking: bool = True,
        stream_event_log_path: str | None = None,
    ):
        import os
        try:
            import anthropic
        except ImportError as e:
            raise RuntimeError("pip install anthropic") from e
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        self._client = anthropic.Anthropic(api_key=key)
        self.model = model
        self.max_tokens = max_tokens
        self.effort = effort if effort and effort.lower() != "off" else None
        self.adaptive_thinking = adaptive_thinking
        # Anthropic forces temperature=1.0 when adaptive thinking is on.
        self.temperature = 1.0 if adaptive_thinking else temperature
        self._stream_event_log = None
        if stream_event_log_path:
            Path(stream_event_log_path).parent.mkdir(parents=True, exist_ok=True)
            self._stream_event_log = open(stream_event_log_path, "a", buffering=1)

    def provider_config_for_log(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "effort": self.effort,
            "adaptive_thinking": self.adaptive_thinking,
        }

    def respond(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSchema],
    ) -> ModelTurn:
        anthro_msgs = _to_anthropic_messages(messages)

        # Three 1-hour ephemeral cache breakpoints: system, end-of-tools,
        # end-of-conversation.  See base.py docs for design rationale and
        # commit log for the empirical 60af8f49 cost data.
        cache_marker = {"type": "ephemeral", "ttl": "1h"}
        system_blocks = [{
            "type": "text",
            "text": system,
            "cache_control": cache_marker,
        }]
        cached_tools = [dict(t) for t in tools]
        if cached_tools:
            cached_tools[-1] = {**cached_tools[-1], "cache_control": cache_marker}
        if anthro_msgs:
            last = anthro_msgs[-1]
            content = last.get("content")
            if isinstance(content, list) and content:
                last_block = dict(content[-1])
                last_block["cache_control"] = cache_marker
                last["content"] = list(content[:-1]) + [last_block]
            elif isinstance(content, str):
                last["content"] = [{
                    "type": "text",
                    "text": content,
                    "cache_control": cache_marker,
                }]

        create_kwargs: dict[str, Any] = dict(
            model=self.model,
            max_tokens=self.max_tokens,
            system=system_blocks,
            tools=cached_tools,
            messages=anthro_msgs,
        )
        # Opus 4.7 rejects non-default temperature with 400 — only send
        # it when adaptive thinking is OFF.  Adaptive thinking forces
        # effective 1.0, so omitting is correct in the on case.
        if not self.adaptive_thinking:
            create_kwargs["temperature"] = self.temperature
        if self.adaptive_thinking:
            create_kwargs["thinking"] = {
                "type": "adaptive",
                "display": "summarized",
            }
        if self.effort:
            create_kwargs["output_config"] = {"effort": self.effort}

        # Stream + assemble (Anthropic's SDK refuses non-streaming
        # requests whose worst-case generation could exceed 10 minutes).
        SKIP_KINDS = {"content_block_delta", "input_json"}
        response_headers: dict[str, str] = {}
        t_start = time.time()
        with self._client.messages.stream(**create_kwargs) as stream:
            for ev in stream:
                if self._stream_event_log is not None:
                    kind = getattr(ev, "type", None)
                    if kind not in SKIP_KINDS:
                        try:
                            payload = {
                                "ts": time.time(),
                                "kind": kind,
                                "model": self.model,
                                "data": _serialize_stream_event(ev),
                            }
                            self._stream_event_log.write(
                                json.dumps(payload, default=str) + "\n"
                            )
                        except Exception:  # noqa: BLE001
                            pass
            resp = stream.get_final_message()
            response_headers = _extract_response_headers(stream)
        wallclock = time.time() - t_start

        usage = _extract_usage(getattr(resp, "usage", None))

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        thinking_blocks: list[dict[str, Any]] = []
        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_parts.append(block.text)
            elif btype == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.id, name=block.name,
                    arguments=dict(block.input) if block.input else {},
                ))
            elif btype == "thinking":
                thinking_blocks.append({
                    "type": "thinking",
                    "thinking": block.thinking,
                    "signature": block.signature,
                })
            elif btype == "redacted_thinking":
                thinking_blocks.append({
                    "type": "redacted_thinking",
                    "data": block.data,
                })
        return ModelTurn(
            text="".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=resp.stop_reason or "",
            raw=resp,
            raw_dict=_safe_model_dump(resp),
            usage=usage,
            wallclock_seconds=wallclock,
            response_headers=response_headers,
            # provider_data carries thinking blocks (with signatures) so
            # they round-trip back into the next call's assistant
            # content via _to_anthropic_messages.  Episode treats this
            # dict as opaque.
            provider_data={"thinking_blocks": thinking_blocks},
        )
