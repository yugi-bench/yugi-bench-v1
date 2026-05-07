#!/usr/bin/env python3
"""Codex run-batch — drive every pending codex workspace via codex exec.

Idempotent + mixed-mode safe.  Walks ~/yugi-bench-runs/, filters to
codex workspaces (metadata.json's ``agent`` == "codex"), runs each
pending workspace's run-codex-exec.sh.  Skips workspaces whose
``results/<id>.jsonl`` already contains an ``outcome`` event — so
manual sessions completing between invocations don't get re-run.

Shared implementation lives in ../_lib/run_batch_core.py (also used
by claude/run-batch.py).

Usage:
    ./run-batch.py
    ./run-batch.py --dry-run
    ./run-batch.py --runs-root /custom/path
    ./run-batch.py --per-session-timeout-seconds 3600
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "_lib"))
from run_batch_core import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main("codex"))
