"""End-to-end test for agent-mcp-eval/claude/prep-session.sh.

Mirrors tests/test_codex_config_merge.py for the claude variant.
Runs the prep script against a real puzzle id from the dataset, with
a stubbed docker (a tiny shell script that returns 0 for any args),
and asserts the produced .claude/settings.json has the expected
structure:

  - permissions.defaultMode = "dontAsk" (auto-deny everything not
    explicitly allowed)
  - permissions.allow = ["mcp__yugi-bench__*"] (allow exactly the
    yugi-bench MCP tools, deny every Claude builtin via dontAsk)
  - 5 top-level boolean knobs (autoMemoryEnabled, disableAllHooks,
    enableAllProjectMcpServers, includeCoAuthoredBy, plus
    effortLevel) at the right values
  - env block with the full ten-env-var lockdown bundle
  - DISABLE_COMPACT + CLAUDE_CODE_MAX_CONTEXT_TOKENS paired correctly
  - .mcp.json with the yugi-bench server pointed at a docker run with
    --network none

No docker / podman / claude / engine needed.  Pure-bash + python +
json + a 4-line stub.  Skipped if the claude/ subfolder is missing.
"""

from __future__ import annotations

import json
import os
import subprocess
import textwrap
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PREP = _REPO_ROOT / "agent-mcp-eval" / "claude" / "prep-session.sh"
_DATASET = _REPO_ROOT / "data" / "yugioh_bench.jsonl"


def _stub_docker(tmp_path: Path) -> Path:
    stub = tmp_path / "fake-docker"
    stub.write_text(
        textwrap.dedent("""\
        #!/usr/bin/env bash
        # Stub docker for tests.  Any invocation exits 0 with no output.
        exit 0
    """)
    )
    stub.chmod(0o755)
    return stub


def _first_puzzle_id() -> str:
    with open(_DATASET) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            return json.loads(line)["instance_id"]
    raise RuntimeError("no puzzles in dataset")


@pytest.fixture(scope="module")
def workspace(tmp_path_factory) -> Path:
    if not _PREP.exists():
        pytest.skip(f"prep-session.sh missing at {_PREP}")
    if not _DATASET.exists():
        pytest.skip(f"dataset missing at {_DATASET}")

    runs_root = tmp_path_factory.mktemp("yugi-claude-runs")
    stub = _stub_docker(tmp_path_factory.mktemp("stub"))
    puzzle = _first_puzzle_id()

    env = os.environ.copy()
    env["DOCKER"] = str(stub)
    env["YUGI_RUNS_ROOT"] = str(runs_root)

    proc = subprocess.run(
        ["bash", str(_PREP), puzzle],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0, (
        f"prep-session.sh failed: rc={proc.returncode}\n"
        f"stdout: {proc.stdout[:500]}\n"
        f"stderr: {proc.stderr[:500]}"
    )

    workspaces = [w for w in runs_root.iterdir() if w.is_dir()]
    assert len(workspaces) == 1, f"expected exactly one workspace, got {workspaces}"
    return workspaces[0]


@pytest.fixture(scope="module")
def settings(workspace: Path) -> dict:
    p = workspace / ".claude" / "settings.json"
    assert p.exists(), f"settings.json not produced at {p}"
    return json.loads(p.read_text())


@pytest.fixture(scope="module")
def mcp_config(workspace: Path) -> dict:
    p = workspace / ".mcp.json"
    assert p.exists(), f".mcp.json not produced at {p}"
    return json.loads(p.read_text())


# ---------------------------------------------------------------------------
# Top-level lockdown knobs
# ---------------------------------------------------------------------------


def test_top_level_scalars(settings: dict):
    """The 5 documented top-level keys (4 booleans + 1 string)
    that complement the permissions / env blocks."""
    assert settings["autoMemoryEnabled"] is False
    assert settings["effortLevel"] == "xhigh"
    assert settings["disableAllHooks"] is True
    assert settings["enableAllProjectMcpServers"] is False
    assert settings["includeCoAuthoredBy"] is False


# ---------------------------------------------------------------------------
# Permissions block
# ---------------------------------------------------------------------------


def test_permissions_default_mode_is_dontAsk(settings: dict):
    """`dontAsk` is the right semantic for benchmark lockdown — auto-
    DENY every tool not in `allow`.  `bypassPermissions` would skip
    the permission layer entirely (deny rules ignored), which is
    wrong for this profile."""
    assert settings["permissions"]["defaultMode"] == "dontAsk"


def test_permissions_allow_yugi_bench_and_toolsearch(settings: dict):
    """Allow list: yugi-bench MCP tools + ToolSearch.  ToolSearch is
    a Claude meta-tool the agent uses to look up MCP schemas when
    input-validation errors fire (verified empirically 2026-05-07 —
    without ToolSearch the agent can't recover from str-vs-int args).
    No other builtin is in the allow list."""
    allow = settings["permissions"]["allow"]
    assert set(allow) == {"mcp__yugi-bench__*", "ToolSearch"}


def test_permissions_deny_dangerous_builtins(settings: dict):
    """Per the permissions docs, deny rules are evaluated FIRST
    (deny -> ask -> allow), so even under dontAsk an explicit deny
    block adds defence-in-depth over the read-only Bash + cwd-reads
    carve-out wherever Claude Code honours it.  Pin the full set so
    a future loosening is loud."""
    deny = set(settings["permissions"]["deny"])
    expected = {
        "Bash",
        "BashOutput",
        "KillShell",
        "Read",
        "Write",
        "Edit",
        "MultiEdit",
        "NotebookEdit",
        "Glob",
        "Grep",
        "Task",
        "Agent",
        "WebFetch",
        "WebSearch",
        "TodoWrite",
        "PowerShell",
    }
    assert deny == expected


# ---------------------------------------------------------------------------
# env block — telemetry / ephemeral / compact
# ---------------------------------------------------------------------------


def test_env_disables_nonessential_traffic(settings: dict):
    """Single env var that sets DISABLE_AUTOUPDATER + DISABLE_FEEDBACK_
    COMMAND + DISABLE_TELEMETRY + DISABLE_ERROR_REPORTING in one shot.
    Parity with codex analytics/feedback/check_for_update_on_startup."""
    assert settings["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"


def test_env_skips_prompt_history(settings: dict):
    """No transcript writes to ~/.claude/projects/<hash>/<id>.jsonl.
    Parity with codex --ephemeral + history.persistence = "none"."""
    assert settings["env"]["CLAUDE_CODE_SKIP_PROMPT_HISTORY"] == "1"


def test_env_disables_auto_compact_and_pins_window(settings: dict):
    """DISABLE_COMPACT only takes effect when MAX_CONTEXT_TOKENS is
    also set.  Both must be present; either alone is a no-op.  1M
    matches sonnet 4.6's extended window — parity with V4's implicit
    1M (api-eval/runner.py imposes no context cap)."""
    assert settings["env"]["DISABLE_COMPACT"] == "1"
    assert settings["env"]["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "1000000"


def test_env_feature_disables(settings: dict):
    """Parity with codex's [features] / [memories] / [tools] toggles."""
    expected = {
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_ATTACHMENTS": "1",
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
        "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
        "CLAUDE_CODE_DISABLE_FAST_MODE": "1",
        "CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING": "1",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    }
    for key, value in expected.items():
        assert settings["env"].get(key) == value, (
            f"env.{key}: expected {value!r}, got {settings['env'].get(key)!r}"
        )


def test_env_does_NOT_disable_1m_context(settings: dict):
    """CLAUDE_CODE_DISABLE_1M_CONTEXT=1 forces sonnet into the 200K
    standard window.  The V4 baseline (api-eval/runner.py) imposes
    no context cap and ran with V4's full 1M window available (peak
    input ~130K).  For parity, claude must also have 1M.  Empirically
    ~80% of easy verified puzzles overflow at the 200K cap because
    the per-turn game-state JSON × number-of-turns exceeds sonnet
    standard.  Pin its absence."""
    assert "CLAUDE_CODE_DISABLE_1M_CONTEXT" not in settings["env"]


def test_env_disables_claude_mds_auto_discovery(settings: dict):
    """Containment: when the launcher runs on a host that has a
    CLAUDE.md at the user's home (i.e. an unrelated agent-doctrine
    file in ~ or a parent directory), claude's cwd-up-to-root
    auto-discovery would inject that prose into the agent's system
    prompt.  The launcher pastes the workspace's per-puzzle CLAUDE.md
    into the user prompt explicitly, so disabling auto-discovery loses
    no useful behaviour but prevents host-level doctrine leaking into
    the benchmark agent's context."""
    assert settings["env"].get("CLAUDE_CODE_DISABLE_CLAUDE_MDS") == "1"


def test_env_block_count(settings: dict):
    """Pin the env-var count so a future well-meaning addition gets
    flagged in review, and removals from the lockdown profile are
    explicit rather than silent.  CLAUDE_CODE_OAUTH_TOKEN is appended
    only when the env var is set at prep time, so the count is 11
    when running the test suite without that env var set, 12 when set.
    (Was 12/13 before dropping CLAUDE_CODE_DISABLE_1M_CONTEXT for V4
    parity.)"""
    assert len(settings["env"]) in (11, 12)


def test_oauth_token_baked_when_present(tmp_path_factory):
    """When CLAUDE_CODE_OAUTH_TOKEN is set in the prep-time environment,
    the workspace's settings.json env should pick it up so the
    workspace is self-contained for batch runs (parity with the
    codex side's auth.json symlink)."""
    if not _PREP.exists() or not _DATASET.exists():
        pytest.skip("prep script or dataset missing")

    runs_root = tmp_path_factory.mktemp("yugi-claude-oauth-runs")
    stub = _stub_docker(tmp_path_factory.mktemp("oauth-stub"))
    puzzle = _first_puzzle_id()
    env = os.environ.copy()
    env["DOCKER"] = str(stub)
    env["YUGI_RUNS_ROOT"] = str(runs_root)
    env["CLAUDE_CODE_OAUTH_TOKEN"] = "sentinel-oauth-token-12345"
    proc = subprocess.run(
        ["bash", str(_PREP), puzzle],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert proc.returncode == 0
    workspaces = [w for w in runs_root.iterdir() if w.is_dir()]
    assert len(workspaces) == 1
    ws = workspaces[0]
    settings_with_oauth = json.loads((ws / ".claude" / "settings.json").read_text())
    assert (
        settings_with_oauth["env"].get("CLAUDE_CODE_OAUTH_TOKEN") == "sentinel-oauth-token-12345"
    ), "CLAUDE_CODE_OAUTH_TOKEN should be baked into the workspace settings.json env"
    # And the auth-detection note should appear in stdout.
    assert "CLAUDE_CODE_OAUTH_TOKEN baked" in proc.stdout


# ---------------------------------------------------------------------------
# Top-level hardening that's NOT in settings.json (CLI-flag enforced)
# ---------------------------------------------------------------------------


def test_settings_does_not_set_dangerous_keys(settings: dict):
    """Defence-in-depth: certain keys would undo the lockdown if
    silently added by a future edit.  Pin their absence."""
    assert "permissions.disableBypassPermissionsMode" not in settings
    # autoMemoryEnabled true would re-enable cross-puzzle leakage.
    assert settings.get("autoMemoryEnabled") is False


# ---------------------------------------------------------------------------
# .mcp.json shape — yugi-bench server with --network none
# ---------------------------------------------------------------------------


def test_mcp_yugi_bench_present(mcp_config: dict):
    assert "yugi-bench" in mcp_config["mcpServers"]


def test_mcp_uses_network_none(mcp_config: dict):
    """The MCP container is one-shot per docker run --rm and must
    have --network none so the engine inside is fully air-gapped."""
    args = mcp_config["mcpServers"]["yugi-bench"]["args"]
    assert "--network" in args
    assert "none" in args


def test_mcp_results_bind_mount(mcp_config: dict, workspace: Path):
    """Per-puzzle JSONL log lands in workspace/results/ via bind
    mount."""
    args = mcp_config["mcpServers"]["yugi-bench"]["args"]
    bind = f"{workspace}/results:/work/results"
    assert bind in args
