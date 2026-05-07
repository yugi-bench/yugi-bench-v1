"""Unit tests for aggregate.

Builds a small synthetic dataset + runs/ tree, exercises every public
aggregator function, and asserts the headline tables match what the
runner.py + engine.replay output schema implies. Pure-Python; no
libocgcore needed.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import sys
from pathlib import Path as _Path
sys.path.insert(0, str(_Path(__file__).resolve().parent.parent / "api-eval"))
import aggregate as agg


# ---------------------------------------------------------------------------
# Synthetic dataset + run tree
# ---------------------------------------------------------------------------
def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")


@pytest.fixture
def dataset(tmp_path: Path) -> Path:
    rows = [
        # Two official-source puzzles at different tiers.
        {"instance_id": "p_a", "metadata": {"source": "Duel Links",
                                            "complexity": "1/10",
                                            "objective": "Win this turn.",
                                            "has_gold_solution": True}},
        {"instance_id": "p_b", "metadata": {"source": "World Championship",
                                            "complexity": "5/10",
                                            "objective": "Win this turn.",
                                            "has_gold_solution": False}},
        # One community-source puzzle.
        {"instance_id": "p_c", "metadata": {"source": "Miscellaneous",
                                            "complexity": "5/10",
                                            "objective": "Win this turn.",
                                            "has_gold_solution": False}},
        # One Uncategorised (falls into "other" partition).
        {"instance_id": "p_d", "metadata": {"source": "Uncategorized",
                                            "complexity": "3/10",
                                            "objective": "Win this turn.",
                                            "has_gold_solution": False}},
    ]
    p = tmp_path / "dataset.jsonl"
    _write_jsonl(p, rows)
    return p


def _msg_win_event(winner: int) -> dict:
    return {"msg_type": 5, "msg_name": "MSG_WIN",
            "winner": winner, "win_reason": "lp_zero"}


def _model_turn(n_tool_calls: int, cumu_input: int, cumu_output: int) -> dict:
    return {
        "type": "model_turn",
        "tool_calls": [{"name": "select_chain", "arguments": {"index": None}}
                       for _ in range(n_tool_calls)],
        "cumulative": {"input_tokens": cumu_input,
                       "output_tokens": cumu_output},
    }


def _tool_result(events: list[dict]) -> dict:
    """A tool_result row whose `content` is the engine response JSON."""
    return {
        "type": "tool_result",
        "content": json.dumps({"events": events}),
    }


@pytest.fixture
def run_dir(tmp_path: Path) -> Path:
    """Synthetic runs/<run-name>/ tree.

    The aggregator independently derives status from the event stream,
    so the fixtures emit ``model_turn`` + ``tool_result`` rows just
    like a real runner.

    p_a wins (MSG_WIN winner=0), p_b stops (final model_turn has no
    tool_calls), p_c crashes (provider_error row), p_d wins.
    Result: 2 wins / 4 puzzles = 50%; official 1 win / 2 = 50%,
    community 0 / 1 = 0%, other 1 / 1 = 100%.
    """
    run = tmp_path / "runs" / "interactive-test"
    run.mkdir(parents=True)

    _write_jsonl(run / "p_a.jsonl", [
        {"type": "config", "provider": "test", "perspective": 0,
         "max_tool_calls": 500},
        _model_turn(12, 1000, 500),
        _tool_result([_msg_win_event(0)]),
        # An outcome row is allowed but never trusted as ground truth.
        {"type": "outcome", "termination": "game_over", "winner": 0,
         "tool_calls_used": 12},
    ])
    _write_jsonl(run / "p_b.jsonl", [
        {"type": "config", "perspective": 0, "max_tool_calls": 500},
        _model_turn(7, 800, 200),
        _tool_result([{"msg_type": 41, "msg_name": "MSG_NEW_PHASE"}]),
        # Final model_turn has zero tool_calls — that's a "stopped".
        {"type": "model_turn", "tool_calls": [],
         "cumulative": {"input_tokens": 800, "output_tokens": 200}},
    ])
    _write_jsonl(run / "p_c.jsonl", [
        {"type": "config", "perspective": 0, "max_tool_calls": 500},
        _model_turn(3, 0, 0),
        {"type": "provider_error", "error": "rate limited"},
    ])
    _write_jsonl(run / "p_d.jsonl", [
        {"type": "config", "perspective": 0, "max_tool_calls": 500},
        _model_turn(20, 2000, 800),
        _tool_result([_msg_win_event(0)]),
    ])
    return run


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
def test_load_dataset_metadata(dataset: Path):
    md = agg.load_dataset_metadata(dataset)
    assert set(md) == {"p_a", "p_b", "p_c", "p_d"}
    assert md["p_a"]["source"] == "Duel Links"
    assert md["p_c"]["source"] == "Miscellaneous"


def test_parse_complexity():
    assert agg._parse_complexity("7/10") == 7
    assert agg._parse_complexity("3/10") == 3
    assert agg._parse_complexity(None) is None
    assert agg._parse_complexity("") is None
    assert agg._parse_complexity("garbage") is None


def test_classify_source_kind():
    assert agg._classify_source_kind("Duel Links") == "official"
    assert agg._classify_source_kind("GX Spirit Caller") == "official"
    assert agg._classify_source_kind("Nightmare Troubadour") == "official"
    assert agg._classify_source_kind("World Championship") == "official"
    assert agg._classify_source_kind("Miscellaneous") == "community"
    assert agg._classify_source_kind("Uncategorized") == "other"
    assert agg._classify_source_kind(None) == "other"


def test_collect_outcomes(dataset: Path, run_dir: Path):
    md = agg.load_dataset_metadata(dataset)
    outcomes = agg.collect_outcomes([run_dir], md)
    assert len(outcomes) == 4
    by_id = {o.instance_id: o for o in outcomes}
    assert by_id["p_a"].status == "win"
    assert by_id["p_a"].source == "Duel Links"
    assert by_id["p_a"].source_kind == "official"
    assert by_id["p_a"].complexity_tier == 1
    assert by_id["p_b"].status == "stopped"
    assert by_id["p_c"].status == "crashed"
    assert by_id["p_d"].status == "win"
    assert by_id["p_d"].source_kind == "other"


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------
def test_headline_counts(dataset: Path, run_dir: Path):
    outcomes = agg.collect_outcomes(
        [run_dir], agg.load_dataset_metadata(dataset))
    h = agg.headline_counts(outcomes)
    assert h["n_puzzles"] == 4
    assert h["wins"] == 2
    assert h["win_rate"] == 0.5
    assert h["by_status"]["win"] == 2
    assert h["by_status"]["stopped"] == 1
    assert h["by_status"]["crashed"] == 1


def test_by_termination(dataset: Path, run_dir: Path):
    outcomes = agg.collect_outcomes(
        [run_dir], agg.load_dataset_metadata(dataset))
    bt = agg.by_termination(outcomes)
    assert bt["game_over"] == 2
    assert bt["model_stopped_without_tool_call"] == 1
    assert bt["exception"] == 1


def test_by_complexity(dataset: Path, run_dir: Path):
    outcomes = agg.collect_outcomes(
        [run_dir], agg.load_dataset_metadata(dataset))
    rows = agg.by_complexity(outcomes)
    by_tier = {r["tier"]: r for r in rows}
    # Tier 1: p_a (win) — 1/1 = 100%
    assert by_tier[1]["n"] == 1
    assert by_tier[1]["wins"] == 1
    assert by_tier[1]["win_rate"] == 1.0
    # Tier 5: p_b (stop) + p_c (crash) — 0/2 = 0%
    assert by_tier[5]["n"] == 2
    assert by_tier[5]["wins"] == 0
    # Tier 3: p_d (win) — 1/1 = 100%
    assert by_tier[3]["wins"] == 1


def test_by_source_partition(dataset: Path, run_dir: Path):
    outcomes = agg.collect_outcomes(
        [run_dir], agg.load_dataset_metadata(dataset))
    bs = agg.by_source(outcomes)

    by_kind = {r["kind"]: r for r in bs["by_kind"]}
    # Official: p_a (win) + p_b (stop) — 1/2 = 50%
    assert by_kind["official"]["n"] == 2
    assert by_kind["official"]["wins"] == 1
    # Community: p_c (crash) — 0/1 = 0%
    assert by_kind["community"]["n"] == 1
    assert by_kind["community"]["wins"] == 0
    # Other: p_d (win) — 1/1 = 100%
    assert by_kind["other"]["n"] == 1
    assert by_kind["other"]["wins"] == 1

    by_src = {r["source"]: r for r in bs["by_source"]}
    assert by_src["Duel Links"]["wins"] == 1
    assert by_src["World Championship"]["wins"] == 0
    assert by_src["Miscellaneous"]["wins"] == 0
    assert by_src["Uncategorized"]["wins"] == 1


def test_usage_rollup(dataset: Path, run_dir: Path):
    outcomes = agg.collect_outcomes(
        [run_dir], agg.load_dataset_metadata(dataset))
    u = agg.usage_rollup(outcomes)
    # p_a 1000 + p_b 800 + p_c (no usage) + p_d 2000 = 3800 input tokens
    assert u["input_tokens"] == 3800
    # p_a 500 + p_b 200 + p_d 800 = 1500 output
    assert u["output_tokens"] == 1500


# ---------------------------------------------------------------------------
# Renderers (round-trip)
# ---------------------------------------------------------------------------
def test_render_text_emits_headline(dataset: Path, run_dir: Path):
    outcomes = agg.collect_outcomes(
        [run_dir], agg.load_dataset_metadata(dataset))
    report = agg.build_report(outcomes, runs=[str(run_dir)], per_puzzle=False)
    out = agg.render_text(report)
    assert "puzzles       : 4" in out
    assert "wins          : 2 (50.0%)" in out
    assert "by termination" in out
    assert "by source kind" in out
    assert "official" in out
    assert "community" in out


def test_render_json_round_trips(dataset: Path, run_dir: Path):
    outcomes = agg.collect_outcomes(
        [run_dir], agg.load_dataset_metadata(dataset))
    report = agg.build_report(outcomes, runs=[str(run_dir)], per_puzzle=True)
    s = agg.render_json(report)
    parsed = json.loads(s)
    assert parsed["headline"]["wins"] == 2
    assert len(parsed["per_puzzle"]) == 4


def test_render_md_includes_per_puzzle(dataset: Path, run_dir: Path):
    outcomes = agg.collect_outcomes(
        [run_dir], agg.load_dataset_metadata(dataset))
    report = agg.build_report(outcomes, runs=[str(run_dir)], per_puzzle=True)
    out = agg.render_md(report)
    assert "## By complexity tier" in out
    assert "## Per puzzle" in out
    assert "p_a" in out


def test_render_csv_per_puzzle(dataset: Path, run_dir: Path):
    outcomes = agg.collect_outcomes(
        [run_dir], agg.load_dataset_metadata(dataset))
    report = agg.build_report(outcomes, runs=[str(run_dir)], per_puzzle=True)
    out = agg.render_csv(report)
    lines = out.strip().splitlines()
    # Header + 4 rows
    assert len(lines) == 5
    assert lines[0].startswith("instance_id,")


# ---------------------------------------------------------------------------
# Resilience
# ---------------------------------------------------------------------------
def test_outcome_missing(tmp_path: Path):
    """A JSONL without an outcome row or any MSG_WIN classifies as no_outcome."""
    run = tmp_path / "runs" / "broken"
    run.mkdir(parents=True)
    _write_jsonl(run / "p_x.jsonl", [
        {"type": "config", "provider": "test", "perspective": 0,
         "max_tool_calls": 500},
        # No outcome row, no model_turn, no MSG_WIN — process killed
        # before any work happened.
    ])
    md = {"p_x": {"source": "Duel Links", "complexity": "1/10"}}
    outcomes = agg.collect_outcomes([run], md)
    assert len(outcomes) == 1
    assert outcomes[0].status == "no_outcome"


def test_malformed_jsonl_line_is_tolerated(tmp_path: Path):
    """A single broken JSONL line shouldn't corrupt the read or skew the
    derivation: the surrounding tool_result with MSG_WIN still wins."""
    run = tmp_path / "runs" / "messy"
    run.mkdir(parents=True)
    p = run / "p_y.jsonl"
    p.write_text(
        '{"type":"config","perspective":0,"max_tool_calls":500}\n'
        'not-json-at-all\n'
        '{"type":"model_turn","tool_calls":[{"name":"x","arguments":{}}]}\n'
        '{"type":"tool_result","content":"{\\"events\\":[{\\"msg_name\\":\\"MSG_WIN\\",\\"winner\\":0}]}"}\n'
    )
    md = {"p_y": {"source": "Duel Links", "complexity": "1/10"}}
    outcomes = agg.collect_outcomes([run], md)
    assert len(outcomes) == 1
    assert outcomes[0].status == "win"


def test_derive_ignores_recorded_outcome_when_disagrees(tmp_path: Path):
    """The aggregator must ignore a misleading recorded outcome row.

    p_z's MSG_WIN says winner=1 (loss for perspective=0), but the
    outcome row claims termination=game_over winner=0 (win). The
    derived status should be 'loss', and has_recorded_mismatch True.
    """
    run = tmp_path / "runs" / "tampered"
    run.mkdir(parents=True)
    _write_jsonl(run / "p_z.jsonl", [
        {"type": "config", "perspective": 0, "max_tool_calls": 500},
        _model_turn(5, 100, 50),
        _tool_result([_msg_win_event(1)]),  # OPPONENT wins
        # Bogus recorded outcome claiming we won.
        {"type": "outcome", "termination": "game_over", "winner": 0,
         "tool_calls_used": 5},
    ])
    md = {"p_z": {"source": "Duel Links", "complexity": "1/10"}}
    outcomes = agg.collect_outcomes([run], md)
    assert len(outcomes) == 1
    assert outcomes[0].status == "loss"
    assert outcomes[0].has_recorded_mismatch is True
    assert outcomes[0].recorded_status == "win"


def test_derive_perspective_1_swaps_win_loss(tmp_path: Path):
    """If config.perspective=1, MSG_WIN winner=1 is a win; winner=0 is a loss."""
    run = tmp_path / "runs" / "p1"
    run.mkdir(parents=True)
    _write_jsonl(run / "p_w.jsonl", [
        {"type": "config", "perspective": 1, "max_tool_calls": 500},
        _model_turn(3, 100, 50),
        _tool_result([_msg_win_event(1)]),
    ])
    md = {"p_w": {"source": "Duel Links", "complexity": "1/10"}}
    outcomes = agg.collect_outcomes([run], md)
    assert outcomes[0].status == "win"
    assert outcomes[0].perspective == 1


def test_derive_exhausted_when_tool_budget_reached(tmp_path: Path):
    """No MSG_WIN + tool_calls cumulatively reach max_tool_calls = exhausted."""
    run = tmp_path / "runs" / "ex"
    run.mkdir(parents=True)
    _write_jsonl(run / "p_e.jsonl", [
        {"type": "config", "perspective": 0, "max_tool_calls": 5},
        _model_turn(5, 1000, 500),
        _tool_result([{"msg_type": 41, "msg_name": "MSG_NEW_PHASE"}]),
    ])
    md = {"p_e": {"source": "Duel Links", "complexity": "1/10"}}
    outcomes = agg.collect_outcomes([run], md)
    assert outcomes[0].status == "exhausted"
