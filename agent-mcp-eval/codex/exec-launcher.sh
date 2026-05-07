#!/usr/bin/env bash
# Auto-driven (codex exec) equivalent of the manual `codex` + type-"start"
# flow.  All restrictions come from .codex/config.toml (CODEX_HOME-loaded)
# and AGENTS.md (cwd-discovered).  The MCP container writes the canonical
# results/<id>.jsonl regardless of agent driver.  This launcher's combined
# output is teed to codex-exec.log for post-mortem.
#
# `codex exec` defaults --sandbox to read-only at the CLI level, which
# overrides config.toml's sandbox_mode = "workspace-write" and prevents
# the MCP container's docker subprocess from completing its handshake
# (interactive `codex` honours the config value, exec does not).  We
# pin --sandbox workspace-write explicitly so the launcher matches the
# manual flow.  Approval policy (never) IS honoured from config; we
# don't pass --full-auto because that's a paired sandbox+approval
# override we don't want propagating opaquely.
#
# Copied into each codex workspace by agent-mcp-eval/codex/prep-session.sh
# and invoked as run-codex-exec.sh.
set -euo pipefail
cd "$(dirname "$0")"
export CODEX_HOME="$PWD/.codex"

if [ ! -e "$CODEX_HOME/auth.json" ] && [ -z "${OPENAI_API_KEY:-}" ]; then
    echo "[run-codex-exec] WARNING: no auth detected" >&2
fi

# Inject the doctrine file directly into the user prompt instead of
# relying on AGENTS.md cwd auto-discovery.  Empirically (2026-05-07)
# the auto-driven --json path doesn't surface the registered MCP
# tool list to the model the way the TUI does, and the model probes
# for MCP *resources* (which yugi-bench doesn't expose), gets back an
# empty list, and incorrectly concludes the yugi-bench tools aren't
# available.  Pasting AGENTS.md into the user message guarantees the
# model sees the "call get_briefing first" doctrine + the tool surface
# regardless of how codex exec assembles its system prompt.
PROMPT="$(cat AGENTS.md)

Begin the puzzle now by calling the get_briefing tool."

codex exec \
    --cd "$PWD" \
    --skip-git-repo-check \
    --sandbox workspace-write \
    --ephemeral \
    --json \
    "$PROMPT" 2>&1 | tee codex-exec.log
exit "${PIPESTATUS[0]}"
