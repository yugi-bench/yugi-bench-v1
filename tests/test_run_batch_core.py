"""Unit tests for agent-mcp-eval/_lib/run_batch_core.py.

Pure-Python — mocks subprocess and HTTP, doesn't actually run codex /
claude / docker.  Covers:

  - WorkspaceState.has_outcome detection (the agent-agnostic
    "is-this-puzzle-done" signal that powers idempotency)
  - discover_workspaces filtering by agent
  - detect_rate_limit pattern matching
  - usage_preflight_pause_seconds across input shapes
  - run_one wraps subprocess.run + classifies result
  - End-to-end main() with mocked subprocess + workspaces:
    * skips done workspaces
    * runs pending workspaces
    * concurrency dispatches to ThreadPoolExecutor
    * rate-limit retry semantics
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Make agent-mcp-eval/_lib importable.  The dir is hyphenated so
# Python can't import it as a package; we sys.path-insert the _lib/
# directly.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT / "agent-mcp-eval" / "_lib"))

import run_batch_core as rbc  # noqa: E402

# ---------------------------------------------------------------------------
# Workspace fixtures
# ---------------------------------------------------------------------------


def _make_workspace(
    runs_root: Path,
    puzzle_id: str,
    agent: str,
    *,
    with_outcome: bool = False,
    with_partial: bool = False,
    with_launcher: bool = True,
) -> Path:
    """Build a synthetic workspace under runs_root that looks like one
    that prep-session.sh would have made."""
    suffix = "-codex" if agent == "codex" else ""
    ws = runs_root / f"{puzzle_id}-2026-01-01T00-00-00{suffix}"
    ws.mkdir(parents=True)
    (ws / "results").mkdir()
    (ws / "metadata.json").write_text(
        json.dumps(
            {
                "puzzle_id": puzzle_id,
                "agent": agent,
                "started_at": "2026-01-01T00-00-00",
            }
        )
    )
    if with_outcome:
        (ws / "results" / f"{puzzle_id}.jsonl").write_text(
            '{"type":"config"}\n'
            '{"type":"start"}\n'
            '{"type":"outcome","termination":"game_over","winner":0}\n'
        )
    elif with_partial:
        (ws / "results" / f"{puzzle_id}.jsonl").write_text('{"type":"config"}\n{"type":"start"}\n')
    if with_launcher:
        launcher = ws / f"run-{agent}-exec.sh"
        launcher.write_text("#!/bin/sh\nexit 0\n")
        launcher.chmod(0o755)
    return ws


# ---------------------------------------------------------------------------
# WorkspaceState.has_outcome
# ---------------------------------------------------------------------------


def test_has_outcome_true_when_outcome_event_present(tmp_path):
    ws = _make_workspace(tmp_path, "yugioh_puzzle_aaa", "codex", with_outcome=True)
    state = rbc.WorkspaceState(
        workspace=ws,
        metadata={"puzzle_id": "yugioh_puzzle_aaa"},
        puzzle_id="yugioh_puzzle_aaa",
        agent="codex",
        jsonl=ws / "results" / "yugioh_puzzle_aaa.jsonl",
        launcher=ws / "run-codex-exec.sh",
    )
    assert state.has_outcome


def test_has_outcome_false_when_partial(tmp_path):
    ws = _make_workspace(tmp_path, "yugioh_puzzle_bbb", "codex", with_partial=True)
    state = rbc.WorkspaceState(
        workspace=ws,
        metadata={"puzzle_id": "yugioh_puzzle_bbb"},
        puzzle_id="yugioh_puzzle_bbb",
        agent="codex",
        jsonl=ws / "results" / "yugioh_puzzle_bbb.jsonl",
        launcher=ws / "run-codex-exec.sh",
    )
    assert not state.has_outcome


def test_has_outcome_false_when_jsonl_missing(tmp_path):
    ws = _make_workspace(tmp_path, "yugioh_puzzle_ccc", "codex")
    state = rbc.WorkspaceState(
        workspace=ws,
        metadata={"puzzle_id": "yugioh_puzzle_ccc"},
        puzzle_id="yugioh_puzzle_ccc",
        agent="codex",
        jsonl=ws / "results" / "yugioh_puzzle_ccc.jsonl",
        launcher=ws / "run-codex-exec.sh",
    )
    assert not state.has_outcome


def test_has_outcome_tolerates_garbage_lines(tmp_path):
    """A corrupt line in the JSONL shouldn't break detection if a valid
    outcome event appears later."""
    ws = _make_workspace(tmp_path, "yugioh_puzzle_ddd", "codex")
    (ws / "results" / "yugioh_puzzle_ddd.jsonl").write_text(
        '{"type":"config"}\nnot-json-garbage\n{"type":"outcome","winner":0}\n'
    )
    state = rbc.WorkspaceState(
        workspace=ws,
        metadata={"puzzle_id": "yugioh_puzzle_ddd"},
        puzzle_id="yugioh_puzzle_ddd",
        agent="codex",
        jsonl=ws / "results" / "yugioh_puzzle_ddd.jsonl",
        launcher=ws / "run-codex-exec.sh",
    )
    assert state.has_outcome


# ---------------------------------------------------------------------------
# discover_workspaces filtering
# ---------------------------------------------------------------------------


def test_discover_workspaces_filters_by_agent(tmp_path):
    _make_workspace(tmp_path, "yugioh_puzzle_x1", "codex")
    _make_workspace(tmp_path, "yugioh_puzzle_x2", "codex", with_outcome=True)
    _make_workspace(tmp_path, "yugioh_puzzle_y1", "claude")
    _make_workspace(tmp_path, "yugioh_puzzle_y2", "claude", with_outcome=True)

    codex_states = rbc.discover_workspaces(tmp_path, "codex")
    assert len(codex_states) == 2
    assert all(s.agent == "codex" for s in codex_states)
    assert {s.puzzle_id for s in codex_states} == {"yugioh_puzzle_x1", "yugioh_puzzle_x2"}

    claude_states = rbc.discover_workspaces(tmp_path, "claude")
    assert len(claude_states) == 2
    assert all(s.agent == "claude" for s in claude_states)


def test_discover_workspaces_skips_dirs_without_metadata(tmp_path):
    _make_workspace(tmp_path, "yugioh_puzzle_x1", "codex")
    (tmp_path / "stray-dir").mkdir()  # no metadata.json
    states = rbc.discover_workspaces(tmp_path, "codex")
    assert len(states) == 1


def test_discover_workspaces_handles_missing_runs_root(tmp_path):
    states = rbc.discover_workspaces(tmp_path / "does-not-exist", "codex")
    assert states == []


def test_discover_workspaces_sorts_verified_first_then_complexity(tmp_path):
    """Run order: verified puzzles first (easiest -> hardest), then non-
    verified (easiest -> hardest), tie-break by puzzle id (= content
    hash).  Picks four real puzzle ids from the dataset that span both
    tiers, prep workspaces in reverse-dataset order, and verify the
    sort puts them in priority order."""
    import json

    dataset_path = _REPO_ROOT / "data" / "yugioh_bench.jsonl"
    verified_path = _REPO_ROOT / "data" / "yugioh_bench_verified.jsonl"
    if not dataset_path.exists():
        pytest.skip("dataset missing — sort relies on dataset metadata")

    verified_ids = set()
    if verified_path.exists():
        for line in open(verified_path):
            line = line.strip()
            if line:
                verified_ids.add(json.loads(line)["instance_id"])

    # Pick the easiest verified + easiest non-verified for reliable
    # cross-tier ordering verification.  These are stable across
    # rebuilds (puzzle_id is the content hash).
    pid_v_easy = "yugioh_puzzle_2a4cb9f1"  # verified, complexity 1
    pid_n_easy = "yugioh_puzzle_13b4a7a8"  # non-verified, complexity 1
    pid_v_hard = "yugioh_puzzle_1dd1df41"  # verified, complexity 10 (hardest verified)
    assert pid_v_easy in verified_ids
    assert pid_n_easy not in verified_ids
    assert pid_v_hard in verified_ids

    # Prep workspaces in REVERSE priority order so we can detect
    # whether discover_workspaces actually re-sorts them.
    _make_workspace(tmp_path, pid_n_easy, "codex")
    _make_workspace(tmp_path, pid_v_hard, "codex")
    _make_workspace(tmp_path, pid_v_easy, "codex")

    states = rbc.discover_workspaces(tmp_path, "codex")
    ids_in_order = [s.puzzle_id for s in states]
    # Verified easy -> verified hard -> non-verified easy.
    assert ids_in_order == [pid_v_easy, pid_v_hard, pid_n_easy], (
        f"Expected verified-first by complexity ascending, got {ids_in_order}"
    )


# ---------------------------------------------------------------------------
# detect_rate_limit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("rate limit exceeded", True),
        ("Rate Limit", True),
        ("HTTP 429 Too Many Requests", True),
        ("Quota exceeded for org", True),
        ("you have been rate-limited", True),
        ("5h window cap reached", True),
        ("7-day window quota", True),
        ("usage limit hit", True),
        ("please try again later", True),
        ("normal completion", False),
        ("connection refused", False),
        ("session ended cleanly with outcome event", False),
    ],
)
def test_detect_rate_limit(text: str, expected: bool):
    assert rbc.detect_rate_limit(text, "") == expected
    # Same patterns in stderr should also fire.
    assert rbc.detect_rate_limit("", text) == expected


# ---------------------------------------------------------------------------
# usage_preflight_pause_seconds
# ---------------------------------------------------------------------------


def test_preflight_below_threshold_returns_none():
    usage = {
        "five_hour": {"utilization": 50.0, "resets_at": "2099-01-01T00:00:00+00:00"},
        "seven_day": {"utilization": 30.0, "resets_at": "2099-01-01T00:00:00+00:00"},
    }
    assert rbc.usage_preflight_pause_seconds(usage, 80, 95, 3600) is None


def test_preflight_5h_over_threshold_pauses():
    usage = {
        "five_hour": {"utilization": 90.0, "resets_at": "2099-01-01T00:00:00+00:00"},
        "seven_day": {"utilization": 30.0, "resets_at": "2099-01-01T00:00:00+00:00"},
    }
    result = rbc.usage_preflight_pause_seconds(usage, 80, 95, 3600)
    assert result is not None
    pause, reason = result
    assert pause > 0
    assert "five_hour" in reason and "90.0" in reason


def test_preflight_7d_over_threshold_pauses():
    usage = {
        "five_hour": {"utilization": 50.0, "resets_at": "2099-01-01T00:00:00+00:00"},
        "seven_day": {"utilization": 99.0, "resets_at": "2099-01-01T00:00:00+00:00"},
    }
    result = rbc.usage_preflight_pause_seconds(usage, 80, 95, 3600)
    assert result is not None
    pause, reason = result
    assert pause > 0
    assert "seven_day" in reason and "99.0" in reason


def test_preflight_pause_capped_by_max():
    """Reset is far in the future; pause should be capped by cap_pause_seconds."""
    usage = {
        "five_hour": {"utilization": 100.0, "resets_at": "2099-01-01T00:00:00+00:00"},
    }
    result = rbc.usage_preflight_pause_seconds(usage, 80, 95, 60)
    assert result is not None
    pause, _ = result
    assert pause == 60.0


def test_preflight_missing_utilization_skipped():
    usage = {"five_hour": {"resets_at": "2099-01-01T00:00:00+00:00"}}
    assert rbc.usage_preflight_pause_seconds(usage, 80, 95, 3600) is None


# ---------------------------------------------------------------------------
# extract_usage_from_log — token-count parsing across both agents
# ---------------------------------------------------------------------------


def test_extract_usage_claude_stream_json_result_event():
    """Claude --print --output-format=stream-json --verbose emits one
    JSON object per line.  The final 'result'-shaped event carries
    cumulative usage (input_tokens + cache_*_input_tokens summed)."""
    stdout = "\n".join(
        [
            '{"type":"system","subtype":"init","model":"claude-opus-4-7"}',
            '{"type":"assistant","message":{"role":"assistant","usage":{"input_tokens":50,"output_tokens":120,"cache_creation_input_tokens":2000,"cache_read_input_tokens":0}}}',
            '{"type":"user","message":{"role":"user"}}',
            '{"type":"assistant","message":{"role":"assistant","usage":{"input_tokens":30,"output_tokens":60,"cache_creation_input_tokens":0,"cache_read_input_tokens":2120}}}',
            '{"type":"result","usage":{"input_tokens":80,"output_tokens":180,"cache_creation_input_tokens":2000,"cache_read_input_tokens":2120}}',
        ]
    )
    u = rbc.extract_usage_from_log(stdout)
    assert u == {"in": 80 + 2000 + 2120, "out": 180, "total": 80 + 2000 + 2120 + 180}


def test_extract_usage_codex_json_prompt_completion():
    """Codex --json: schema undocumented, but 'prompt_tokens' /
    'completion_tokens' is the OpenAI-API convention.  Track latest."""
    stdout = "\n".join(
        [
            '{"type":"started","model":"gpt-5.5"}',
            '{"type":"agent_message","usage":{"prompt_tokens":1000,"completion_tokens":500}}',
            '{"type":"task_complete","usage":{"prompt_tokens":3500,"completion_tokens":1800}}',
        ]
    )
    u = rbc.extract_usage_from_log(stdout)
    assert u == {"in": 3500, "out": 1800, "total": 5300}


def test_extract_usage_returns_none_when_no_json():
    """Non-JSON output (e.g. fallback text-mode launcher) yields None
    so callers print without tokens — no regression."""
    stdout = "OpenAI Codex v0.128.0\n--------\nuser\nstart\ncodex\n...output text\n"
    assert rbc.extract_usage_from_log(stdout) is None


def test_extract_usage_returns_none_when_json_lacks_usage():
    """Parseable JSON without any usage-shaped sub-dict → None."""
    stdout = '{"type":"started"}\n{"type":"completed","exit_code":0}\n'
    assert rbc.extract_usage_from_log(stdout) is None


def test_extract_usage_handles_partial_input_only():
    """Some events report only one side; helper fills missing with 0."""
    stdout = '{"type":"r","usage":{"input_tokens":42}}\n'
    u = rbc.extract_usage_from_log(stdout)
    assert u == {"in": 42, "out": 0, "total": 42}


def test_extract_usage_skips_corrupt_lines():
    """A non-JSON line in the middle of valid events doesn't kill
    parsing; the helper recovers and uses the latest valid event."""
    stdout = "\n".join(
        [
            '{"type":"a","usage":{"input_tokens":10,"output_tokens":5}}',
            "this is not json",
            '{"type":"b","usage":{"input_tokens":20,"output_tokens":15}}',
        ]
    )
    u = rbc.extract_usage_from_log(stdout)
    assert u == {"in": 20, "out": 15, "total": 35}


# ---------------------------------------------------------------------------
# run_one wrapping subprocess.run
# ---------------------------------------------------------------------------


def test_run_one_success_when_outcome_present(tmp_path):
    ws = _make_workspace(tmp_path, "yugioh_puzzle_z1", "codex", with_outcome=True)
    state = rbc.WorkspaceState(
        workspace=ws,
        metadata={"puzzle_id": "yugioh_puzzle_z1"},
        puzzle_id="yugioh_puzzle_z1",
        agent="codex",
        jsonl=ws / "results" / "yugioh_puzzle_z1.jsonl",
        launcher=ws / "run-codex-exec.sh",
    )
    with patch("run_batch_core.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        ok, rate_limited, summary, _, _, _ = rbc.run_one(state, 60)
    assert ok is True
    assert rate_limited is False
    assert "outcome event present" in summary


def test_run_one_rate_limited_pattern(tmp_path):
    ws = _make_workspace(tmp_path, "yugioh_puzzle_z2", "codex")  # no outcome
    state = rbc.WorkspaceState(
        workspace=ws,
        metadata={"puzzle_id": "yugioh_puzzle_z2"},
        puzzle_id="yugioh_puzzle_z2",
        agent="codex",
        jsonl=ws / "results" / "yugioh_puzzle_z2.jsonl",
        launcher=ws / "run-codex-exec.sh",
    )
    with patch("run_batch_core.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="HTTP 429 rate limit",
        )
        ok, rate_limited, summary, _, _, _ = rbc.run_one(state, 60)
    assert ok is False
    assert rate_limited is True
    assert "rate-limited" in summary


def test_run_one_failure_no_outcome(tmp_path):
    ws = _make_workspace(tmp_path, "yugioh_puzzle_z3", "codex")  # no outcome
    state = rbc.WorkspaceState(
        workspace=ws,
        metadata={"puzzle_id": "yugioh_puzzle_z3"},
        puzzle_id="yugioh_puzzle_z3",
        agent="codex",
        jsonl=ws / "results" / "yugioh_puzzle_z3.jsonl",
        launcher=ws / "run-codex-exec.sh",
    )
    with patch("run_batch_core.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="some other error")
        ok, rate_limited, summary, _, _, _ = rbc.run_one(state, 60)
    assert ok is False
    assert rate_limited is False
    assert "no outcome event" in summary


# ---------------------------------------------------------------------------
# Resilience: a single bad workspace must not kill the batch
# ---------------------------------------------------------------------------


def test_detect_rate_limit_skips_informational_stream_json_event():
    """claude --print --output-format=stream-json emits
    {"type":"rate_limit_event","rate_limit_info":{"status":"allowed",...}}
    on every session as informational metadata.  The literal substrings
    `rate_limit` / `rateLimit` would match the regex.  Empirically
    (2026-05-07 batch 2), this caused 0816c364 to spin in a 15-attempt
    retry loop because every retry's informational rate-limit-event
    falsely matched.  Pin: status="allowed" must NOT be flagged."""
    stdout = "\n".join(
        [
            '{"type":"system","subtype":"init"}',
            '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1778138400,"rateLimitType":"five_hour","overageStatus":"ok"}}',
            '{"type":"assistant","message":{"content":[{"type":"text","text":"playing puzzle"}]}}',
            '{"type":"result","subtype":"success","is_error":false,"result":"WIN"}',
        ]
    )
    assert rbc.detect_rate_limit(stdout, "") is False


def test_detect_rate_limit_flags_actual_denial():
    """When rate_limit_event has status='denied' that's a real rate-limit
    signal — must flag.  `overageStatus` is intentionally NOT consulted
    (see the next test for why)."""
    stdout_denied = '{"type":"rate_limit_event","rate_limit_info":{"status":"denied"}}'
    assert rbc.detect_rate_limit(stdout_denied, "") is True
    stdout_exceeded = '{"type":"rate_limit_event","rate_limit_info":{"status":"exceeded"}}'
    assert rbc.detect_rate_limit(stdout_exceeded, "") is True


def test_detect_rate_limit_does_not_flag_overage_disabled_org():
    """When the user's org has overage disabled, every rate_limit_event
    carries `overageStatus:"rejected"` along with `overageDisabledReason:
    "org_level_disabled"` — even when the request itself succeeded
    (`status:"allowed"`).  Empirically (2026-05-07 batch 3): this misled
    an earlier version of detect_rate_limit into firing a 1hr global
    pause on workspace 2a4cb9f1, even though that workspace had just
    WON.  Pin: status="allowed" with overageStatus="rejected" must NOT
    flag.  Only the top-level `status` field reflects the request
    outcome."""
    stdout = '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","resetsAt":1778138400,"rateLimitType":"five_hour","overageStatus":"rejected","overageDisabledReason":"org_level_disabled","isUsingOverage":false}}'
    assert rbc.detect_rate_limit(stdout, "") is False


def test_detect_rate_limit_flags_human_readable_text_in_stderr():
    """CLI exit messages like 'You've hit your limit · resets 7am' should
    fire on the stderr regex fallback even when no JSON is present."""
    assert rbc.detect_rate_limit("", "Error: 429 Too Many Requests") is True
    assert rbc.detect_rate_limit("", "rate-limited; please try again later") is True
    assert rbc.detect_rate_limit("", "5h window quota exceeded") is True


def test_detect_rate_limit_flags_result_event_with_error_text():
    """The final result event when is_error=true and the result text
    mentions rate-limiting (e.g. terminal_reason='blocking_limit'
    happens when context fills, but a real rate-limit error would
    surface as is_error=true with text mentioning 429 / quota / etc.)."""
    stdout = '{"type":"result","subtype":"success","is_error":true,"result":"429 Too Many Requests · please try again later"}'
    assert rbc.detect_rate_limit(stdout, "") is True


def test_detect_rate_limit_does_not_flag_agent_text_mentioning_rate():
    """Agent's own thinking/text content might say 'damage rate' or
    'attack rate' — these aren't in JSON event lines that get parsed
    structurally, but if they ARE inside an assistant.text content,
    the JSON-line detector skips that LINE entirely so the regex
    doesn't see it.  Validate."""
    stdout = "\n".join(
        [
            '{"type":"assistant","message":{"content":[{"type":"text","text":"I will increase the damage rate this turn"}]}}',
            '{"type":"result","subtype":"success","is_error":false,"result":"WIN"}',
        ]
    )
    # The line is JSON, so the structural pass skips it (it's not a
    # rate_limit_event), and the regex fallback strips the JSON line.
    assert rbc.detect_rate_limit(stdout, "") is False


def test_install_signal_handler_sets_drain_flag(monkeypatch):
    """SIGINT/SIGTERM flips the drain flag; second signal forces exit.
    Use monkeypatch to avoid actually interrupting pytest."""
    rbc._drain_requested = False  # reset module-global
    rbc.install_signal_handler()
    # Simulate first signal — should set drain
    handler = signal.getsignal(signal.SIGINT)
    assert callable(handler)
    handler(signal.SIGINT, None)
    assert rbc.is_drain_requested() is True
    # Reset for next test
    rbc._drain_requested = False


def test_run_one_timeout_decodes_bytes_and_does_not_raise(tmp_path):
    """Python stdlib quirk: subprocess.TimeoutExpired's .stdout / .stderr
    are raw byte buffers even when subprocess.run was called with
    text=True (the text-mode decode only fires on successful completion,
    not on the timeout-error path).  Empirically (2026-05-07 batch 4):
    `<bytes>.startswith("{")` inside extract_usage_from_log raised
    `TypeError: startswith first arg must be bytes or a tuple of bytes,
    not str` and got caught as a generic worker exception, marking 3
    workspaces FAILED even though one had a valid game_over outcome.
    Pin: TimeoutExpired with bytes stdout/stderr must round-trip cleanly."""
    ws = _make_workspace(tmp_path, "yugioh_puzzle_zt", "codex")
    state = rbc.WorkspaceState(
        workspace=ws,
        metadata={"puzzle_id": "yugioh_puzzle_zt"},
        puzzle_id="yugioh_puzzle_zt",
        agent="codex",
        jsonl=ws / "results" / "yugioh_puzzle_zt.jsonl",
        launcher=ws / "run-codex-exec.sh",
    )
    bytes_stdout = (
        b'{"type":"system","subtype":"init"}\n'
        b'{"type":"assistant","message":{"content":[{"type":"text","text":"thinking"}]}}\n'
    )
    bytes_stderr = b"some warning\n"
    timeout_exc = subprocess.TimeoutExpired(
        cmd="run-codex-exec.sh",
        timeout=60,
        output=bytes_stdout,
        stderr=bytes_stderr,
    )
    with patch("run_batch_core.subprocess.run") as mock_run:
        mock_run.side_effect = timeout_exc
        ok, rate_limited, summary, so, se, _ = rbc.run_one(state, 60)
    assert ok is False
    assert rate_limited is False
    assert "timeout after 60s" in summary
    assert isinstance(so, str) and "thinking" in so
    assert isinstance(se, str) and "warning" in se


def test_run_one_timeout_with_outcome_counts_as_success(tmp_path):
    """When subprocess hits the wallclock timeout BUT the runner inside
    the launcher already wrote a game_over outcome event before being
    killed, the workspace's puzzle did finish — count as success.
    Empirically (2026-05-07 batch 4): 42ffb7a8 reached game_over (loss)
    before the launcher's claude --print process timed out at 1800s,
    but run-batch flagged it as a worker-exception failure instead of
    DONE-with-loss.  Pin: state.has_outcome must be re-checked on the
    timeout path."""
    ws = _make_workspace(tmp_path, "yugioh_puzzle_zo", "codex", with_outcome=True)
    state = rbc.WorkspaceState(
        workspace=ws,
        metadata={"puzzle_id": "yugioh_puzzle_zo"},
        puzzle_id="yugioh_puzzle_zo",
        agent="codex",
        jsonl=ws / "results" / "yugioh_puzzle_zo.jsonl",
        launcher=ws / "run-codex-exec.sh",
    )
    timeout_exc = subprocess.TimeoutExpired(
        cmd="run-codex-exec.sh",
        timeout=60,
        output=b"partial stdout before kill\n",
        stderr=b"",
    )
    with patch("run_batch_core.subprocess.run") as mock_run:
        mock_run.side_effect = timeout_exc
        ok, rate_limited, summary, _, _, _ = rbc.run_one(state, 60)
    assert ok is True
    assert rate_limited is False
    assert "outcome event present" in summary
    assert "timeout" in summary  # still note the timeout in the summary


def test_to_text_helper():
    """_to_text coerces None / bytes / str → str cleanly."""
    assert rbc._to_text(None) == ""
    assert rbc._to_text("") == ""
    assert rbc._to_text("already a string") == "already a string"
    assert rbc._to_text(b"") == ""
    assert rbc._to_text(b"hello \xe4\xb8\x96\xe7\x95\x8c") == "hello 世界"
    # Invalid utf-8 must not raise — fall back to replacement char.
    assert "�" in rbc._to_text(b"\xff\xfe\xfd")


def test_extract_usage_accepts_bytes_input():
    """Belt-and-braces: extract_usage_from_log decodes bytes stdout."""
    bytes_stdout = b'{"type":"result","usage":{"input_tokens":42,"output_tokens":7}}\n'
    out = rbc.extract_usage_from_log(bytes_stdout)
    assert out == {"in": 42, "out": 7, "total": 49}


def test_detect_rate_limit_accepts_bytes_input():
    """Belt-and-braces: detect_rate_limit decodes bytes stdout/stderr."""
    bytes_stdout = b'{"type":"rate_limit_event","rate_limit_info":{"status":"denied"}}'
    assert rbc.detect_rate_limit(bytes_stdout, b"") is True


def test_run_one_handles_missing_launcher(tmp_path):
    """FileNotFoundError on subprocess.run (e.g. workspace deleted
    between discovery and launch, or launcher script removed) gets
    folded into a (False, False, ...) result instead of raising."""
    ws = _make_workspace(tmp_path, "yugioh_puzzle_zm", "codex")
    state = rbc.WorkspaceState(
        workspace=ws,
        metadata={"puzzle_id": "yugioh_puzzle_zm"},
        puzzle_id="yugioh_puzzle_zm",
        agent="codex",
        jsonl=ws / "results" / "yugioh_puzzle_zm.jsonl",
        launcher=ws / "run-codex-exec.sh",
    )
    with patch("run_batch_core.subprocess.run") as mock_run:
        mock_run.side_effect = FileNotFoundError(
            2, "No such file or directory", "run-codex-exec.sh"
        )
        ok, rate_limited, summary, _, _, _ = rbc.run_one(state, 60)
    assert ok is False
    assert rate_limited is False
    assert "launcher unrunnable" in summary
    assert "FileNotFoundError" in summary


def test_run_one_handles_permission_error(tmp_path):
    ws = _make_workspace(tmp_path, "yugioh_puzzle_zp", "codex")
    state = rbc.WorkspaceState(
        workspace=ws,
        metadata={"puzzle_id": "yugioh_puzzle_zp"},
        puzzle_id="yugioh_puzzle_zp",
        agent="codex",
        jsonl=ws / "results" / "yugioh_puzzle_zp.jsonl",
        launcher=ws / "run-codex-exec.sh",
    )
    with patch("run_batch_core.subprocess.run") as mock_run:
        mock_run.side_effect = PermissionError(13, "Permission denied")
        ok, rate_limited, summary, _, _, _ = rbc.run_one(state, 60)
    assert ok is False
    assert rate_limited is False
    assert "launcher unrunnable" in summary
    assert "PermissionError" in summary


def test_run_one_handles_unexpected_exception(tmp_path):
    """A wholly-unexpected exception class also gets caught and folded
    into a failed result via the defensive Exception handler."""
    ws = _make_workspace(tmp_path, "yugioh_puzzle_zu", "codex")
    state = rbc.WorkspaceState(
        workspace=ws,
        metadata={"puzzle_id": "yugioh_puzzle_zu"},
        puzzle_id="yugioh_puzzle_zu",
        agent="codex",
        jsonl=ws / "results" / "yugioh_puzzle_zu.jsonl",
        launcher=ws / "run-codex-exec.sh",
    )
    with patch("run_batch_core.subprocess.run") as mock_run:
        mock_run.side_effect = RuntimeError("disk gnomes ate the launcher")
        ok, rate_limited, summary, _, _, _ = rbc.run_one(state, 60)
    assert ok is False
    assert rate_limited is False
    assert "launcher error" in summary
    assert "RuntimeError" in summary


def test_process_workspace_never_raises(tmp_path, capsys):
    """An exception thrown anywhere inside the worker (outside run_one)
    must still be caught by process_workspace's outer try/except so the
    surrounding batch loop sees a normal return.  Failure is counted."""
    ws = _make_workspace(tmp_path, "yugioh_puzzle_zw", "codex")
    state = rbc.WorkspaceState(
        workspace=ws,
        metadata={"puzzle_id": "yugioh_puzzle_zw"},
        puzzle_id="yugioh_puzzle_zw",
        agent="codex",
        jsonl=ws / "results" / "yugioh_puzzle_zw.jsonl",
        launcher=ws / "run-codex-exec.sh",
    )
    args = type(
        "Args",
        (),
        {
            "no_preflight": True,
            "per_session_timeout_seconds": 60,
            "rate_limit_pause_seconds": 3600,
            "max_rate_limit_retries": 5,
            "five_hour_stop_pct": 80,
            "seven_day_stop_pct": 95,
        },
    )()
    batch = rbc.BatchState(agent="codex", args=args)
    # Force run_one to raise (bypasses its own catch-all).
    with patch("run_batch_core.run_one", side_effect=ValueError("kaboom")):
        rbc.process_workspace(1, 1, state, batch)  # must NOT raise
    assert batch.fail == 1
    assert batch.success == 0
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "ValueError" in out


def test_main_continues_after_single_workspace_exception(tmp_path):
    """End-to-end: when one workspace explodes, the other workspaces
    in the batch still run.  This is the user-visible promise."""
    _make_workspace(tmp_path, "yugioh_puzzle_h1", "codex")
    _make_workspace(tmp_path, "yugioh_puzzle_h2", "codex")  # this one explodes
    _make_workspace(tmp_path, "yugioh_puzzle_h3", "codex", with_outcome=True)

    call_log = []

    def side_effect(*args, **kwargs):
        # subprocess.run is called with [launcher_path] as args[0].
        launcher = args[0][0] if args and args[0] else ""
        call_log.append(launcher)
        if "yugioh_puzzle_h2" in launcher:
            raise FileNotFoundError(2, "missing", launcher)
        return MagicMock(returncode=0, stdout="", stderr="")

    rc, mock_run = _run_main_with_mocks("codex", tmp_path, side_effect)
    # rc is incidental (h1 fails its outcome check via mocked subprocess,
    # h2 fails via FileNotFoundError, so main returns 1 — both are
    # legitimate fail outcomes).  The promise we're validating is that
    # BOTH workspaces' launchers got attempted, i.e. h2's exception
    # didn't kill h1.
    assert rc in (0, 1)
    h1_called = any("yugioh_puzzle_h1" in c for c in call_log)
    h2_called = any("yugioh_puzzle_h2" in c for c in call_log)
    assert h1_called, f"h1 launcher never invoked; call_log={call_log}"
    assert h2_called, f"h2 launcher never invoked; call_log={call_log}"
    # h3 has an outcome already so it gets skipped (not launched).
    h3_called = any("yugioh_puzzle_h3" in c for c in call_log)
    assert not h3_called, "completed workspace should be skipped"


# ---------------------------------------------------------------------------
# main() — end-to-end with mocked subprocess
# ---------------------------------------------------------------------------


def _run_main_with_mocks(agent, runs_root, mock_run_side_effect, *extra_args):
    """Drive main() against a synthetic runs_root.  Mocks subprocess.run
    so we don't actually execute the launchers; mocks the HTTP probe so
    we don't hit network."""
    argv = ["--runs-root", str(runs_root), "--no-preflight"] + list(extra_args)
    with (
        patch("run_batch_core.subprocess.run") as mock_run,
        patch("run_batch_core.fetch_usage", return_value=None),
    ):
        mock_run.side_effect = mock_run_side_effect
        rc = rbc.main(agent, argv)
    return rc, mock_run


def test_main_skips_done_workspaces(tmp_path, capsys):
    _make_workspace(tmp_path, "yugioh_puzzle_d1", "codex", with_outcome=True)
    _make_workspace(tmp_path, "yugioh_puzzle_d2", "codex", with_outcome=True)

    def side_effect(*args, **kwargs):
        # Should never be called — both workspaces already done.
        raise AssertionError("subprocess.run should not be called for done workspaces")

    rc, mock_run = _run_main_with_mocks("codex", tmp_path, side_effect)
    assert rc == 0
    assert mock_run.call_count == 0
    out = capsys.readouterr().out
    assert "2 done, 0 pending" in out


def test_main_runs_pending_workspaces(tmp_path, capsys):
    pending_ws = _make_workspace(tmp_path, "yugioh_puzzle_p1", "codex")
    _make_workspace(tmp_path, "yugioh_puzzle_p2", "codex", with_outcome=True)

    def side_effect(*args, **kwargs):
        # When the launcher is "run", create the outcome event so
        # has_outcome flips True post-run.
        (pending_ws / "results" / "yugioh_puzzle_p1.jsonl").write_text(
            '{"type":"outcome","winner":0}\n'
        )
        return MagicMock(returncode=0, stdout="", stderr="")

    rc, mock_run = _run_main_with_mocks("codex", tmp_path, side_effect)
    assert rc == 0
    assert mock_run.call_count == 1
    out = capsys.readouterr().out
    assert "1 done, 1 pending" in out
    assert "yugioh_puzzle_p1 DONE" in out


def test_main_idempotent_re_invocation(tmp_path, capsys):
    """Running the script twice should produce the same final state.
    Second invocation must skip the now-completed workspace."""
    ws = _make_workspace(tmp_path, "yugioh_puzzle_i1", "codex")

    def first_run_side_effect(*args, **kwargs):
        (ws / "results" / "yugioh_puzzle_i1.jsonl").write_text('{"type":"outcome","winner":0}\n')
        return MagicMock(returncode=0, stdout="", stderr="")

    rc, mock_run = _run_main_with_mocks("codex", tmp_path, first_run_side_effect)
    assert rc == 0
    assert mock_run.call_count == 1

    # Second invocation: outcome event is now present so workspace is done
    def second_run_side_effect(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called on re-invocation")

    rc2, mock_run2 = _run_main_with_mocks("codex", tmp_path, second_run_side_effect)
    assert rc2 == 0
    assert mock_run2.call_count == 0
    out = capsys.readouterr().out
    assert "nothing to do" in out


def test_main_dry_run_does_not_invoke_subprocess(tmp_path, capsys):
    _make_workspace(tmp_path, "yugioh_puzzle_dr1", "codex")

    def side_effect(*args, **kwargs):
        raise AssertionError("subprocess.run should not be called in --dry-run")

    rc, mock_run = _run_main_with_mocks("codex", tmp_path, side_effect, "--dry-run")
    assert rc == 0
    assert mock_run.call_count == 0
    out = capsys.readouterr().out
    assert "pending [ready]" in out


def test_main_concurrency_threadpool(tmp_path, capsys):
    """When --concurrency > 1, all workspaces should still be processed
    and the count of subprocess.run calls should match."""
    workspaces = []
    for i in range(4):
        ws = _make_workspace(tmp_path, f"yugioh_puzzle_c{i}", "codex")
        workspaces.append(ws)

    call_count = [0]

    def side_effect(argv, **kwargs):
        # Find which workspace this call is for via the launcher path
        call_count[0] += 1
        for ws in workspaces:
            if str(ws / "run-codex-exec.sh") == argv[0]:
                (ws / "results" / f"{ws.name.split('-2026')[0]}.jsonl").write_text(
                    '{"type":"outcome","winner":0}\n'
                )
                break
        return MagicMock(returncode=0, stdout="", stderr="")

    rc, mock_run = _run_main_with_mocks("codex", tmp_path, side_effect, "--concurrency", "2")
    assert rc == 0
    assert call_count[0] == 4
    # All four should now be done
    for ws in workspaces:
        jsonl = list((ws / "results").glob("*.jsonl"))
        assert len(jsonl) == 1
        text = jsonl[0].read_text()
        assert '"outcome"' in text


def test_main_no_workspaces_returns_1(tmp_path, capsys):
    """No workspaces under runs_root → exit code 1 + stderr message."""
    rc, _ = _run_main_with_mocks("codex", tmp_path, lambda *a, **kw: None)
    assert rc == 1
    err = capsys.readouterr().err
    assert "no codex workspaces found" in err


# ---------------------------------------------------------------------------
# Filter flags: --limit, --offset, --only, --puzzle-ids
# ---------------------------------------------------------------------------


def _make_pending_codex(tmp_path: Path, n: int) -> list[Path]:
    """Build N pending codex workspaces, returns the workspace dirs."""
    return [_make_workspace(tmp_path, f"yugioh_puzzle_f{i:02d}", "codex") for i in range(n)]


def _ran_puzzle_ids(mock_run) -> list[str]:
    """Pull the puzzle_id (from launcher path's parent dir name) for each
    subprocess.run call mock_run received."""
    out = []
    for call in mock_run.call_args_list:
        argv = call.args[0]
        # argv[0] is path/to/<workspace>/run-codex-exec.sh
        ws = Path(argv[0]).parent
        # workspace name format: yugioh_puzzle_<id>-<ts>-codex
        # extract puzzle id (everything before the timestamp split)
        name = ws.name
        # Split by "-2026" (timestamp prefix) and take the part before
        pid = name.split("-2026")[0]
        out.append(pid)
    return out


def test_limit_caps_processed_count(tmp_path):
    _make_pending_codex(tmp_path, 5)

    def side_effect(argv, **kwargs):
        ws = Path(argv[0]).parent
        # Mark this workspace done by writing an outcome event
        pid = ws.name.split("-2026")[0]
        (ws / "results" / f"{pid}.jsonl").write_text('{"type":"outcome","winner":0}\n')
        return MagicMock(returncode=0, stdout="", stderr="")

    rc, mock_run = _run_main_with_mocks("codex", tmp_path, side_effect, "--limit", "2")
    assert rc == 0
    assert mock_run.call_count == 2


def test_offset_skips_first_n(tmp_path):
    _make_pending_codex(tmp_path, 5)

    def side_effect(argv, **kwargs):
        ws = Path(argv[0]).parent
        pid = ws.name.split("-2026")[0]
        (ws / "results" / f"{pid}.jsonl").write_text('{"type":"outcome","winner":0}\n')
        return MagicMock(returncode=0, stdout="", stderr="")

    rc, mock_run = _run_main_with_mocks("codex", tmp_path, side_effect, "--offset", "3")
    assert rc == 0
    # Skipped 3 of 5 → processed 2
    assert mock_run.call_count == 2
    ran = _ran_puzzle_ids(mock_run)
    # Workspaces are sorted alphabetically (ws f00..f04); offset=3 means
    # f03 + f04 get processed.
    assert set(ran) == {"yugioh_puzzle_f03", "yugioh_puzzle_f04"}


def test_offset_plus_limit(tmp_path):
    _make_pending_codex(tmp_path, 5)

    def side_effect(argv, **kwargs):
        ws = Path(argv[0]).parent
        pid = ws.name.split("-2026")[0]
        (ws / "results" / f"{pid}.jsonl").write_text('{"type":"outcome","winner":0}\n')
        return MagicMock(returncode=0, stdout="", stderr="")

    rc, mock_run = _run_main_with_mocks(
        "codex", tmp_path, side_effect, "--offset", "1", "--limit", "2"
    )
    assert rc == 0
    assert mock_run.call_count == 2
    ran = _ran_puzzle_ids(mock_run)
    assert set(ran) == {"yugioh_puzzle_f01", "yugioh_puzzle_f02"}


def test_only_single_puzzle_id(tmp_path):
    _make_pending_codex(tmp_path, 5)

    def side_effect(argv, **kwargs):
        ws = Path(argv[0]).parent
        pid = ws.name.split("-2026")[0]
        (ws / "results" / f"{pid}.jsonl").write_text('{"type":"outcome","winner":0}\n')
        return MagicMock(returncode=0, stdout="", stderr="")

    rc, mock_run = _run_main_with_mocks(
        "codex", tmp_path, side_effect, "--only", "yugioh_puzzle_f02"
    )
    assert rc == 0
    assert mock_run.call_count == 1
    assert _ran_puzzle_ids(mock_run) == ["yugioh_puzzle_f02"]


def test_puzzle_ids_comma_separated(tmp_path):
    _make_pending_codex(tmp_path, 5)

    def side_effect(argv, **kwargs):
        ws = Path(argv[0]).parent
        pid = ws.name.split("-2026")[0]
        (ws / "results" / f"{pid}.jsonl").write_text('{"type":"outcome","winner":0}\n')
        return MagicMock(returncode=0, stdout="", stderr="")

    rc, mock_run = _run_main_with_mocks(
        "codex",
        tmp_path,
        side_effect,
        "--puzzle-ids",
        "yugioh_puzzle_f00,yugioh_puzzle_f04",
    )
    assert rc == 0
    assert mock_run.call_count == 2
    assert set(_ran_puzzle_ids(mock_run)) == {"yugioh_puzzle_f00", "yugioh_puzzle_f04"}


def test_only_with_offset_and_limit_and_concurrency(tmp_path):
    """Filter combinations compose with concurrency dispatch."""
    _make_pending_codex(tmp_path, 6)

    def side_effect(argv, **kwargs):
        ws = Path(argv[0]).parent
        pid = ws.name.split("-2026")[0]
        (ws / "results" / f"{pid}.jsonl").write_text('{"type":"outcome","winner":0}\n')
        return MagicMock(returncode=0, stdout="", stderr="")

    rc, mock_run = _run_main_with_mocks(
        "codex",
        tmp_path,
        side_effect,
        "--puzzle-ids",
        "yugioh_puzzle_f00,yugioh_puzzle_f02,yugioh_puzzle_f04",
        "--offset",
        "1",
        "--limit",
        "1",
        "--concurrency",
        "2",
    )
    # 3 ids selected → offset 1 → 2 left → limit 1 → 1 processed
    assert rc == 0
    assert mock_run.call_count == 1
    # f02 (the middle one of the 3 selected, after dropping f00)
    assert _ran_puzzle_ids(mock_run) == ["yugioh_puzzle_f02"]


def test_limit_zero_is_a_noop(tmp_path):
    _make_pending_codex(tmp_path, 3)

    def side_effect(argv, **kwargs):
        raise AssertionError("limit=0 should run nothing")

    rc, mock_run = _run_main_with_mocks("codex", tmp_path, side_effect, "--limit", "0")
    assert rc == 0
    assert mock_run.call_count == 0
