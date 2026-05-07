#!/usr/bin/env python3
"""Claude run-batch — drive every pending claude workspace via claude --print.

Idempotent + mixed-mode safe.  Walks ~/yugi-bench-runs/, filters to
claude workspaces (metadata.json's ``agent`` == "claude"), runs each
pending workspace's run-claude-exec.sh.  Skips workspaces whose
``results/<id>.jsonl`` already contains an ``outcome`` event — so
manual sessions completing between invocations don't get re-run.

CAVEAT: claude --print historically misbehaved with MCP servers per
the 2026-05-04 finding.  The current lockdown profile (in
.claude/settings.json: permissions.defaultMode = bypassPermissions +
deny WebSearch/WebFetch + the launcher's --strict-mcp-config) should
suppress the approval-channel cancellation that caused that, but
verify on a single workspace before scaling up.

Shared implementation lives in ../_lib/run_batch_core.py (also used
by codex/run-batch.py).

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
    raise SystemExit(main("claude"))
