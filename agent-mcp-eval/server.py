"""MCP server — yugi-bench environment as an MCP-over-stdio surface.

Architecture
============

Per-puzzle process. Each container is invoked as::

    python agent-mcp-eval/server.py --puzzle yugioh_puzzle_<id> [--results-dir /work/results]

It loads the instance from ``data/yugioh_bench.jsonl``, starts the harness on
the puzzle's lua_setup, builds the rich initial briefing via
``lib.prompt_builder.build_interactive_system_prompt``, and exposes:

  - 20 engine response verbs (the ``select_*`` / ``announce_*`` / ``sort_card`` /
    ``rock_paper_scissors`` family — one per ``Harness.respond_*`` method)
  - ``restart`` (rebuild the harness from the puzzle's initial state)
  - ``get_state``, ``pending_decision``, ``get_glossary`` (always-on inspection)
  - ``get_briefing`` (returns the rich initial system prompt + first
    observation; the agent calls this first to know what it's playing)

This is the **default-mode** tool set — bit-identical to the configuration the
2026-05-03 DeepSeek 220-puzzle sweep ran under. ``inspect_card`` (forage-only)
is intentionally excluded; ``--show-solution`` is intentionally NOT a tool —
the agent never gets oracle access.

The MCP tool result for any engine-mutating call is the full post-action
state-render — same content the existing API-driven Episode loop sends as the
next user observation. The agent therefore needs zero extra inspection calls
in the steady state.

JSONL output
============

The container writes one ``<puzzle_id>.jsonl`` to ``--results-dir`` mirroring
the schema the API-driven sweep produces — ``config`` / ``start`` /
``observation`` / ``model_turn`` / ``tool_result`` / ``state_snapshot`` /
``outcome``. ``api-eval/extract_actions.py`` and
``engine/replay.py`` work unchanged on the result.

``model_turn`` events are emitted with empty ``text`` / ``usage`` /
``response_headers`` (the server has no visibility into the agent's inner
state); ``tool_calls`` is populated with the single call that triggered the
turn. Cost analysis tools that read ``model_usage_totals`` will see zeros —
honest, since the model is external.

Chain auto-decline
==================

The Episode loop in ``engine/episode.py`` auto-declines optional
``MSG_SELECT_CHAIN`` windows when a model batches multiple actions in one turn
(commit ``84af38e``). The MCP-as-environment model sees one tool call at a
time over the wire — there's no batched turn. We replicate the semantics by
auto-declining at the *next* call's entry: if the engine sits at an optional
``select_chain`` and the agent's incoming call is not ``select_chain``, the
server transparently dispatches ``select_chain([])`` first, logs an
``auto_chain_decline`` event, then dispatches the agent's call against the
post-decline state. Forced chains never auto-decline.

Result delivery
===============

When the engine reaches ``game_over`` after a tool call, the tool result
contains the full post-action state plus a ``"game_over": true,
"winner": N`` block. The server writes the ``outcome`` JSONL line and exits
cleanly so the docker container terminates and the driver can collect.
Subsequent tool calls (if the agent doesn't disconnect) return an error.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --- Repo-root resolution -----------------------------------------------------
# The container bakes the flat-layout repo at /app, but for local non-docker
# tests we run from the repo root. Add the repo root to sys.path so absolute
# imports like ``engine.core`` work regardless of cwd.
_THIS_FILE = Path(__file__).resolve()
_REPO_ROOT = _THIS_FILE.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from engine.core import CardDB, OCGEngine, glossary  # noqa: E402
from engine.harness import Harness  # noqa: E402
from engine.replay import _auto_advance_opponent  # noqa: E402, WPS437
from engine.state import build_decision, build_state  # noqa: E402
from engine.tools import (  # noqa: E402
    ALWAYS_AVAILABLE_INSPECTION,
    META_TOOL_NAMES,
    TOOL_TO_HARNESS_METHOD,
    TOOLS,
    coerce_args,
)

# ---------------------------------------------------------------------------
# Configuration + state
# ---------------------------------------------------------------------------

# Default tool budget — matches the 2026-05-03 sweep's max_tool_calls=500.
DEFAULT_MAX_TOOL_CALLS = 500
DEFAULT_PERSPECTIVE = 0


@dataclass
class ServerConfig:
    puzzle_id: str
    dataset_path: Path
    results_dir: Path
    max_tool_calls: int = DEFAULT_MAX_TOOL_CALLS
    perspective: int = DEFAULT_PERSPECTIVE
    # Drain opponent-side decisions automatically after each agent
    # tool call. Mirrors engine.replay's auto_opponent=True default.
    # Off by default so the agent's view matches the live API-driven
    # Episode loop exactly (model handles every decision); turn ON
    # for replay-mode driver runs and for agents that don't want to
    # be bothered with opponent-side responses.
    auto_opponent: bool = False


@dataclass
class ServerState:
    """Mutable state carried across MCP tool calls."""

    config: ServerConfig
    instance: dict[str, Any]
    card_db: CardDB
    engine: OCGEngine
    harness: Harness
    system_prompt: str
    initial_pending: dict[str, Any] | None
    log_path: Path
    log_fh: Any  # file handle
    start_wallclock: float = field(default_factory=time.time)
    tool_calls_used: int = 0
    terminated: bool = False
    last_outcome: dict[str, Any] | None = None
    auto_decline_count: int = 0


# ---------------------------------------------------------------------------
# Asset discovery — mirror engine.core's behaviour for non-container runs
# ---------------------------------------------------------------------------


def _resolve_dataset(repo_root: Path, override: str | None) -> Path:
    if override:
        return Path(override)
    # In the container the dataset lives at /app/data/yugioh_bench.jsonl.
    # In local repos it lives at repo_root/data/yugioh_bench.jsonl.
    return repo_root / "data" / "yugioh_bench.jsonl"


def _resolve_results_dir(override: str | None) -> Path:
    if override:
        d = Path(override)
    else:
        # /work/results is the convention we'll bind-mount into the container.
        # Fall back to ./results-mcp/ for local non-container runs.
        d = Path("/work/results") if Path("/work").is_dir() else Path("./results-mcp")
    d.mkdir(parents=True, exist_ok=True)
    return d


def _load_instance(dataset_path: Path, puzzle_id: str) -> dict[str, Any]:
    if not dataset_path.exists():
        raise FileNotFoundError(f"dataset missing at {dataset_path}")
    with open(dataset_path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            if d.get("instance_id") == puzzle_id:
                return d
    raise KeyError(f"puzzle {puzzle_id!r} not found in {dataset_path}; check the id")


# ---------------------------------------------------------------------------
# JSONL logging
# ---------------------------------------------------------------------------


def _log(state: ServerState, event: dict[str, Any]) -> None:
    state.log_fh.write(json.dumps(event, default=str) + "\n")
    state.log_fh.flush()


def _pending_summary(pending: Any) -> dict[str, Any] | None:
    if pending is None:
        return None
    return {
        "msg_name": pending.msg_name,
        "responder": getattr(pending, "responder", None),
        "player": getattr(pending, "player", None),
    }


def _new_tool_use_id() -> str:
    return f"mcp_{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Tool registry — default mode (no inspect_card, no show_solution)
# ---------------------------------------------------------------------------


def _build_briefing_tool() -> dict[str, Any]:
    return {
        "name": "get_briefing",
        "description": (
            "Return the puzzle briefing: rich system prompt (preamble, rules, "
            "full omniscient state, full card glossary, action grammar) plus "
            "the first observation. Call this first before any other tool — "
            "it tells you what puzzle you are playing and what the initial "
            "state is. Idempotent; safe to call again."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    }


def _container_tool_set() -> list[dict[str, Any]]:
    """Default-mode tool list: drop inspect_card, drop forage-only tools.

    Adds get_briefing as the agent's bootstrap tool.
    """
    keep: list[dict[str, Any]] = [_build_briefing_tool()]
    for t in TOOLS:
        name = t["name"]
        # Drop forage-only inspection (inspect_card).
        if (
            name not in ALWAYS_AVAILABLE_INSPECTION
            and name not in TOOL_TO_HARNESS_METHOD
            and name not in META_TOOL_NAMES
        ):
            continue
        keep.append(t)
    return keep


# ---------------------------------------------------------------------------
# Existing-log classification — non-destructive handling of re-runs.
#
# The container is one-shot per `docker run --rm` invocation, so every
# time a fresh agent session connects (codex/claude reopens the puzzle),
# a new container starts with the same results-dir bind-mount.  Without
# this guard, opening the JSONL log file in "w" mode would truncate any
# prior run's results.  Instead:
#   - "complete" (has outcome event)  → refuse to overwrite, exit 0
#   - "partial"  (events but no outcome) → archive under a timestamped
#                                          .partial-<ts>.jsonl name and
#                                          start fresh
#   - "corrupt"  (couldn't parse)     → same as partial
#   - "fresh"    (missing or empty)   → proceed normally
# ---------------------------------------------------------------------------


def _classify_existing_log(log_path: Path) -> str:
    if not log_path.exists():
        return "fresh"
    try:
        size = log_path.stat().st_size
    except OSError:
        return "corrupt"
    if size == 0:
        return "fresh"
    try:
        with open(log_path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except Exception:
                    return "corrupt"
                if event.get("type") == "outcome":
                    return "complete"
        return "partial"
    except Exception:
        return "corrupt"


def _handle_existing_log(log_path: Path, puzzle_id: str) -> bool:
    """Apply the non-destructive policy to an existing log file.

    Returns True if the caller should proceed with a fresh run, False
    if the caller should exit cleanly (preserve the existing complete
    log untouched).
    """
    classification = _classify_existing_log(log_path)
    if classification == "fresh":
        return True
    if classification == "complete":
        sys.stderr.write(
            f"[yugi-bench-container] {puzzle_id}: existing JSONL has a final "
            f"outcome event; refusing to overwrite. Move or delete "
            f"{log_path} if you want to re-attempt this puzzle.\n"
        )
        return False
    # partial or corrupt — archive and proceed fresh
    ts = time.strftime("%Y%m%dT%H%M%S", time.localtime())
    backup = log_path.with_name(f"{log_path.stem}.{classification}-{ts}.jsonl")
    try:
        log_path.rename(backup)
        sys.stderr.write(
            f"[yugi-bench-container] {puzzle_id}: archived {classification} "
            f"JSONL to {backup.name} before fresh run.\n"
        )
    except OSError as e:
        sys.stderr.write(
            f"[yugi-bench-container] {puzzle_id}: could not archive existing "
            f"JSONL ({e}); aborting to preserve data.\n"
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Bootstrap — load puzzle, start engine, build briefing
# ---------------------------------------------------------------------------


def _bootstrap(config: ServerConfig) -> ServerState:
    instance = _load_instance(config.dataset_path, config.puzzle_id)

    # Asset paths come from engine.core's defaults (which honour env-var
    # overrides) — we don't pin them here so the container's COPY-baked
    # /app/vendor/ tree just works without env vars.
    from engine.core import (
        CARD_SCRIPT_DIR,
        DB_DIR,
        DYLIB_PATH,
        SCRIPT_DIR,
    )

    card_db = CardDB(Path(DB_DIR))
    engine = OCGEngine(
        Path(DYLIB_PATH),
        card_db,
        Path(SCRIPT_DIR),
        Path(CARD_SCRIPT_DIR),
    )
    harness = Harness(engine)
    initial_step = harness.start(instance["lua_setup"])

    # Auto-opponent: drain any opponent-side decisions queued at puzzle init
    # so the agent's first observation always reflects a player-perspective
    # pending decision. Mirrors engine.replay's behaviour at line 204.
    if config.auto_opponent and not initial_step.game_over:
        _auto_advance_opponent(harness, config.perspective)

    from lib.prompt_builder import build_interactive_system_prompt

    system_prompt = build_interactive_system_prompt(
        instance=instance,
        card_db=card_db,
        harness=harness,
        forage=False,
        show_solution=False,
    )

    log_path = config.results_dir / f"{config.puzzle_id}.jsonl"
    log_fh = open(log_path, "w")
    tool_set = _container_tool_set()

    state = ServerState(
        config=config,
        instance=instance,
        card_db=card_db,
        engine=engine,
        harness=harness,
        system_prompt=system_prompt,
        initial_pending=_pending_summary(initial_step.pending),
        log_path=log_path,
        log_fh=log_fh,
    )

    _log(
        state,
        {
            "type": "config",
            "perspective": config.perspective,
            "max_tool_calls": config.max_tool_calls,
            "forage": False,
            "show_solution": False,
            "system_prompt": system_prompt,
            "tools": tool_set,
            "provider": {"name": "mcp-stdio", "model": "external", "container": True},
        },
    )
    _log(
        state,
        {
            "type": "start",
            "events": initial_step.events,
            "pending": _pending_summary(initial_step.pending),
        },
    )

    if initial_step.game_over:
        # Pathological: puzzle starts in a terminal state.
        _emit_outcome(state, "game_over_before_first_decision", initial_step)
    return state


# ---------------------------------------------------------------------------
# Dispatch — same shape as Episode._dispatch_*, locally re-implemented
# ---------------------------------------------------------------------------


def _ok(content: str) -> dict[str, Any]:
    return {"content": content, "is_error": False}


def _err(msg: str) -> dict[str, Any]:
    return {"content": msg, "is_error": True}


def _full_observation_json(state: ServerState, events: list[dict] | None = None) -> str:
    snap = build_state(
        state.harness,
        state.card_db,
        perspective=state.config.perspective,
        include_decision=True,
        events=events or [],
    )
    return json.dumps(snap, default=str)


def _dispatch_inspection(state: ServerState, name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "get_state":
        p = int(args.get("perspective", state.config.perspective))
        return _ok(
            _full_observation_json(state)
            if p == state.config.perspective
            else json.dumps(
                build_state(state.harness, state.card_db, perspective=p, include_decision=True),
                default=str,
            )
        )
    if name == "pending_decision":
        pending = state.harness.pending
        if pending is None:
            return _ok(json.dumps({"pending": None}))
        return _ok(json.dumps(build_decision(pending, state.card_db), default=str))
    if name == "get_glossary":
        return _ok(json.dumps(glossary(), default=str))
    return _err(f"inspection tool {name!r} not implemented")


def _dispatch_meta(state: ServerState, name: str) -> dict[str, Any]:
    if name == "restart":
        state.engine.destroy()
        # Recreate the engine + harness from scratch on the same lua_setup.
        from engine.core import (
            CARD_SCRIPT_DIR,
            DYLIB_PATH,
            SCRIPT_DIR,
        )

        state.engine = OCGEngine(
            Path(DYLIB_PATH),
            state.card_db,
            Path(SCRIPT_DIR),
            Path(CARD_SCRIPT_DIR),
        )
        state.harness = Harness(state.engine)
        step = state.harness.start(state.instance["lua_setup"])
        if state.config.auto_opponent and not step.game_over:
            _auto_advance_opponent(state.harness, state.config.perspective)
        payload = {
            "restart_acknowledged": True,
            "tool_calls_used": state.tool_calls_used,
            "tool_calls_remaining": state.config.max_tool_calls - state.tool_calls_used,
            "events": step.events,
            "note": (
                "Engine state reset to puzzle initial conditions. "
                "The next observation reflects fresh state."
            ),
        }
        return {"content": json.dumps(payload, default=str), "step": step}
    return _err(f"meta tool {name!r} not implemented")


def _dispatch_response(state: ServerState, name: str, args: dict[str, Any]) -> dict[str, Any]:
    method_name = TOOL_TO_HARNESS_METHOD[name]
    method = getattr(state.harness, method_name)
    kwargs = coerce_args(name, args)
    step = method(**kwargs)
    summary = {
        "ok": True,
        "events": step.events[-20:],
        "pending": _pending_summary(step.pending),
        "game_over": step.game_over,
        "winner": step.winner,
        "lp": list(state.harness.state.lp),
        "turn": state.harness.state.turn_count,
        "phase": state.harness.state.phase,
    }
    return {"content": json.dumps(summary, default=str), "step": step}


def _maybe_auto_decline(state: ServerState, incoming_tool_name: str) -> None:
    """Auto-decline optional select_chain windows before dispatching the
    agent's incoming call.

    Mirrors Episode._auto_decline_chains_until_real semantics, fired at the
    next call's entry rather than between batched tool_calls in one turn.
    Forced chains never auto-decline. Auto-declines do NOT consume from
    the agent's tool-call budget.
    """
    while True:
        pending = state.harness.pending
        if pending is None:
            return
        if pending.msg_name != "MSG_SELECT_CHAIN":
            return
        if getattr(pending.parsed, "forced", False):
            return  # forced chains always require an explicit numeric pick
        if incoming_tool_name == "select_chain":
            return  # agent will handle this window explicitly
        try:
            step = state.harness.respond_select_chain(index=None)
        except Exception as e:  # noqa: BLE001
            _log(
                state,
                {
                    "type": "auto_chain_decline_error",
                    "error": f"{type(e).__name__}: {e}",
                },
            )
            return
        # Drain opponent decisions surfaced by the chain resolution before
        # the next loop iteration's pending check (mirrors engine.replay
        # line 270-271).
        if state.config.auto_opponent and not state.harness.state.game_over:
            _auto_advance_opponent(state.harness, state.config.perspective)
        state.auto_decline_count += 1
        _log(
            state,
            {
                "type": "auto_chain_decline",
                "before_tool": incoming_tool_name,
                "events": step.events[-10:],
                "new_pending": _pending_summary(step.pending),
                "game_over": step.game_over,
            },
        )
        if step.game_over or state.harness.state.game_over:
            _emit_outcome(state, "game_over", step)
            return


# ---------------------------------------------------------------------------
# MCP request handling — the single tool-call entry point
# ---------------------------------------------------------------------------


def _handle_tool_call(state: ServerState, name: str, arguments: dict[str, Any]) -> str:
    """Process one MCP tool call. Returns the textual content the MCP client
    receives back. Side-effect: writes JSONL events.
    """
    if state.terminated:
        return json.dumps(
            {
                "ok": False,
                "is_error": True,
                "error": "episode terminated; container will exit shortly",
                "outcome": state.last_outcome,
            },
            default=str,
        )

    # 0. Bootstrap-only tool: get_briefing returns the system prompt + first
    #    observation without consuming budget or advancing state.
    if name == "get_briefing":
        first_obs = _full_observation_json(state)
        payload = {
            "system_prompt": state.system_prompt,
            "first_observation": json.loads(first_obs),
            "max_tool_calls": state.config.max_tool_calls,
            "tool_calls_remaining": state.config.max_tool_calls - state.tool_calls_used,
            "puzzle_id": state.config.puzzle_id,
            "note": "Call this once at start. Inspection tools (get_state, "
            "pending_decision, get_glossary) are also free.",
        }
        return json.dumps(payload, default=str)

    # 1. Budget gate. Inspection + restart still count, mirroring the
    #    behaviour of the existing JSONL config (max_tool_calls=500 caps
    #    every tool the agent calls).
    if state.tool_calls_used >= state.config.max_tool_calls:
        if state.last_outcome is None:
            _emit_outcome(state, "tool_budget_exhausted", None)
        return json.dumps(
            {
                "ok": False,
                "is_error": True,
                "error": (
                    f"tool budget exhausted ({state.tool_calls_used}/{state.config.max_tool_calls})"
                ),
            }
        )

    # 2. Auto-decline pending optional chain windows BEFORE dispatching the
    #    agent's incoming response/inspection/meta tool.
    _maybe_auto_decline(state, name)
    if state.terminated:  # auto-decline pushed us into game_over
        return json.dumps(
            {
                "ok": False,
                "is_error": True,
                "error": "auto-chain-decline ended the episode",
                "outcome": state.last_outcome,
            },
            default=str,
        )

    # 2b. Tolerant null-chain smoothing — mirrors engine.replay Case 1
    #     (replay.py:246-250). If the agent submits select_chain(index=None)
    #     but the engine isn't actually at a chain window (because we
    #     auto-declined it, or it auto-resolved), the explicit null-decline
    #     is a no-op. Skip it so the agent's solution-extracted action
    #     sequence stays aligned with the live engine path. Required for
    #     bit-identity with engine.replay --solutions.
    if (
        name == "select_chain"
        and isinstance(arguments, dict)
        and arguments.get("index") is None
        and state.harness.pending is not None
        and state.harness.pending.msg_name != "MSG_SELECT_CHAIN"
    ):
        _log(
            state,
            {
                "type": "tolerant_null_chain_skip",
                "before_tool": name,
                "current_pending": _pending_summary(state.harness.pending),
            },
        )
        # Return a benign no-op result; do not consume budget.
        return json.dumps(
            {
                "ok": True,
                "skipped": True,
                "reason": (
                    "select_chain(null) submitted while engine is not at "
                    "MSG_SELECT_CHAIN; treated as no-op (tolerant-replay "
                    "rule mirroring engine.replay)"
                ),
                "current_pending": _pending_summary(state.harness.pending),
            }
        )

    # 3. Dispatch by tool category, mirroring engine/episode.py's _dispatch.
    tool_use_id = _new_tool_use_id()
    state.tool_calls_used += 1
    dispatch_started = time.time()
    try:
        if name in ALWAYS_AVAILABLE_INSPECTION:
            result = _dispatch_inspection(state, name, arguments)
        elif name in META_TOOL_NAMES:
            result = _dispatch_meta(state, name)
        elif name in TOOL_TO_HARNESS_METHOD:
            result = _dispatch_response(state, name, arguments)
        else:
            result = _err(f"unknown tool: {name!r}")
    except Exception as e:  # noqa: BLE001
        result = _err(f"{type(e).__name__}: {e}\n{traceback.format_exc(limit=3)}")

    # 3b. Drain opponent-side decisions before snapshotting state so the
    #     agent's next observation always reflects a player-perspective
    #     pending. Mirrors engine.replay's line 327 hook. No-op when
    #     pending is already player-side, when game_over, or when
    #     auto_opponent is off.
    if (
        state.config.auto_opponent
        and not result.get("is_error")
        and not state.harness.state.game_over
    ):
        try:
            _auto_advance_opponent(state.harness, state.config.perspective)
        except Exception as e:  # noqa: BLE001
            _log(
                state,
                {
                    "type": "auto_opponent_error",
                    "error": f"{type(e).__name__}: {e}",
                },
            )

    elapsed = time.time() - dispatch_started

    # 4. Emit the JSONL trio: model_turn (synthetic), tool_result, state_snapshot.
    _log(
        state,
        {
            "type": "model_turn",
            "text": "",
            "tool_calls": [{"id": tool_use_id, "name": name, "arguments": arguments}],
            "stop_reason": "tool_use",
            "provider_data": {},
            "usage": {},
            "elapsed_seconds": round(elapsed, 3),
            "cumulative": {},
            "response_headers": {},
        },
    )
    _log(
        state,
        {
            "type": "tool_result",
            "tool_use_id": tool_use_id,
            "name": name,
            "arguments": arguments,
            "content": result["content"],
            "is_error": result.get("is_error", False),
        },
    )
    try:
        if state.harness._started and state.engine.duel:
            snap = build_state(
                state.harness,
                state.card_db,
                perspective=state.config.perspective,
                include_decision=True,
            )
            _log(
                state,
                {
                    "type": "state_snapshot",
                    "after_tool": name,
                    "after_tool_use_id": tool_use_id,
                    "is_error": result.get("is_error", False),
                    "state": snap,
                },
            )
    except Exception as _e:  # noqa: BLE001
        _log(
            state,
            {
                "type": "state_snapshot",
                "after_tool": name,
                "error": f"{type(_e).__name__}: {_e}",
            },
        )

    # 5. The agent's actual tool result content: full observation post-action
    #    (for engine-mutating tools) or the tool's own payload (inspection/meta).
    step = result.get("step")
    if step is not None and not result.get("is_error"):
        # Engine-mutating tool succeeded — return the full state-render the
        # agent would have received as its next user observation in the
        # API-driven Episode loop.
        full_obs = _full_observation_json(state, events=step.events)
        full_obs_payload = json.loads(full_obs)
        full_obs_payload["_action_summary"] = json.loads(result["content"])
        if step.game_over or state.harness.state.game_over:
            _emit_outcome(state, "game_over", step)
            full_obs_payload["_outcome"] = state.last_outcome
        # Also write the post-action observation event for log parity with
        # the API-driven sweep.
        _log(state, {"type": "observation", "content": json.dumps(full_obs_payload, default=str)})
        return json.dumps(full_obs_payload, default=str)
    # Inspection / meta / errors: return the dispatcher's content. Errors are
    # wrapped as JSON so the agent always has a uniform parse target — the
    # API-driven Episode loop sometimes returns plain-string errors but the
    # MCP surface aims for one shape.
    if result.get("is_error"):
        return json.dumps(
            {
                "ok": False,
                "is_error": True,
                "tool": name,
                "error": result["content"],
            }
        )
    return result["content"]


def _terminal_chain_drain(state: ServerState) -> None:
    """Drain any optional MSG_SELECT_CHAIN windows the engine is sitting at
    when the agent has stopped sending tool calls.

    Mirrors engine.replay lines 337-349: after the action loop exits, optional
    chain prompts (battle-damage step triggers, end-of-turn effects, etc) may
    still be open. Declining them lets the engine resolve to game_over for
    runs that should have won. Without this, perfectly valid solutions can
    report 'incomplete' (or in our case 'agent_disconnected') merely because
    the agent didn't keep the connection open long enough for one or two more
    auto-declines to fire.
    """
    drained = 0
    while (
        not state.harness.state.game_over
        and state.harness.pending is not None
        and state.harness.pending.msg_name == "MSG_SELECT_CHAIN"
        and not getattr(state.harness.pending.parsed, "forced", False)
    ):
        try:
            step = state.harness.respond_select_chain(index=None)
        except Exception as e:  # noqa: BLE001
            _log(
                state,
                {
                    "type": "terminal_chain_drain_error",
                    "error": f"{type(e).__name__}: {e}",
                },
            )
            break
        drained += 1
        if state.config.auto_opponent and not state.harness.state.game_over:
            try:
                _auto_advance_opponent(state.harness, state.config.perspective)
            except Exception as e:  # noqa: BLE001
                _log(
                    state,
                    {
                        "type": "auto_opponent_error",
                        "error": f"{type(e).__name__}: {e}",
                    },
                )
                break
        if step.game_over or state.harness.state.game_over:
            break
    if drained:
        _log(
            state,
            {
                "type": "terminal_chain_drain",
                "drained": drained,
                "game_over": state.harness.state.game_over,
                "winner": state.harness.state.winner,
            },
        )


def _emit_outcome(state: ServerState, termination: str, step: Any) -> None:
    if state.last_outcome is not None:
        return
    last_events = []
    if step is not None and getattr(step, "events", None):
        last_events = list(step.events)[-20:]
    s = state.harness.state
    win_reason_raw = getattr(s, "win_reason", None)
    outcome = {
        "type": "outcome",
        "termination": termination,
        "game_over": s.game_over,
        "winner": s.winner,
        "win_reason": str(win_reason_raw) if win_reason_raw is not None else None,
        "win_reason_raw": win_reason_raw,
        "turn_count": s.turn_count,
        "lp": list(s.lp),
        "tool_calls_used": state.tool_calls_used,
        "auto_decline_count": state.auto_decline_count,
        "wallclock_seconds": round(time.time() - state.start_wallclock, 3),
        "last_events": last_events,
        "model_usage_totals": {},
    }
    _log(state, outcome)
    state.last_outcome = outcome
    state.terminated = True


# ---------------------------------------------------------------------------
# MCP server bootstrap
# ---------------------------------------------------------------------------


async def _run_mcp(state: ServerState) -> None:
    """Run the MCP-over-stdio server until the agent disconnects or the
    episode terminates.

    The official `mcp` Python SDK is the transport. We register two handlers:
    ``list_tools`` (return the default-mode tool list) and ``call_tool``
    (dispatch via ``_handle_tool_call``).
    """
    from mcp.server import Server
    from mcp.server.stdio import stdio_server
    from mcp.types import TextContent, Tool

    app = Server("yugi-bench-env")

    tool_set = _container_tool_set()
    mcp_tools = [
        Tool(
            name=t["name"],
            description=t["description"],
            inputSchema=t["input_schema"],
        )
        for t in tool_set
    ]

    @app.list_tools()
    async def list_tools() -> list[Tool]:  # noqa: D401
        return mcp_tools

    @app.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        text = _handle_tool_call(state, name, arguments or {})
        return [TextContent(type="text", text=text)]

    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> ServerConfig:
    p = argparse.ArgumentParser(
        prog="agent-mcp-eval.server",
        description="yugi-bench MCP environment — one container, one puzzle.",
    )
    p.add_argument(
        "--puzzle", required=True, help="Puzzle instance_id (e.g. yugioh_puzzle_42ffb7a8)."
    )
    p.add_argument(
        "--dataset",
        default=None,
        help="Path to yugioh_bench.jsonl. Defaults to /app/data/ "
        "in the container or ./data/ locally.",
    )
    p.add_argument(
        "--results-dir",
        default=None,
        help="Directory for per-puzzle JSONL output. Defaults to "
        "/work/results in the container or ./results-mcp/ locally.",
    )
    p.add_argument("--max-tool-calls", type=int, default=DEFAULT_MAX_TOOL_CALLS)
    p.add_argument("--perspective", type=int, default=DEFAULT_PERSPECTIVE, choices=[0, 1])
    p.add_argument(
        "--auto-opponent",
        action="store_true",
        help="Drain opponent-side decisions automatically after "
        "each agent tool call (mirrors engine.replay's "
        "auto_opponent=True default). Off by default so the "
        "agent's view matches the live API-driven Episode "
        "loop exactly. Turn ON for replay-mode driver runs.",
    )
    args = p.parse_args(argv)

    repo_root = Path(os.environ.get("YGO_BENCH_ROOT", str(_REPO_ROOT)))
    return ServerConfig(
        puzzle_id=args.puzzle,
        dataset_path=_resolve_dataset(repo_root, args.dataset),
        results_dir=_resolve_results_dir(args.results_dir),
        max_tool_calls=args.max_tool_calls,
        perspective=args.perspective,
        auto_opponent=args.auto_opponent,
    )


def main(argv: list[str] | None = None) -> int:
    config = _parse_args(argv)
    log_path = config.results_dir / f"{config.puzzle_id}.jsonl"
    if not _handle_existing_log(log_path, config.puzzle_id):
        # Existing complete results preserved; exit before the expensive
        # engine bootstrap so the container shuts down quickly.
        return 0
    state = _bootstrap(config)
    try:
        asyncio.run(_run_mcp(state))
    except KeyboardInterrupt:
        pass
    finally:
        # Flush any pending outcome (e.g. agent disconnected mid-puzzle).
        # Before declaring the agent disconnected, drain any optional
        # chain windows the engine might still be sitting at — those can
        # close out a winning attack sequence whose final auto-declines
        # never had a chance to fire because the agent stopped sending
        # tool calls (see _terminal_chain_drain docstring).
        if state.last_outcome is None:
            _terminal_chain_drain(state)
            if state.harness.state.game_over and state.last_outcome is None:
                _emit_outcome(state, "game_over", None)
            elif state.last_outcome is None:
                _emit_outcome(state, "agent_disconnected", None)
        try:
            state.log_fh.close()
        except Exception:
            pass
        try:
            state.engine.destroy()
        except Exception:
            pass
    # Exit code: 0 on win, 1 on loss/draw, 2 on incomplete (no game_over reached).
    out = state.last_outcome or {}
    if out.get("termination") == "game_over":
        return 0 if out.get("winner") == 0 else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
