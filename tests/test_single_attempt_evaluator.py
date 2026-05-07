"""SingleAttemptEvaluator edge-case tests — runs against the real engine.

Covers the structural guarantees ``replay_solution`` / ``SingleAttemptEvaluator``
must give callers: empty solutions, unknown tool names, bad args, and the
auto-opponent drain.  Skipped if libocgcore is not available.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

_DYLIB = os.environ.get(
    "YGO_DYLIB",
    str(Path(__file__).resolve().parents[1].parent / "edopro" /
        "ocgcore" / "bin" / "release" / "libocgcore.so"),
)
_DB_DIR = os.environ.get(
    "YGO_DB_DIR",
    str(Path(__file__).resolve().parents[1].parent / "distribution" / "expansions"),
)

pytestmark = pytest.mark.skipif(
    not Path(_DYLIB).exists() or not Path(_DB_DIR).exists(),
    reason=f"requires libocgcore at {_DYLIB} and card DBs at {_DB_DIR}",
)

from engine.replay import (
    EvalResult,
    SingleAttemptEvaluator,
    replay_solution,
)
from engine.core import CardDB, OCGEngine


@pytest.fixture(scope="module")
def evaluator() -> SingleAttemptEvaluator:
    return SingleAttemptEvaluator()


@pytest.fixture(scope="module")
def sample_lua() -> str:
    sample = Path(__file__).resolve().parents[1] / "sample" / "puzzle.lua"
    return sample.read_text()


def test_empty_solution_is_incomplete(evaluator, sample_lua):
    r = evaluator.evaluate_one(sample_lua, [])
    assert isinstance(r, EvalResult)
    assert r.status == "incomplete"
    assert r.steps_applied == 0
    assert r.total_steps == 0
    assert r.pending_after is not None
    assert r.pending_after["player"] == 0


def test_unknown_tool_is_error(evaluator, sample_lua):
    r = evaluator.evaluate_one(
        sample_lua,
        [{"tool": "not_a_real_verb", "args": {}}],
    )
    assert r.status == "error"
    assert "unknown tool" in (r.error or "")
    assert r.failure_step == 0


def test_malformed_args_is_error(evaluator, sample_lua):
    r = evaluator.evaluate_one(
        sample_lua,
        [{"tool": "select_idlecmd", "args": "not-a-dict"}],
    )
    assert r.status == "error"
    assert "must be a dict" in (r.error or "")


def test_missing_args_is_treated_as_empty_dict(evaluator, sample_lua):
    # select_idlecmd with no args should be routed but fail with the harness
    # rejecting (no command chosen).
    r = evaluator.evaluate_one(
        sample_lua,
        [{"tool": "select_idlecmd"}],
    )
    assert r.status == "error"
    # Either HarnessError or missing kwarg — both are fine, just verify
    # it didn't crash with an unhandled exception.


def test_valid_mini_sequence_advances(evaluator):
    """Load the first dataset puzzle and apply a single select_idlecmd — the
    evaluator should consume one step and return either 'incomplete' (if
    the engine survives) or 'error' with a pending_after (if our cmd was
    rejected by the engine).  Either way we should get a valid EvalResult
    with a non-None pending_after when not game_over."""
    dataset = Path(__file__).resolve().parents[1] / "data" / "yugioh_bench.jsonl"
    with open(dataset) as f:
        inst = json.loads(f.readline())
    r = evaluator.evaluate_one(
        inst["lua_setup"],
        [{"tool": "select_idlecmd", "args": {"command": "to_end_phase"}}],
    )
    assert r.status in {"incomplete", "error", "loss"}
    # If the harness accepted to_end_phase, the turn ended → probably loss
    # (opponent's turn, and they have no plays → they go to end phase → we
    # next draw 0 cards = can't satisfy win, so status != win).  The strict
    # invariant: status is a recognized string and steps_applied >= 0.
    assert r.steps_applied >= 0
    assert isinstance(r.lp, list) and len(r.lp) == 2


def test_auto_opponent_true_vs_false_produce_same_shape(evaluator, sample_lua):
    """Toggling auto_opponent should not change the EvalResult's schema."""
    db = CardDB(Path(_DB_DIR))
    for auto in (True, False):
        engine = OCGEngine(Path(_DYLIB), db,
                           Path(os.environ.get("YGO_SCRIPT_DIR",
                                str(Path(__file__).resolve().parents[1].parent
                                    / "distribution" / "script"))),
                           Path(os.environ.get("YGO_CARD_SCRIPT_DIR",
                                str(Path(__file__).resolve().parents[1].parent
                                    / "distribution" / "script" / "official"))))
        try:
            r = replay_solution(engine, sample_lua, [], auto_opponent=auto)
        finally:
            engine.destroy()
        assert isinstance(r, EvalResult)
        assert r.status in {"win", "loss", "incomplete", "error"}


def test_longer_solution_than_stream_stops_with_pending_after(evaluator, sample_lua):
    """Submitting more actions than the engine will consume should still
    return a valid EvalResult describing where we got stuck."""
    r = evaluator.evaluate_one(
        sample_lua,
        [{"tool": "select_idlecmd", "args": {"command": "to_end_phase"}}] * 50,
    )
    # The first to_end_phase probably ends our turn; subsequent prompts
    # will be for other decisions or the turn has ended.  Either way:
    assert r.status in {"incomplete", "error", "loss"}
    assert isinstance(r.lp, list)


def test_tolerant_chains_drops_spurious_null_chain(evaluator):
    """A null select_chain emitted when the engine isn't asking for a
    chain should be silently skipped (tolerant=True default)."""
    dataset = Path(__file__).resolve().parents[1] / "data" / "yugioh_bench.jsonl"
    with open(dataset) as f:
        inst = json.loads(f.readline())
    # First pending is MSG_SELECT_IDLECMD — emit a spurious null chain
    # before the real action.  With tolerant_chains, the null is dropped
    # and we advance as if we'd started with to_end_phase.
    r = evaluator.evaluate_one(
        inst["lua_setup"],
        [
            {"tool": "select_chain",   "args": {"index": None}},
            {"tool": "select_idlecmd", "args": {"command": "to_end_phase"}},
        ],
    )
    # If tolerant had rejected the null, we'd get an error at step 0.
    assert r.status != "error", f"tolerant chain skip failed: {r.error}"
    assert r.steps_applied >= 1


def test_strict_chains_rejects_spurious_null_chain(sample_lua):
    """With tolerant_chains=False, the spurious null-chain is an error."""
    db = CardDB(Path(_DB_DIR))
    engine = OCGEngine(
        Path(_DYLIB), db,
        Path(os.environ.get("YGO_SCRIPT_DIR",
            str(Path(__file__).resolve().parents[1].parent
                / "distribution" / "script"))),
        Path(os.environ.get("YGO_CARD_SCRIPT_DIR",
            str(Path(__file__).resolve().parents[1].parent
                / "distribution" / "script" / "official"))),
    )
    try:
        r = replay_solution(
            engine, sample_lua,
            [{"tool": "select_chain", "args": {"index": None}}],
            tolerant_chains=False,
        )
    finally:
        engine.destroy()
    assert r.status == "error"
    assert r.failure_step == 0
