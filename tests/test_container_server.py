"""End-to-end test for the agent-mcp-eval/server.py MCP dispatcher.

Bypasses the MCP transport layer (no stdio_server, no real MCP client)
and drives ``_handle_tool_call`` directly with the action lists in
``solutions/``. Validates:

  - Bootstrap loads the puzzle from data/yugioh_bench.jsonl, builds the
    rich system prompt, and writes ``config`` + ``start`` JSONL events.
  - ``get_briefing`` returns the system prompt + first observation
    without consuming the tool-call budget.
  - Driving a known winning ``solutions/<id>.json`` reaches
    ``game_over`` with ``winner=0`` and emits the expected JSONL event
    distribution: ``(N actions × 4) + 3`` lines for N actions
    (model_turn + tool_result + state_snapshot + observation per
    action; config + start + outcome bracket the run).
  - Schema parity vs. the API-driven sweep — every event-type's keys
    match what ``runner.py --interactive`` produces.

This runs in-process; ``docker`` is NOT required. The build smoke
(``docker build`` + ``docker run -i ...``) needs a host with docker
and is verified separately.
"""

from __future__ import annotations

# Load agent-mcp-eval/server.py directly — the directory has a hyphen so
# it isn't importable as a package; tests/conftest.py only puts src/ on path.
import importlib.util as _ilu
import json
import sys as _sys
from collections import Counter
from pathlib import Path
from pathlib import Path as _Path

import pytest

_AME = _Path(__file__).resolve().parent.parent / "agent-mcp-eval"
_spec = _ilu.spec_from_file_location("agent_mcp_eval_server", _AME / "server.py")
S = _ilu.module_from_spec(_spec)
_sys.modules["agent_mcp_eval_server"] = S
_spec.loader.exec_module(S)


REPO_ROOT = Path(__file__).resolve().parent.parent
SOLUTIONS_DIR = REPO_ROOT / "solutions"


def _engine_assets_present() -> bool:
    """Skip these tests when libocgcore + card DB + scripts aren't reachable.

    engine.core picks the assets from /app/vendor/ in the container, from
    repo_root/vendor/ via setup.sh, or from env-var overrides. CI without
    any of those should skip cleanly.
    """
    try:
        from engine.core import CARD_SCRIPT_DIR, DB_DIR, DYLIB_PATH, SCRIPT_DIR

        return all(Path(p).exists() for p in (DYLIB_PATH, DB_DIR, SCRIPT_DIR, CARD_SCRIPT_DIR))
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _engine_assets_present(),
    reason="libocgcore + card DB + scripts not installed (run setup.sh, "
    "or use the container image, or set YGO_DYLIB / YGO_DB_DIR / "
    "YGO_SCRIPT_DIR env vars)",
)


def _make_state(puzzle_id: str, tmp_path: Path, max_tool_calls: int = 500):
    """Boot the server in a tmp results dir; caller cleans up."""
    config = S.ServerConfig(
        puzzle_id=puzzle_id,
        dataset_path=S._resolve_dataset(S._REPO_ROOT, None),
        results_dir=tmp_path,
        max_tool_calls=max_tool_calls,
    )
    return S._bootstrap(config)


def _teardown(state) -> None:
    try:
        state.log_fh.close()
    except Exception:
        pass
    try:
        state.engine.destroy()
    except Exception:
        pass


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_bootstrap_emits_config_and_start(tmp_path: Path) -> None:
    state = _make_state("yugioh_puzzle_42ffb7a8", tmp_path)
    try:
        events = _read_jsonl(state.log_path)
        assert [e["type"] for e in events] == ["config", "start"]
        cfg = events[0]
        assert cfg["forage"] is False
        assert cfg["show_solution"] is False
        assert cfg["max_tool_calls"] == 500
        assert "system_prompt" in cfg and len(cfg["system_prompt"]) > 1000
        assert isinstance(cfg["tools"], list) and len(cfg["tools"]) > 20
        names = {t["name"] for t in cfg["tools"]}
        # Default-mode tool surface: get_briefing, restart, three inspection,
        # twenty response verbs.
        assert "get_briefing" in names
        assert "inspect_card" not in names  # forage-only, must not leak
        assert "restart" in names
        assert "select_idlecmd" in names
        assert events[1]["pending"]["msg_name"] == "MSG_SELECT_IDLECMD"
    finally:
        _teardown(state)


def test_get_briefing_does_not_consume_budget(tmp_path: Path) -> None:
    state = _make_state("yugioh_puzzle_42ffb7a8", tmp_path, max_tool_calls=10)
    try:
        out_text = S._handle_tool_call(state, "get_briefing", {})
        out = json.loads(out_text)
        assert out["puzzle_id"] == "yugioh_puzzle_42ffb7a8"
        assert "system_prompt" in out
        assert "first_observation" in out
        assert state.tool_calls_used == 0  # not consumed
        # Idempotent: a second call returns the same payload.
        out2 = json.loads(S._handle_tool_call(state, "get_briefing", {}))
        assert out["puzzle_id"] == out2["puzzle_id"]
        assert state.tool_calls_used == 0
    finally:
        _teardown(state)


def test_winning_solution_reaches_game_over(tmp_path: Path) -> None:
    """Drive a known-winning solution and confirm winner=0 + schema parity."""
    sol_id = "yugioh_puzzle_044c693a"  # 39-action win from the DeepSeek sweep
    sol_path = SOLUTIONS_DIR / f"{sol_id}.json"
    if not sol_path.exists():
        pytest.skip(f"solution fixture missing: {sol_path}")
    actions = json.loads(sol_path.read_text())

    state = _make_state(sol_id, tmp_path)
    try:
        # Agent's idiomatic first call.
        S._handle_tool_call(state, "get_briefing", {})
        for step in actions:
            S._handle_tool_call(state, step["tool"], step["args"])
            if state.terminated:
                break
        assert state.terminated, "expected episode to terminate"
        assert state.last_outcome is not None
        assert state.last_outcome["termination"] == "game_over"
        assert state.last_outcome["winner"] == 0
        # All actions consumed.
        assert state.tool_calls_used == len(actions)
    finally:
        _teardown(state)

    # Re-open the JSONL and check the event distribution + schema parity.
    events = _read_jsonl(tmp_path / f"{sol_id}.jsonl")
    counts = Counter(e["type"] for e in events)
    n = len(actions)
    assert counts["config"] == 1
    assert counts["start"] == 1
    assert counts["outcome"] == 1
    assert counts["model_turn"] == n
    assert counts["tool_result"] == n
    assert counts["state_snapshot"] == n
    assert counts["observation"] == n
    # Total = config + start + 4N + outcome
    assert sum(counts.values()) == 4 * n + 3

    # Schema parity vs. the API-driven sweep.
    expected_keys = {
        "config": {
            "type",
            "perspective",
            "max_tool_calls",
            "forage",
            "show_solution",
            "system_prompt",
            "tools",
            "provider",
        },
        "start": {"type", "events", "pending"},
        "model_turn": {
            "type",
            "text",
            "tool_calls",
            "stop_reason",
            "provider_data",
            "usage",
            "elapsed_seconds",
            "cumulative",
            "response_headers",
        },
        "tool_result": {"type", "tool_use_id", "name", "arguments", "content", "is_error"},
        "state_snapshot": {"type", "after_tool", "after_tool_use_id", "is_error", "state"},
        "observation": {"type", "content"},
    }
    for ev in events:
        if ev["type"] not in expected_keys:
            continue
        assert expected_keys[ev["type"]].issubset(set(ev.keys())), (
            f"event {ev['type']} missing keys: {expected_keys[ev['type']] - set(ev.keys())}"
        )


def test_unknown_tool_returns_error_without_advancing(tmp_path: Path) -> None:
    state = _make_state("yugioh_puzzle_42ffb7a8", tmp_path)
    try:
        S._handle_tool_call(state, "get_briefing", {})
        out = json.loads(S._handle_tool_call(state, "this_tool_does_not_exist", {}))
        assert out.get("is_error", out.get("ok", True) is False) or "unknown tool" in str(out)
    finally:
        _teardown(state)


def test_budget_exhaustion_emits_outcome(tmp_path: Path) -> None:
    """When the agent burns through the tool-call budget, the server emits
    a tool_budget_exhausted outcome and refuses further dispatch."""
    state = _make_state("yugioh_puzzle_42ffb7a8", tmp_path, max_tool_calls=2)
    try:
        # First two calls eat the budget (each consumes one).
        S._handle_tool_call(state, "pending_decision", {})
        S._handle_tool_call(state, "pending_decision", {})
        # Third call must be refused.
        out = json.loads(S._handle_tool_call(state, "pending_decision", {}))
        assert out.get("is_error") is True or out.get("ok") is False
        assert state.last_outcome is not None
        assert state.last_outcome["termination"] == "tool_budget_exhausted"
    finally:
        _teardown(state)
