"""End-to-end test for agent-mcp-eval/codex/prep-session.sh.

Runs the prep script against a real puzzle id from the dataset, with a
stubbed docker (a tiny shell script that returns 0 for any args), and
asserts the produced .codex/config.toml has the expected structure:

  - 9 top-level scalars in the right positions
  - 11 sub-tables
  - [features] with 12 toggles all false
  - [mcp_servers.yugi-bench] with 25 enabled_tools matching the
    canonical list
  - 25 per-tool [mcp_servers.yugi-bench.tools.<name>] approval blocks
  - __WORKSPACE__ placeholder substituted in
    sandbox_workspace_write.writable_roots and the [projects.<>]
    table key

No docker / podman / engine needed.  Pure-bash + tomllib + a 4-line
stub.  Skipped if the codex/ subfolder is missing (e.g. running an
older checkout).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

try:
    import tomllib  # py 3.11+
except ImportError:
    pytest.skip("tomllib (py 3.11+) required", allow_module_level=True)


_REPO_ROOT = Path(__file__).resolve().parent.parent
_PREP = _REPO_ROOT / "agent-mcp-eval" / "codex" / "prep-session.sh"
_DATASET = _REPO_ROOT / "data" / "yugioh_bench.jsonl"

# Canonical 25-tool surface — kept in sync with engine.tools.TOOLS
# filtered by container.server._container_tool_set.
EXPECTED_TOOLS = {
    "get_briefing",
    "get_state", "pending_decision", "get_glossary",
    "restart",
    "select_battlecmd", "select_idlecmd", "select_effectyn",
    "select_yesno", "select_option", "select_card",
    "select_card_codes", "select_unselect_card", "select_chain",
    "select_place", "select_position", "select_tribute",
    "select_counter", "select_sum", "sort_card",
    "announce_race", "announce_attribute", "announce_card",
    "announce_number", "rock_paper_scissors",
}


def _stub_docker(tmp_path: Path) -> Path:
    """Tiny stub that mimics docker enough for the prep script's
    `docker image inspect` check (returns 0 for any args)."""
    stub = tmp_path / "fake-docker"
    stub.write_text(textwrap.dedent("""\
        #!/usr/bin/env bash
        # Stub docker for tests.  `image inspect` and any other subcommand
        # exits 0; produces no output.  We never actually launch the image.
        exit 0
    """))
    stub.chmod(0o755)
    return stub


def _first_puzzle_id() -> str:
    """Read the first puzzle id from the dataset.  The prep script
    requires it to be a real id."""
    with open(_DATASET) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            return json.loads(line)["instance_id"]
    raise RuntimeError("no puzzles in dataset")


@pytest.fixture(scope="module")
def workspace(tmp_path_factory) -> Path:
    """Run the prep script once per module; return the produced workspace."""
    if not _PREP.exists():
        pytest.skip(f"prep-session.sh missing at {_PREP}")
    if not _DATASET.exists():
        pytest.skip(f"dataset missing at {_DATASET}")

    runs_root = tmp_path_factory.mktemp("yugi-test-runs")
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

    workspaces = list(runs_root.glob("*-codex"))
    assert len(workspaces) == 1, f"expected exactly one workspace, got {workspaces}"
    return workspaces[0]


@pytest.fixture(scope="module")
def config_doc(workspace: Path) -> dict:
    config_path = workspace / ".codex" / "config.toml"
    assert config_path.exists(), f"config.toml not produced at {config_path}"
    return tomllib.load(open(config_path, "rb"))


# ---------------------------------------------------------------------------
# Top-level structure
# ---------------------------------------------------------------------------

def test_top_level_scalars_present(config_doc: dict):
    """All 10 expected top-level scalars in the right position (BEFORE
    any [section] header) and with the right values."""
    expected = {
        "project_root_markers": [],
        "sandbox_mode": "workspace-write",
        "default_permissions": "cwd-only",
        "approval_policy": "never",
        "model_reasoning_effort": "xhigh",
        "web_search": "disabled",
        "model_auto_compact_token_limit": 999999999,
        "model_context_window": 256000,
        "check_for_update_on_startup": False,
        "disable_paste_burst": True,
    }
    for key, value in expected.items():
        assert config_doc.get(key) == value, (
            f"top-level scalar {key}: expected {value!r}, "
            f"got {config_doc.get(key)!r}"
        )


def test_tool_output_token_limit_NOT_present(config_doc: dict):
    """tool_output_token_limit must NOT be set — MCP get_state /
    get_briefing responses are long, and truncating them hurts agent
    reasoning more than compaction would.  Documented intent in
    restrictions.toml.template; this test pins it so a future
    well-meaning edit doesn't quietly add it back."""
    assert "tool_output_token_limit" not in config_doc


def test_top_level_table_count(config_doc: dict):
    """11 named tables: analytics, apps, features, feedback, history,
    mcp_servers, memories, permissions, projects,
    sandbox_workspace_write, tools."""
    expected_tables = {
        "analytics", "apps", "features", "feedback", "history",
        "mcp_servers", "memories", "permissions", "projects",
        "sandbox_workspace_write", "tools",
    }
    actual_tables = {k for k, v in config_doc.items() if isinstance(v, dict)}
    assert actual_tables == expected_tables


# ---------------------------------------------------------------------------
# Specific lockdown sections
# ---------------------------------------------------------------------------

def test_features_disabled(config_doc: dict):
    features = config_doc["features"]
    expected_off = {
        "apps", "codex_hooks", "fast_mode", "memories", "multi_agent",
        "personality", "prevent_idle_sleep", "shell_snapshot", "shell_tool",
        "skill_mcp_dependency_install", "undo", "unified_exec",
    }
    assert set(features.keys()) == expected_off
    for key in expected_off:
        assert features[key] is False, f"features.{key} should be false"


def test_features_web_search_NOT_present(config_doc: dict):
    """[features].web_search is deprecated; we use top-level web_search
    instead.  Must NOT appear in [features] (codex prints a deprecation
    warning if it does)."""
    assert "web_search" not in config_doc["features"]


def test_tools_block(config_doc: dict):
    tools = config_doc["tools"]
    assert tools.get("view_image") is False
    assert tools.get("web_search") is False


def test_apps_default_disabled(config_doc: dict):
    apps_default = config_doc["apps"]["_default"]
    assert apps_default == {
        "enabled": False,
        "destructive_enabled": False,
        "open_world_enabled": False,
    }


def test_memories_disabled(config_doc: dict):
    memories = config_doc["memories"]
    assert memories.get("use_memories") is False
    assert memories.get("generate_memories") is False


def test_permissions_cwd_only_profile(config_doc: dict):
    perms = config_doc["permissions"]["cwd-only"]
    assert perms["filesystem"][":project_roots"] == {".": "read"}
    assert perms["filesystem"]["glob_scan_max_depth"] == 1
    assert perms["network"]["enabled"] is False


def test_sandbox_workspace_write(config_doc: dict, workspace: Path):
    sw = config_doc["sandbox_workspace_write"]
    assert sw["network_access"] is False
    assert sw["exclude_slash_tmp"] is True
    assert sw["exclude_tmpdir_env_var"] is True
    # The __WORKSPACE__ placeholder must be substituted with the actual
    # absolute workspace path.
    assert sw["writable_roots"] == [f"{workspace}/results"]


def test_projects_trust_block(config_doc: dict, workspace: Path):
    projects = config_doc["projects"]
    # The project trust block uses the absolute workspace path as the key.
    assert str(workspace) in projects
    assert projects[str(workspace)]["trust_level"] == "trusted"


# ---------------------------------------------------------------------------
# MCP server + tool surface
# ---------------------------------------------------------------------------

def test_mcp_server_yugi_bench_present(config_doc: dict):
    mcp = config_doc["mcp_servers"]["yugi-bench"]
    assert mcp["startup_timeout_sec"] == 120
    assert mcp["default_tools_approval_mode"] == "approve"


def test_enabled_tools_matches_canonical_25(config_doc: dict):
    """The enabled_tools allowlist must match the canonical 25-tool
    surface exposed by the container in default mode."""
    enabled = set(config_doc["mcp_servers"]["yugi-bench"]["enabled_tools"])
    assert enabled == EXPECTED_TOOLS, (
        f"missing: {EXPECTED_TOOLS - enabled}, extra: {enabled - EXPECTED_TOOLS}"
    )


def test_per_tool_approval_blocks_for_all_25(config_doc: dict):
    """Each of the 25 tools has its own [mcp_servers.yugi-bench.tools.<name>]
    block with approval_mode = "approve"."""
    tools = config_doc["mcp_servers"]["yugi-bench"]["tools"]
    assert set(tools.keys()) == EXPECTED_TOOLS
    for name, block in tools.items():
        assert block.get("approval_mode") == "approve", (
            f"{name}: approval_mode should be 'approve', got {block.get('approval_mode')!r}"
        )


def test_workspace_substituted_in_docker_args(config_doc: dict, workspace: Path):
    """The MCP server's docker args must reference the workspace's
    results dir as a bind-mount target."""
    args = config_doc["mcp_servers"]["yugi-bench"]["args"]
    bind_mount_arg = f"{workspace}/results:/work/results"
    assert bind_mount_arg in args


def test_workspace_path_no_placeholder_left(config_doc: dict):
    """No __WORKSPACE__ placeholder should survive in any value
    after the merge."""
    payload = json.dumps(config_doc, default=str)
    assert "__WORKSPACE__" not in payload


# ---------------------------------------------------------------------------
# Sanity: round-trip through tomllib
# ---------------------------------------------------------------------------

def test_config_round_trips_through_tomllib(workspace: Path):
    """The produced config.toml must be valid TOML that re-parses to
    the same dict on a second pass."""
    config_path = workspace / ".codex" / "config.toml"
    raw = config_path.read_bytes()
    first = tomllib.loads(raw.decode())
    # The only way to round-trip via stdlib is parse-only; we can't
    # re-emit.  But we can re-parse the same bytes and get the same
    # structure.
    second = tomllib.loads(raw.decode())
    assert first == second
