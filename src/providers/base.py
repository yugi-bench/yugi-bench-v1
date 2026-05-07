"""Provider-agnostic types + ABC for tool-calling providers.

This file is the COMPLETE SPECIFICATION for what a yugi-bench
tool-calling provider must do.  An implementer should be able to
write a working provider knowing only what's in this file — no
peeking at existing provider implementations needed.

============================================================================
DESIGN PRINCIPLES
============================================================================

1. **LCD interface.** Every provider exposes the same ``respond()``
   signature: take a system prompt, a conversation history, and a tool
   schema; return a ``ModelTurn``.  Episode's loop does not branch on
   provider type — it treats every provider identically.

2. **Provider-specific state is OPAQUE.** Anthropic-specific concepts
   (thinking blocks + cryptographic signatures, prompt-cache breakpoints
   with TTLs, adaptive-thinking effort levels), OpenAI-specific concepts
   (function-call objects, tool-choice modes), local-model concepts
   (any), MUST be hidden inside the provider.  Episode never imports an
   SDK type, never inspects a provider attribute beyond the LCD fields,
   never special-cases by ``provider.name``.

3. **Round-tripping is the provider's job.** When a provider returns a
   ``ModelTurn`` whose internal state needs to round-trip back to the
   API on the next call (e.g. Anthropic requires thinking blocks to
   come back verbatim with their signatures), the provider stores that
   state in ``ModelTurn.provider_data``.  Episode appends a serializable
   snapshot of provider_data into the assistant message, and the
   provider reads it back from there next call.  Episode treats the
   contents as opaque JSON — it never inspects the keys.

4. **All providers are at parity.** A provider author should never
   have to grep for "anthropic" or "openai" in non-provider code.  If a
   feature requires support from the harness side (e.g. logging
   stream events to a side file), that feature is exposed via a
   provider hook with a default no-op implementation, not by
   special-casing inside Episode.

5. **Side-effect surfaces are explicit.** Providers that want to log
   streaming events, emit telemetry, capture HTTP headers, etc., do so
   through documented optional hooks declared in this base file.  No
   provider may write to the Episode log file directly; Episode owns
   that.  Providers may write to their own side files (e.g.
   ``<out_dir>/stream-events.jsonl``) via an explicit constructor
   parameter.

============================================================================
THE INTERFACE
============================================================================

A provider is a class with:

- ``name: str`` — short stable identifier (used for logging + CLI
  selection).  Conventional: lowercase, no spaces (``"anthropic"``,
  ``"openai"``, ``"vllm"``, ``"claude-cli"``, ``"my-local-llm"``).
- ``model: str`` — the specific model identifier the provider is
  configured to talk to.  Free-form; surfaced in logs.
- ``__init__(...)`` — provider-specific config.  Common patterns are
  documented under ``ToolCallingProvider`` below.
- ``respond(system, messages, tools) -> ModelTurn`` — the one
  required method.

The ABC also provides a concrete ``complete(prompt, system) ->
ModelTurn`` method on top of ``respond()``, used by n-attempts bulk-
mode callers.  Subclasses inherit it for free and don't need to
override it.

Optional hooks (each with a no-op default in the ABC):

- ``configure_logging(log_dir)`` — called once by Episode before any
  ``respond()`` so the provider can open per-puzzle side log files.
- ``provider_config_for_log() -> dict`` — return a JSON-serializable
  dict of the provider's settings to bake into the per-puzzle config
  log entry.

============================================================================
THE CONVERSATION SHAPE
============================================================================

The ``messages`` argument to ``respond()`` is a list of dicts using the
LCD shape below.  Providers translate to/from their SDK's native shape
internally (see ``_to_anthropic_messages`` etc. in the existing
implementations as one example, but you're free to do this however you
like — the shape Episode uses is the contract).

User message:
    {"role": "user", "content": "<string or structured content>"}

Assistant message (after ``respond()`` returned a ModelTurn):
    {"role": "assistant",
     "text": "<the visible text>",
     "tool_calls": [{"id": "...", "name": "...", "arguments": {...}}, ...],
     "provider_data": {<provider-opaque dict>}}

Tool result message (engine's response to a tool_call):
    {"role": "tool",
     "tool_call_id": "<the id from the assistant's tool_call>",
     "content": "<string>",
     "is_error": False}

The provider's ``respond()`` MUST handle all three shapes when
translating ``messages`` to its API's native format.  Episode emits
messages in this exact shape; the provider is responsible for the
conversion both ways.

The ``tools`` argument is a list of JSON-Schema-shaped dicts (the
canonical shape Anthropic uses):

    {"name": "select_idlecmd",
     "description": "...",
     "input_schema": {"type": "object", "properties": {...}, "required": [...]}}

Providers whose API uses a different shape (e.g. OpenAI's
``{"type": "function", "function": {...}}``) translate as needed.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Type aliases for documentation clarity.  These aren't enforced by Python's
# type system; they're hints to the implementer.
# ---------------------------------------------------------------------------

Message = dict[str, Any]
"""Conversation message — see module docstring 'THE CONVERSATION SHAPE'."""

ToolSchema = dict[str, Any]
"""Tool definition with ``name`` / ``description`` / ``input_schema`` keys."""


# ---------------------------------------------------------------------------
# LCD types — same across every provider.  Episode reads these.
# ---------------------------------------------------------------------------


@dataclass
class ToolCall:
    """One tool invocation the model requested.

    All three fields are universal across providers:

    - ``id``: a unique identifier for this call within the assistant
      turn, used to correlate the engine's tool_result back to the
      call.  Providers must generate or extract this from the API
      response.  Format is provider-specific (Anthropic uses
      ``"toolu_<random>"``; OpenAI uses ``"call_<random>"``); Episode
      treats it as an opaque string.
    - ``name``: the tool name (must match a name in the ``tools`` list
      passed to ``respond``).
    - ``arguments``: the parsed JSON arguments object.  Keys + types
      follow the tool's ``input_schema``.
    """

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ModelTurn:
    """The provider's response to one ``respond()`` call.

    All fields are LCD across providers; ``provider_data`` is the one
    escape hatch for provider-specific state that needs to round-trip
    into the next call's conversation.

    Field semantics:

    - ``text``: the visible text the model produced (joined across
      whatever blocks/segments the API returned).  May be empty if the
      model returned only tool_calls.
    - ``tool_calls``: zero or more ``ToolCall``s extracted from the
      response.
    - ``stop_reason``: free-form string indicating why the model
      stopped.  Conventional values include ``"end_turn"``,
      ``"tool_use"``, ``"max_tokens"``, ``"stop_sequence"``.  Surfaced
      in logs; not interpreted by Episode.
    - ``usage``: token + cost accounting from the API response.
      Conventional keys (when available): ``input_tokens``,
      ``output_tokens``, ``cache_read_input_tokens``,
      ``cache_creation_input_tokens``, plus any nested breakdowns
      (e.g. Anthropic's ``cache_creation`` ttl split).  Empty dict if
      the provider doesn't expose structured usage.
    - ``wallclock_seconds``: the time ``respond()`` took end-to-end,
      including any retries inside the SDK.
    - ``response_headers``: HTTP response headers (lowercased keys).
      Useful for forensic provenance (request-id, organization-id,
      rate-limit headers, served-model-id).  Empty dict if not
      exposed.
    - ``provider_data``: opaque JSON-serializable dict for
      provider-specific state that must round-trip into subsequent
      calls.  Episode appends this verbatim to the assistant message
      and passes it back via ``messages``.  The provider reads it
      back in its next ``respond()`` call when translating
      ``messages`` to API-native format.

      Example: Anthropic's ``provider_data`` carries thinking blocks
      with their cryptographic signatures, because the API verifies
      the signature on subsequent calls.  Episode doesn't know or
      care about thinking blocks; it just preserves the dict.
    """

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    wallclock_seconds: float = 0.0
    response_headers: dict[str, str] = field(default_factory=dict)
    provider_data: dict[str, Any] = field(default_factory=dict)
    raw: Any = None
    """Optional: the raw SDK response object, for debugging.  Not
    serialized into logs (might not be JSON-friendly).  Episode never
    reads this; it's purely for the provider's own use during
    development."""

    raw_dict: dict[str, Any] = field(default_factory=dict)
    """JSON-serializable mirror of ``raw`` — typically
    ``resp.model_dump(mode='json')`` from the provider SDK.  Captures
    the FULL API response for forensic side-logging (response id,
    system_fingerprint, complete reasoning content, finish_reason,
    timestamps, etc.).  The runner persists this to a per-puzzle
    debug file; it's never required for correct operation, only for
    post-hoc analysis when something looks off."""


# ---------------------------------------------------------------------------
# The ABC every provider implements.
# ---------------------------------------------------------------------------


class ToolCallingProvider(ABC):
    """Abstract provider — implement this to add a new backend.

    Lifecycle (per puzzle):

    1. ``__init__(...)`` — provider configures its SDK client / CLI
       binary / local-model handle / etc.  Common kwargs by convention
       (none required by the ABC):
         - ``model: str`` — the model identifier
         - ``api_key: str | None`` — falls back to env var
         - ``max_tokens: int`` — per-turn output cap
         - ``temperature: float`` — sampling temp
         - ``base_url: str | None`` — for OpenAI-compatible endpoints
       Provider-specific kwargs (e.g. ``effort``, ``adaptive_thinking``)
       are fine; document them in the provider's docstring.

    2. ``configure_logging(log_dir: Path)`` — called once by Episode
       before any ``respond()`` if the provider opted in (default
       no-op).  ``log_dir`` is the per-puzzle output directory.  Use
       this hook if you want to write a side log file.

    3. ``provider_config_for_log() -> dict`` — called once by Episode
       to capture the provider's effective configuration in the
       per-puzzle ``config`` log entry.  Default implementation returns
       just ``{"name": self.name, "model": self.model}``; override to
       include extra settings.

    4. ``respond(system, messages, tools) -> ModelTurn`` — called once
       per model turn.  This is the only required method.  See its
       docstring below.

    5. ``complete(prompt, system) -> ModelTurn`` (concrete on the ABC)
       — single-shot convenience for bulk-mode callers; wraps
       ``respond()`` with one user message and ``tools=[]``.  Subclasses
       inherit it for free.

    Provider implementations should be FULLY SELF-CONTAINED.  A new
    provider should be writeable as a single file in
    ``providers/`` without touching any existing provider's
    file or any shared module.
    """

    name: str = "abstract"
    model: str = ""

    def complete(self, prompt: str, system: str = "") -> ModelTurn:
        """Single-shot convenience over ``respond()`` for bulk-mode callers.

        Sends one user message, no tools, no conversation history.
        Returns the full ``ModelTurn`` so callers can read ``.text`` for
        the assistant reply and ``.usage`` / ``.wallclock_seconds`` /
        ``.response_headers`` for accounting on the same object — no
        shared mutable state on the provider, so concurrent callers
        from a thread pool stay independent.

        N-attempts bulk mode uses this; fully interactive mode (Episode)
        calls ``respond()`` directly. Provider-specific config (``reasoning_effort``,
        ``thinking_enabled``, ``effort``, etc.) lives in ``__init__``
        and is honoured here exactly as it is for ``respond()`` —
        passing ``tools=[]`` doesn't disable thinking mode.
        """
        return self.respond(
            system=system,
            messages=[{"role": "user", "content": prompt}],
            tools=[],
        )

    @abstractmethod
    def respond(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSchema],
    ) -> ModelTurn:
        """Send one prompt to the model, return one turn.

        Parameters
        ----------
        system : str
            The system prompt for the entire conversation.  Identical
            across calls within an episode.  Provider may apply
            prompt-caching markers if it has the capability — that's
            an internal concern.
        messages : list[Message]
            The conversation history in LCD shape (see module
            docstring).  Translate to your SDK's native shape inside
            this method.
        tools : list[ToolSchema]
            The tool definitions available to the model on this turn.
            Same list across calls within an episode — caching
            applies.  Translate to your SDK's tool-definition shape
            as needed.

        Returns
        -------
        ModelTurn
            See ``ModelTurn`` docstring for field semantics.  At
            minimum populate ``text``, ``tool_calls``, ``stop_reason``,
            ``usage``, ``wallclock_seconds``.  Populate
            ``response_headers`` if your transport exposes them.
            Populate ``provider_data`` with anything your provider
            needs to round-trip on subsequent calls (e.g. Anthropic's
            thinking blocks with signatures).

        Error handling
        --------------
        Raise an exception on transport failure (network error, auth
        error, malformed response).  Episode wraps ``respond()`` in a
        retry loop with exponential backoff and logs each attempt.
        Do NOT swallow errors silently.

        Round-tripping provider_data
        ----------------------------
        On each call where the messages list contains an assistant
        turn that you produced, you'll find your previous
        ``provider_data`` under ``messages[i]["provider_data"]``.
        Read it back and reconstruct whatever API-native objects you
        need (e.g. Anthropic reads ``provider_data["thinking_blocks"]``
        and prepends them to the assistant content blocks).  Episode
        guarantees: if you put dict X in turn.provider_data, you'll
        get dict X back in the corresponding assistant message's
        ``provider_data`` field on subsequent calls.
        """

    # --- Optional hooks (no-op defaults) ------------------------------

    def configure_logging(self, log_dir: Any) -> None:  # log_dir: pathlib.Path
        """Optional hook: open per-puzzle side log files.

        Episode calls this once before the first ``respond()`` if the
        provider was constructed with logging enabled.  ``log_dir`` is
        the per-puzzle output directory (created by Episode).  Use
        this to open files like ``log_dir / "stream-events.jsonl"``.

        Default implementation is a no-op.
        """
        return None

    def provider_config_for_log(self) -> dict[str, Any]:
        """Optional hook: dict of effective provider settings for the
        per-puzzle ``config`` log entry.

        Default returns ``{"name": self.name, "model": self.model}``.
        Override to add extra settings (e.g. ``temperature``,
        ``max_tokens``, ``effort`` — anything a replayer would need to
        reconstruct the run).  Must be JSON-serializable.
        """
        return {"name": self.name, "model": self.model}
