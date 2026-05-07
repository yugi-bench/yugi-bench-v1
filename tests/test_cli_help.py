"""Smoke test: every CLI entry-point exits 0 on --help.

Catches import-level breakage (missing module, broken side-import, etc.)
across every user-facing CLI. Pure-Python; no libocgcore needed.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent

# Each entry is the argv list passed to `python` (excluding the
# interpreter itself).  Script-path entries work for hyphenated dirs;
# `-m module` entries work for proper Python packages (engine.* etc.).
CLI_TARGETS = [
    ["api-eval/runner.py"],
    ["-m", "engine.replay"],
    ["-m", "engine.multi_attempt"],
    ["api-eval/aggregate.py"],
    ["src/dataset/dump_prompt.py"],
    ["api-eval/extract_actions.py"],
    ["src/dataset/build_verified_subset.py"],
    ["src/dataset/build_benchmark.py"],
]


@pytest.mark.parametrize("argv", CLI_TARGETS, ids=lambda a: a[-1] if a[0] == "-m" else a[0])
def test_cli_help_exits_zero(argv: list[str]):
    import os
    env = os.environ.copy()
    # src/ layout: add src/ to PYTHONPATH so `python -m engine.replay` etc.
    # find their packages.  Direct script paths (api-eval/runner.py etc.)
    # don't need this since they sys.path-bootstrap themselves.
    env["PYTHONPATH"] = str(REPO_ROOT / "src") + ":" + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, *argv, "--help"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )
    label = argv[-1] if argv[0] == "-m" else argv[0]
    assert result.returncode == 0, (
        f"{label} --help failed: rc={result.returncode}\n"
        f"stderr: {result.stderr[:500]}"
    )
    combined = result.stdout + result.stderr
    assert "usage:" in combined.lower(), (
        f"{label} --help did not print a usage block: {combined[:300]}"
    )
