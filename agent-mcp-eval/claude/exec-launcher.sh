#!/usr/bin/env bash
# Auto-driven (claude --print) equivalent of the manual `claude` flow.
# Restrictions come from .claude/settings.json (auto-loaded from cwd by
# Claude Code) and CLAUDE.md (auto-discovered from cwd).  The MCP
# container writes canonical results/<id>.jsonl regardless of driver.
# Combined output is teed to claude-exec.log for post-mortem.
#
# CAVEAT: claude --print historically misbehaved with MCP servers per
# the 2026-05-04 finding.  The current lockdown profile
# (permissions.defaultMode = dontAsk, allow = ["mcp__yugi-bench__*",
# "ToolSearch"], full deny list of dangerous builtins, and
# --strict-mcp-config below) closes the approval-channel cancellation
# that caused the original failure.  Verified working end-to-end on
# 2026-05-07 against gpt-style claude opus-4-7 on a complexity-1
# puzzle; the agent connects cleanly, calls get_briefing, and plays
# through.  ToolSearch must be in the allow list because the agent
# uses it to look up MCP tool schemas when input-validation errors
# fire — without it, the agent can't recover.
#
# Copied into each claude workspace by agent-mcp-eval/claude/prep-session.sh
# and invoked as run-claude-exec.sh.
set -euo pipefail
cd "$(dirname "$0")"

# Inject the doctrine file directly into the user prompt instead of
# relying on CLAUDE.md cwd auto-discovery.  Same rationale as the
# codex sibling launcher (2026-05-07): the --print path is less
# reliable than the interactive flow at surfacing MCP tool lists +
# CLAUDE.md doctrine, so we paste the doctrine into the user message
# to guarantee the model sees "call get_briefing first".
PROMPT="$(cat CLAUDE.md)

Begin the puzzle now by calling the get_briefing tool."

# --tools "ToolSearch" strips every Claude builtin (Bash, Read, Edit,
# Glob, Grep, Task, WebFetch/WebSearch, TodoWrite, …) from the agent's
# tool surface entirely.  MCP tools always pass through.  This is
# stronger than permissions.deny because the agent literally never
# sees the builtins exist — no read-only Bash carve-out, no
# cat-the-solution-file path.  ToolSearch is kept because empirical
# 2026-05-07 smoke showed the agent needs it for MCP-schema lookup
# when input-validation errors fire.
## NOTE: --tools is a VARIADIC flag (declared `--tools <tools...>` in
## --help) and consumes every following positional arg as a value.
## We use the equals form `--tools=ToolSearch` (single token) so the
## "$PROMPT" trailing positional is safely interpreted as the prompt
## argument rather than as another tool name.  Without the equals form
## the launcher errors out with "Input must be provided either through
## stdin or as a prompt argument when using --print".
claude --strict-mcp-config --mcp-config ./.mcp.json \
    --print --output-format stream-json --verbose \
    --permission-mode dontAsk \
    --tools=ToolSearch \
    "$PROMPT" 2>&1 | tee claude-exec.log
exit "${PIPESTATUS[0]}"
