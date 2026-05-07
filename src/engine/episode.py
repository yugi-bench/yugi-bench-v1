"""Fully interactive Episode loop — LLM plays YuGiOh via tools.

Hooks the provider-agnostic tool schema (``engine.tools``) onto the
one-to-one response harness (``engine.harness``). Exports:

  - ``Episode`` — single puzzle run (setup → loop → outcome).
  - ``ResumeError`` — raised when a partial-JSONL resume can't rebuild.
  - ``run_episode`` — convenience wrapper around ``Episode.run()``.

The Episode loop never interprets the game itself.  It:
  1. Starts the harness, collects the initial pending decision.
  2. Renders state (perspective = pending player) via ``engine.state``.
  3. Sends state + tool schema to the LLM via the
     ``providers.ToolCallingProvider`` ABC.
  4. Dispatches each tool call: inspection tools → handled locally;
     response tools → ``getattr(harness, TOOL_TO_HARNESS_METHOD[name])``.
  5. Appends tool result to messages, loops until game_over, tool-call
     budget exhausted, or the model stops calling tools.

Logs every exchange (model turn, tool call, tool result, state snapshot)
to an append-only JSONL for replay / scoring.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
import traceback
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .core import CardDB, OCGEngine
from .harness import (
    Harness,
    HarnessError,
    InvalidResponseError,
    PendingDecision,
    StepResult,
)
from providers import (
    AnthropicToolProvider,
    ClaudeCLIToolProvider,
    DeepSeekToolProvider,
    ModelTurn,
    OpenAIToolProvider,
    ToolCall,
    ToolCallingProvider,
    VLLMToolProvider,
)
from .state import build_state
from .tools import (
    INSPECTION_TOOL_NAMES,
    META_TOOL_NAMES,
    RESPONSE_TOOLS,
    TOOL_TO_HARNESS_METHOD,
    TOOLS,
    coerce_args,
    normalize_place_arg,
)


class ResumeError(Exception):
    """Raised by Episode._resume when a JSONL trace can't be replayed.

    Caller (run_inference) catches this and falls back to a fresh run
    by unlinking the JSONL and calling Episode without resume_from.
    """


@dataclass
class Episode:
    engine: OCGEngine
    provider: ToolCallingProvider
    lua_setup: str
    instance: dict[str, Any]
    card_db: CardDB
    perspective: int = 0
    max_tool_calls: int = 200
    forage: bool = False
    show_solution: bool = False
    system_prompt: str = ""  # built dynamically in _start() via lib.prompt_builder
    log_path: Path | None = None
    tools: list[dict[str, Any]] = field(default_factory=list)
    # If set, rebuild engine + conversation from this JSONL trace and
    # continue the loop from the recorded crash point instead of
    # starting fresh.  Used to resume runs killed by API credit
    # exhaustion etc.  See _resume() for the replay protocol.
    resume_from: Path | None = None

    def __post_init__(self):
        self.harness = Harness(self.engine)
        self._messages: list[dict[str, Any]] = []
        self._tool_calls_used = 0
        self._log_f = None
        # Set by _resume when the resumed JSONL ended on an unanswered
        # observation (typical API-exhaustion case).  Causes the first
        # loop iteration to skip obs generation since the obs is already
        # in _messages — otherwise we'd send a duplicate user message.
        self._resume_skip_first_obs = False
        # Tool list assembly:
        # - 20 response verbs: always available.
        # - `restart` (meta): always available.
        # - `get_state` + `pending_decision` (always-available inspection):
        #   ALWAYS available — they reflect EVOLVING state that's not
        #   already in the system prompt, so they're useful for grounding
        #   the model regardless of forage.
        # - `inspect_card` + `get_glossary` (forage-only inspection):
        #   only available with --forage.  In default mode the card
        #   glossary + engine notation are already in the system prompt,
        #   so these would just be a copy/paste lookup of static info.
        if not self.tools:
            from .tools import (
                ALWAYS_AVAILABLE_INSPECTION,
                FORAGE_ONLY_INSPECTION,
            )
            allowed: list[str] = (
                list(META_TOOL_NAMES)
                + list(TOOL_TO_HARNESS_METHOD.keys())
                + list(ALWAYS_AVAILABLE_INSPECTION)
            )
            if self.forage:
                allowed.extend(FORAGE_ONLY_INSPECTION)
            allowed_set = set(allowed)
            self.tools = [t for t in TOOLS if t["name"] in allowed_set]
        # Running totals across all turns of this puzzle.  Keys mirror
        # the per-turn `usage` dict (input_tokens, output_tokens,
        # cache_read_input_tokens, cache_creation_input_tokens, ...) plus
        # `model_calls` and `model_wallclock_seconds`.
        self._usage_totals: dict[str, Any] = {
            "model_calls": 0,
            "model_wallclock_seconds": 0.0,
        }
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_f = self.log_path.open("a", buffering=1)

    def _accumulate_usage(self, usage: dict[str, Any], wallclock: float) -> None:
        self._usage_totals["model_calls"] += 1
        self._usage_totals["model_wallclock_seconds"] = round(
            self._usage_totals["model_wallclock_seconds"] + wallclock, 3
        )
        for k, v in (usage or {}).items():
            if isinstance(v, (int, float)):
                self._usage_totals[k] = self._usage_totals.get(k, 0) + v
            elif isinstance(v, dict):
                # Sum nested numeric fields (e.g. cache_creation breakdown).
                bucket = self._usage_totals.setdefault(k, {})
                for kk, vv in v.items():
                    if isinstance(vv, (int, float)):
                        bucket[kk] = bucket.get(kk, 0) + vv

    # ------------------------------------------------------------------
    def run(self) -> dict[str, Any]:
        if self.resume_from is not None:
            start = self._resume(self.resume_from)
            # Resume that lands directly at game_over means the recorded
            # run already finished — label as a normal game_over rather
            # than the "before first decision" startup-state-pre-game-over
            # case below.
            if start.game_over:
                return self._outcome("game_over", start)
        else:
            start = self._start()
            if start.game_over:
                return self._outcome("game_over_before_first_decision", start)

        prior: StepResult = start

        while True:
            # Recovery hook: when pending is None and game isn'''t over, the
            # engine may have queued work that wasn'''t drained (e.g. a prior
            # tool_call'''s _respond_*/advance raised MSG_RETRY-exhaustion and
            # left _pending=None even after the harness restored it for
            # successful cases). Try one more advance() to see if a new
            # decision surfaces or the duel is actually over.
            if (self.harness.pending is None
                    and not self.harness.state.game_over):
                try:
                    recovery_step = self.harness.advance()
                    self._log({
                        "type": "harness_recovery_advance",
                        "events": recovery_step.events[-10:],
                        "new_pending": _pending_summary(recovery_step.pending),
                        "game_over": recovery_step.game_over,
                    })
                    if recovery_step.game_over or self.harness.state.game_over:
                        return self._outcome("game_over", recovery_step)
                    if recovery_step.pending is not None:
                        prior = recovery_step
                except Exception as _e:  # noqa: BLE001
                    self._log({
                        "type": "harness_recovery_advance_error",
                        "error": f"{type(_e).__name__}: {_e}",
                    })

            if self.harness.pending is None:
                return self._outcome(
                    "no_pending_decision_unexpected",
                    StepResult(events=[], pending=None,
                               game_over=self.harness.state.game_over),
                )
            if self.harness.state.game_over:
                return self._outcome("game_over", prior)
            if self._tool_calls_used >= self.max_tool_calls:
                return self._outcome("tool_budget_exhausted", prior)

            if self._resume_skip_first_obs:
                # On resume, the JSONL's trailing observation is already
                # in _messages — sending another one would duplicate the
                # user message.  Skip generation for one iteration.
                self._resume_skip_first_obs = False
            else:
                obs_msg = self._observation_user_message(prior.events)
                self._messages.append({"role": "user", "content": obs_msg})
                self._log({"type": "observation", "content": obs_msg})

            turn = self._call_model()
            # Append assistant turn to the conversation.  provider_data is
            # opaque — Episode preserves it verbatim so the provider can
            # round-trip its own state on the next call (e.g. Anthropic
            # carries thinking-blocks-with-signatures here).
            self._messages.append({
                "role": "assistant",
                "text": turn.text,
                "tool_calls": [
                    {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
                    for tc in turn.tool_calls
                ],
                "provider_data": dict(turn.provider_data),
            })
            self._accumulate_usage(turn.usage, turn.wallclock_seconds)
            self._log({
                "type": "model_turn",
                "text": turn.text,
                "tool_calls": [{"id": tc.id, "name": tc.name,
                                "arguments": tc.arguments}
                               for tc in turn.tool_calls],
                "stop_reason": turn.stop_reason,
                "provider_data": dict(turn.provider_data),
                "usage": turn.usage,
                "elapsed_seconds": round(turn.wallclock_seconds, 3),
                "cumulative": dict(self._usage_totals),
                "response_headers": turn.response_headers,
            })

            if not turn.tool_calls:
                return self._outcome("model_stopped_without_tool_call", prior)

            step_for_next_observation: StepResult | None = None
            batched = len(turn.tool_calls) > 1
            # When the model batches multiple tool_calls in one response, an
            # error in any single call usually means engine state has shifted
            # away from the model'''s predicted sequence — subsequent calls in
            # the same batch will cascade-fail (e.g. select_idlecmd advances
            # the state, the next select_idlecmd hits "no pending decision").
            # Short-circuit by appending placeholder tool_results for the
            # rest of the batch without dispatching, so the OpenAI tool-call
            # protocol stays valid and the model gets to re-plan from the
            # next observation rather than terminating with
            # no_pending_decision_unexpected.
            encountered_error_in_batch = False
            for idx, tc in enumerate(turn.tool_calls):
                if encountered_error_in_batch:
                    skip_msg = (
                        "Skipped: an earlier tool_call in this batch errored, "
                        "so the engine state likely diverged from your predicted "
                        "sequence. Re-evaluate from the next observation."
                    )
                    self._messages.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": skip_msg, "is_error": True,
                    })
                    self._log({
                        "type": "tool_result",
                        "tool_use_id": tc.id,
                        "name": tc.name,
                        "arguments": tc.arguments,
                        "content": skip_msg,
                        "is_error": True,
                        "skipped_after_batch_error": True,
                    })
                    continue
                if self._tool_calls_used >= self.max_tool_calls:
                    self._messages.append({
                        "role": "tool", "tool_call_id": tc.id,
                        "content": "Tool budget exhausted.", "is_error": True,
                    })
                    continue
                self._tool_calls_used += 1

                result = self._dispatch(tc)
                self._messages.append({
                    "role": "tool", "tool_call_id": tc.id,
                    "content": result["content"],
                    "is_error": result.get("is_error", False),
                })
                self._log({
                    "type": "tool_result",
                    "tool_use_id": tc.id,
                    "name": tc.name,
                    "arguments": tc.arguments,
                    "content": result["content"],
                    "is_error": result.get("is_error", False),
                })
                # Snapshot the engine state AFTER this action so the log is
                # self-contained for forensic replay — no need to read the
                # next observation entry to know what the action did.
                # Skipped only when the harness has been torn down (game
                # over already, or not yet started).
                try:
                    if self.harness._started and self.engine.duel:
                        snap = build_state(
                            self.harness, self.engine.card_db,
                            perspective=self.perspective,
                            include_decision=True,
                        )
                        self._log({
                            "type": "state_snapshot",
                            "after_tool": tc.name,
                            "after_tool_use_id": tc.id,
                            "is_error": result.get("is_error", False),
                            "state": snap,
                        })
                except Exception as _e:  # noqa: BLE001
                    # Don't let a state-build failure abort the run — record
                    # the error and continue.
                    self._log({
                        "type": "state_snapshot",
                        "after_tool": tc.name,
                        "error": f"{type(_e).__name__}: {_e}",
                    })

                if result.get("step") is not None:
                    step_for_next_observation = result["step"]
                    if result["step"].game_over or self.harness.state.game_over:
                        return self._outcome("game_over", result["step"])

                # Chain auto-decline: if the model batched multiple actions in
                # this turn and the engine now sits at an OPTIONAL select_chain
                # window, decline it transparently so the model's next queued
                # action runs against the post-chain state.  Only fires when
                # the model didn't queue a select_chain explicitly to handle
                # this window.  Forced chains never auto-decline — they always
                # come back to the model.
                if batched:
                    auto_step = self._auto_decline_chains_until_real(
                        turn.tool_calls, idx,
                    )
                    if auto_step is not None:
                        step_for_next_observation = auto_step
                        if (auto_step.game_over
                                or self.harness.state.game_over):
                            return self._outcome("game_over", auto_step)

                # If this tool_call errored AND we are in a batched response,
                # short-circuit the rest of the batch on the next iteration.
                if batched and result.get("is_error"):
                    encountered_error_in_batch = True

            if step_for_next_observation is not None:
                prior = step_for_next_observation
            else:
                prior = StepResult(
                    events=[], pending=self.harness.pending,
                    game_over=self.harness.state.game_over,
                    winner=self.harness.state.winner,
                )

    # ------------------------------------------------------------------
    # Setup / observation
    # ------------------------------------------------------------------
    def _start(self) -> StepResult:
        step = self.harness.start(self.lua_setup)
        # Now that the engine is initialised on this puzzle, build the
        # system prompt from the universal lib builder (depends on the
        # live engine state for omniscient/visible state rendering).
        from lib.prompt_builder import build_interactive_system_prompt
        self.system_prompt = build_interactive_system_prompt(
            instance=self.instance,
            card_db=self.card_db,
            harness=self.harness,
            forage=self.forage,
            show_solution=self.show_solution,
        )
        # Log the full configuration first so a replayer can reconstruct
        # the exact run conditions.  System prompt + tools change between
        # runs (prompt expansion, restart-tool addition, etc) so capturing
        # them inline is the only way to make old logs faithful.  The
        # provider supplies its own config dict via the LCD hook so
        # Episode never has to know provider-specific attribute names.
        try:
            provider_cfg = self.provider.provider_config_for_log()
        except Exception:  # noqa: BLE001
            provider_cfg = {"name": getattr(self.provider, "name", "?"),
                            "model": getattr(self.provider, "model", "?")}
        self._log({
            "type": "config",
            "perspective": self.perspective,
            "max_tool_calls": self.max_tool_calls,
            "forage": self.forage,
            "show_solution": self.show_solution,
            "system_prompt": self.system_prompt,
            "tools": list(self.tools),
            "provider": provider_cfg,
        })
        self._log({"type": "start", "events": step.events,
                   "pending": _pending_summary(step.pending)})
        return step

    def _resume(self, jsonl_path: Path) -> StepResult:
        """Re-create state from a JSONL trace and resume the loop.

        Walks the recorded events, rebuilds the conversation messages
        list (with provider_data preserved so reasoning_content /
        thinking_blocks round-trip correctly), and re-dispatches every
        engine-affecting tool call against a fresh harness so engine
        state matches the recording.

        ocgcore is deterministic given the same inputs, so the replay
        lands at the same internal state the original run was at when
        it died — provided the JSONL is well-formed.  If anything
        diverges (replay exception, engine state mismatch), we raise
        ``ResumeError`` so the caller can fall back to a fresh run.
        """
        records: list[dict[str, Any]] = []
        with jsonl_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except Exception as e:  # noqa: BLE001
                    raise ResumeError(f"corrupt JSONL line: {e}") from e
        if not records:
            raise ResumeError("empty JSONL")

        # The first record is the config (system prompt, tools, etc).
        # Use that snapshot rather than rebuilding via lib.prompt_builder
        # so the resumed conversation matches the original prompt
        # byte-for-byte.
        config_rec = records[0]
        if config_rec.get("type") != "config":
            raise ResumeError("first record is not 'config'")
        self.system_prompt = config_rec.get("system_prompt", "")

        # Re-init the engine on the original puzzle.  We don't log this
        # — the JSONL already has the original 'start' record.
        first = self.harness.start(self.lua_setup)

        # Pre-pass: older JSONL writers strip tool_call ids from
        # model_turn records but DO preserve tool_use_id on tool_result
        # records.  The provider then rejects the rebuilt conversation
        # because each assistant tool_call needs an id that matches a
        # subsequent tool message's tool_call_id.  Walk records once
        # to harvest each model_turn's tool ids from its immediately-
        # following tool_result records.  Later assistant rebuild uses
        # these (falling back to synthesis only if even the tool_results
        # are missing ids).
        model_turn_id_map: dict[int, list[str]] = {}
        for i, rec in enumerate(records):
            if rec.get("type") != "model_turn":
                continue
            ids: list[str] = []
            for j in range(i + 1, len(records)):
                rj_t = records[j].get("type")
                if rj_t == "tool_result":
                    ids.append(records[j].get("tool_use_id") or "")
                elif rj_t in ("model_turn", "observation"):
                    break
                # state_snapshot, auto_chain_decline — keep looking.
            model_turn_id_map[i] = ids

        # Walk the rest of the records in order.  Engine-affecting
        # records (model_turn tool_calls + auto_chain_decline) get
        # dispatched against the harness; conversation records
        # (observation, model_turn, tool_result) get appended to
        # _messages so the next provider.respond() sees the full
        # history.
        replayed_calls = 0
        replayed_auto_declines = 0
        synthetic_id_counter = 0
        for i, rec in enumerate(records[1:], start=1):
            t = rec.get("type")
            if t == "start":
                # Already handled by self.harness.start above.
                continue
            if t == "observation":
                self._messages.append({"role": "user",
                                       "content": rec.get("content", "")})
            elif t == "model_turn":
                # Build the assistant message's tool_calls list with
                # ids guaranteed and matching the upcoming tool_result
                # records.
                ids_from_results = model_turn_id_map.get(i, [])
                tcs_out: list[dict[str, Any]] = []
                for idx, tc in enumerate(rec.get("tool_calls") or []):
                    tcid = tc.get("id")
                    if not tcid and idx < len(ids_from_results) and ids_from_results[idx]:
                        tcid = ids_from_results[idx]
                    if not tcid:
                        tcid = f"resume_call_{synthetic_id_counter}"
                        synthetic_id_counter += 1
                    tcs_out.append({
                        "id": tcid,
                        "name": tc.get("name"),
                        "arguments": tc.get("arguments") or {},
                    })
                self._messages.append({
                    "role": "assistant",
                    "text": rec.get("text", ""),
                    "tool_calls": tcs_out,
                    "provider_data": dict(rec.get("provider_data") or {}),
                })
                if rec.get("usage"):
                    self._accumulate_usage(
                        rec["usage"],
                        rec.get("elapsed_seconds", 0.0) or 0.0,
                    )
                # Replay each tool call against the harness.  Inspection
                # tools are no-op for engine state; restart re-inits;
                # response tools mutate via the harness method map.
                for tc in tcs_out:
                    name = tc["name"]
                    args = tc.get("arguments") or {}
                    self._tool_calls_used += 1
                    try:
                        if name == "restart":
                            self.engine.destroy()
                            self.harness = Harness(self.engine)
                            self.harness.start(self.lua_setup)
                        elif name in TOOL_TO_HARNESS_METHOD:
                            method = getattr(
                                self.harness,
                                TOOL_TO_HARNESS_METHOD[name],
                            )
                            method(**coerce_args(name, args))
                        # else: inspection tool — no engine effect.
                        replayed_calls += 1
                    except Exception:  # noqa: BLE001
                        # Don't abort here — the original run may have
                        # produced an InvalidResponseError on this exact
                        # call.  As long as the next state_snapshot in
                        # the JSONL matches the engine, we're fine.
                        pass
            elif t == "tool_result":
                # tool_use_id is preserved by the runner's _log call;
                # the assistant tool_call ids were back-filled from
                # these in the pre-pass above so they match.
                self._messages.append({
                    "role": "tool",
                    "tool_call_id": rec.get("tool_use_id") or "",
                    "content": rec.get("content", ""),
                    "is_error": rec.get("is_error", False),
                })
            elif t == "auto_chain_decline":
                try:
                    self.harness.respond_select_chain(index=None)
                    replayed_auto_declines += 1
                except Exception:  # noqa: BLE001
                    pass
            # state_snapshot, provider_error, restart_acknowledged,
            # outcome — informational; no replay needed.  An outcome
            # record at the end means the run actually finished and
            # _shouldn't_ have been queued for resume; treat as fatal.
            elif t == "outcome":
                raise ResumeError(
                    "JSONL already has a terminal outcome record — "
                    "this run finished and shouldn't be resumed."
                )

        # If the JSONL ended on a user observation that was never
        # answered (typical API-exhaustion case), the obs is already in
        # _messages; tell the loop to skip generating a duplicate.
        if (self._messages
                and self._messages[-1].get("role") == "user"):
            self._resume_skip_first_obs = True

        # Mark the resume in the log so a future reader can see where
        # the new tail starts.
        self._log({
            "type": "resume",
            "from_jsonl": str(jsonl_path),
            "records_replayed": len(records),
            "tool_calls_used": self._tool_calls_used,
            "messages": len(self._messages),
            "replayed_calls": replayed_calls,
            "replayed_auto_declines": replayed_auto_declines,
            "skip_first_obs": self._resume_skip_first_obs,
        })

        # Return current state as if we just took the last engine step.
        # The Episode loop will see the harness.pending below and emit
        # the next observation (with empty events — nothing new since
        # last recorded observation), then call the model for turn N+1.
        return StepResult(
            events=[],
            pending=self.harness.pending,
            game_over=self.harness.state.game_over,
            winner=self.harness.state.winner,
        )

    def _observation_user_message(self, events: list[dict]) -> str:
        state = build_state(
            self.harness, self.engine.card_db,
            perspective=self.perspective, include_decision=True, events=events,
        )
        return json.dumps(state, default=str, indent=2)

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------
    def _dispatch(self, tc: ToolCall) -> dict[str, Any]:
        try:
            if tc.name in INSPECTION_TOOL_NAMES:
                return self._dispatch_inspection(tc)
            if tc.name in META_TOOL_NAMES:
                return self._dispatch_meta(tc)
            if tc.name in TOOL_TO_HARNESS_METHOD:
                return self._dispatch_response(tc)
            return _err(f"unknown tool: {tc.name!r}")
        except InvalidResponseError as e:
            return _err(f"InvalidResponse: {e}")
        except HarnessError as e:
            return _err(f"HarnessError: {e}")
        except Exception as e:  # noqa: BLE001
            return _err(f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")

    def _dispatch_inspection(self, tc: ToolCall) -> dict[str, Any]:
        if tc.name == "get_state":
            p = tc.arguments.get("perspective", self.perspective)
            state = build_state(self.harness, self.engine.card_db,
                                perspective=int(p), include_decision=True)
            return _ok(json.dumps(state, default=str))
        if tc.name == "pending_decision":
            from .state import build_decision
            pending = self.harness.pending
            if pending is None:
                return _ok(json.dumps({"pending": None}))
            d = build_decision(pending, self.engine.card_db)
            return _ok(json.dumps(d, default=str))
        if tc.name == "inspect_card":
            code = int(tc.arguments["card_code"])
            info = self.engine.card_db.get(code) or {
                "code": code, "name": "unknown", "note": "not in CardDB",
            }
            return _ok(json.dumps(info, default=str))
        if tc.name == "get_glossary":
            from .core import glossary
            return _ok(json.dumps(glossary(), default=str))
        return _err(f"inspection tool {tc.name!r} not implemented")

    def _dispatch_meta(self, tc: ToolCall) -> dict[str, Any]:
        if tc.name == "restart":
            self.engine.destroy()
            self.harness = Harness(self.engine)
            step = self.harness.start(self.lua_setup)
            payload = {
                "restart_acknowledged": True,
                "tool_calls_used": self._tool_calls_used,
                "tool_calls_remaining": self.max_tool_calls - self._tool_calls_used,
                "events": step.events,
                "note": ("Engine state reset to puzzle initial conditions. "
                         "The next observation reflects fresh state."),
            }
            return {
                "content": json.dumps(payload, default=str),
                "step": step,
            }
        return _err(f"meta tool {tc.name!r} not implemented")

    def _auto_decline_chains_until_real(
        self, all_tool_calls: list[ToolCall], current_idx: int,
    ) -> StepResult | None:
        """Auto-decline optional chain windows between batched actions.

        After a tool call dispatch in a multi-action turn, if the engine
        sits at an OPTIONAL ``select_chain`` window AND the model didn't
        queue a ``select_chain`` action next, transparently respond with
        ``index=null`` so the engine progresses to the next "real"
        decision the model batched for.  Loops in case multiple chain
        windows fire in a row (multi-round chain resolution).

        Forced chains never auto-decline — they always come back to the
        model for an explicit numeric pick.

        Auto-declines do NOT count against ``max_tool_calls`` — they're
        harness-internal, not model decisions.

        Returns the final ``StepResult`` from the last auto-declined
        chain (or ``None`` if no auto-decline fired).
        """
        next_idx = current_idx + 1
        next_tc = (all_tool_calls[next_idx]
                   if next_idx < len(all_tool_calls) else None)
        last_step: StepResult | None = None
        while True:
            pending = self.harness.pending
            if pending is None:
                break
            if pending.msg_name != "MSG_SELECT_CHAIN":
                break
            if getattr(pending.parsed, "forced", False):
                break  # forced chains always go back to the model
            if next_tc is not None and next_tc.name == "select_chain":
                break  # model will handle this window explicitly
            # Auto-decline: respond null to the engine, log, loop.
            try:
                step = self.harness.respond_select_chain(index=None)
            except Exception as e:  # noqa: BLE001
                self._log({
                    "type": "auto_chain_decline_error",
                    "error": f"{type(e).__name__}: {e}",
                })
                break
            self._log({
                "type": "auto_chain_decline",
                "after_tool_use_id": all_tool_calls[current_idx].id,
                "events": step.events[-10:],
                "new_pending": _pending_summary(step.pending),
                "game_over": step.game_over,
            })
            last_step = step
            if step.game_over or self.harness.state.game_over:
                return step
        return last_step

    def _dispatch_response(self, tc: ToolCall) -> dict[str, Any]:
        method_name = TOOL_TO_HARNESS_METHOD[tc.name]
        method = getattr(self.harness, method_name)
        kwargs = coerce_args(tc.name, tc.arguments)
        step: StepResult = method(**kwargs)

        summary = {
            "ok": True,
            "events": step.events[-20:],
            "pending": _pending_summary(step.pending),
            "game_over": step.game_over,
            "winner": step.winner,
            "lp": list(self.harness.state.lp),
            "turn": self.harness.state.turn_count,
            "phase": self.harness.state.phase,
        }
        return {"content": json.dumps(summary, default=str), "step": step}

    # ------------------------------------------------------------------
    def _call_model(self) -> ModelTurn:
        for attempt in range(3):
            try:
                return self.provider.respond(
                    system=self.system_prompt,
                    messages=self._messages,
                    tools=self.tools,
                )
            except Exception as e:  # noqa: BLE001
                self._log({"type": "provider_error",
                           "attempt": attempt + 1, "error": str(e)})
                if attempt == 2:
                    raise
                time.sleep(2 ** attempt)
        raise RuntimeError("unreachable")

    # ------------------------------------------------------------------
    def _outcome(self, termination: str, last_step: StepResult) -> dict[str, Any]:
        from .core import render_win_reason
        raw_reason = self.harness.state.win_reason
        out = {
            "termination": termination,
            "game_over": self.harness.state.game_over,
            "winner": self.harness.state.winner,
            "win_reason": (render_win_reason(raw_reason)
                           if raw_reason is not None else None),
            "win_reason_raw": raw_reason,
            "turn_count": self.harness.state.turn_count,
            "lp": list(self.harness.state.lp),
            "tool_calls_used": self._tool_calls_used,
            "last_events": last_step.events[-20:] if last_step else [],
            "model_usage_totals": dict(self._usage_totals),
        }
        self._log({"type": "outcome", **out})
        if self._log_f is not None:
            self._log_f.close()
        return out

    def _log(self, payload: dict[str, Any]) -> None:
        if self._log_f is None:
            return
        self._log_f.write(json.dumps(payload, default=str) + "\n")


# ---------------------------------------------------------------------------
def _pending_summary(p: PendingDecision | None) -> dict[str, Any] | None:
    if p is None:
        return None
    return {
        "msg_type": p.msg_type,
        "msg_name": p.msg_name,
        "player": p.player,
    }


def _ok(content: str) -> dict[str, Any]:
    return {"content": content, "is_error": False, "step": None}


def _err(msg: str) -> dict[str, Any]:
    return {"content": msg, "is_error": True, "step": None}


# ---------------------------------------------------------------------------
# Convenience entry point
# ---------------------------------------------------------------------------
def run_episode(
    *,
    engine: OCGEngine,
    provider: ToolCallingProvider,
    lua_setup: str,
    instance: dict[str, Any],
    card_db: CardDB,
    perspective: int = 0,
    max_tool_calls: int = 200,
    forage: bool = False,
    show_solution: bool = False,
    log_path: Path | None = None,
) -> dict[str, Any]:
    return Episode(
        engine=engine, provider=provider, lua_setup=lua_setup,
        instance=instance, card_db=card_db,
        perspective=perspective, max_tool_calls=max_tool_calls,
        forage=forage, show_solution=show_solution,
        log_path=log_path,
    ).run()
