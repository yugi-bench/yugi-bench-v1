#!/usr/bin/env bash
# Prepare one isolated workspace for a single claude puzzle session.
# Cd into the workspace, then either:
#   - launch `claude --strict-mcp-config --mcp-config ./.mcp.json` manually
#   - run ./run-claude-exec.sh for the same flow, fully scripted
#     (note: claude --print + MCP has known historical issues — see
#     ../claude/exec-launcher.sh's header)
#
# Each workspace contains:
#   - CLAUDE.md — Claude Code auto-discovers this from cwd; the agent's
#     instructions for this puzzle live here.
#   - .mcp.json — MCP server pinned to THIS puzzle via
#     `docker run … --puzzle <id> --network none`.  Auto-discovered
#     from cwd; combined with `--strict-mcp-config --mcp-config
#     ./.mcp.json` to lock out global ~/.claude.json mcpServers.
#   - .claude/settings.json — isolation flags (web search/fetch denied,
#     auto-memory off, defaultMode bypassPermissions) so the agent
#     can't leak signal via search and tool prompts are skipped.
#   - results/ — bind-mounted into the container; per-puzzle JSONL log
#     lands there automatically.
#   - metadata.json — kept for aggregate-results.py.
#   - run-claude-exec.sh — copied from ../claude/exec-launcher.sh.
#
# Multiple workspaces in different terminals run fully in parallel.
#
# Usage:
#   ./prep-session.sh <puzzle_id> [opts]
#
# Options:
#   --max-tool-calls N          tool-call budget per session  (default 500)
#   --no-auto-opponent          skip opponent-side auto-decisions
#                               (default: --auto-opponent ON, friendlier
#                               for live agents; OFF matches the
#                               original API-driven sweep config)
#   --image TAG                 container image tag           (default yugi-bench-env:latest)
#   --runs-root DIR             where to put workspaces       (default ~/yugi-bench-runs)
#   --memory SIZE               container memory cap          (default 1g)
#   --cpus N                    container CPU cap             (default 1.0)
#   --docker BIN                container runtime             (default $DOCKER env or 'docker')
set -euo pipefail

if [ $# -lt 1 ]; then
    sed -n '1,/^set -euo pipefail/p' "$0" | sed -e '$d' -e 's/^# \?//'
    exit 2
fi

PUZZLE_ID="$1"
shift

MAX_TOOL_CALLS=500
AUTO_OPPONENT=1
IMAGE_TAG="yugi-bench-env:latest"
RUNS_ROOT="${YUGI_RUNS_ROOT:-$HOME/yugi-bench-runs}"
MEMORY="1g"
CPUS="1.0"
DOCKER_BIN="${DOCKER:-docker}"

while [ $# -gt 0 ]; do
    case "$1" in
        --max-tool-calls)   MAX_TOOL_CALLS="$2"; shift 2 ;;
        --no-auto-opponent) AUTO_OPPONENT=0; shift ;;
        --auto-opponent)    AUTO_OPPONENT=1; shift ;;
        --image)            IMAGE_TAG="$2"; shift 2 ;;
        --runs-root)        RUNS_ROOT="$2"; shift 2 ;;
        --memory)           MEMORY="$2"; shift 2 ;;
        --cpus)             CPUS="$2"; shift 2 ;;
        --docker)           DOCKER_BIN="$2"; shift 2 ;;
        *)                  echo "[prep] unknown flag: $1" >&2; exit 2 ;;
    esac
done

# Resolve repo root (this script lives at agent-mcp-eval/claude/prep-session.sh)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DATASET="$REPO_ROOT/data/yugioh_bench.jsonl"
EXEC_LAUNCHER="$SCRIPT_DIR/exec-launcher.sh"

# --- Validate puzzle id is in the dataset ---
if [ ! -f "$DATASET" ]; then
    echo "[prep] ERROR: dataset missing at $DATASET" >&2
    exit 1
fi
if ! grep -q "\"instance_id\": \"$PUZZLE_ID\"" "$DATASET"; then
    echo "[prep] ERROR: puzzle '$PUZZLE_ID' not found in $DATASET" >&2
    echo "[prep] First few available IDs:" >&2
    grep -o '"instance_id": "[^"]*"' "$DATASET" | head -5 >&2
    exit 1
fi

# --- Exec launcher presence ---
if [ ! -r "$EXEC_LAUNCHER" ]; then
    echo "[prep] ERROR: exec launcher missing: $EXEC_LAUNCHER" >&2
    exit 1
fi

# --- Image presence check ---
if ! "$DOCKER_BIN" image inspect "$IMAGE_TAG" >/dev/null 2>&1; then
    cat >&2 <<EOF
[prep] ERROR: image '$IMAGE_TAG' not found.

Build it first:
    $REPO_ROOT/agent-mcp-eval/build-image.sh
EOF
    exit 1
fi

# --- Prepare workspace ---
TS="$(date +%Y-%m-%dT%H-%M-%S)"
WORKSPACE="$RUNS_ROOT/${PUZZLE_ID}-${TS}"
mkdir -p "$WORKSPACE/results" "$WORKSPACE/.claude"

# --- Write .mcp.json + .claude/settings.json (per-session config) ---
# Note: we use python3 to emit valid JSON rather than bash heredoc string
# splicing — quoting is hard to get right by hand and the failure mode
# is "Claude Code silently ignores the config".
python3 - "$WORKSPACE" "$DOCKER_BIN" "$IMAGE_TAG" "$PUZZLE_ID" \
        "$MAX_TOOL_CALLS" "$AUTO_OPPONENT" "$MEMORY" "$CPUS" <<'PYEOF'
import json
import os
import sys
from pathlib import Path

(workspace, docker_bin, image_tag, puzzle_id,
 max_tool_calls, auto_opp, memory, cpus) = sys.argv[1:]

docker_args = ["run", "-i", "--rm", "--network", "none"]
# Rootless podman maps container uid 1000 to a host subuid by default,
# making the bind-mounted results dir unwritable. --userns=keep-id maps
# the host caller uid identically. Docker Desktop on macOS handles uid
# mapping its own way and doesn't need this flag.
if "podman" in os.path.basename(docker_bin):
    docker_args.append("--userns=keep-id")
docker_args += [
    "--memory", memory,
    "--cpus", cpus,
    "-v", f"{workspace}/results:/work/results",
    image_tag,
    "--puzzle", puzzle_id,
    "--max-tool-calls", str(max_tool_calls),
]
if auto_opp == "1":
    docker_args.append("--auto-opponent")

mcp_config = {
    "mcpServers": {
        "yugi-bench": {
            "command": docker_bin,
            "args": docker_args,
        }
    }
}
Path(f"{workspace}/.mcp.json").write_text(
    json.dumps(mcp_config, indent=2) + "\n"
)

# --- Write .claude/settings.json — Claude-side parity with the codex
# restrictions.toml.template lockdown.  Verified against the docs at
# code.claude.com/docs/en/{settings,permissions,permission-modes,env-vars,
# cli-reference} as of 2026-05-07.  See claude/README.md "The lockdown
# profile" for the full parity matrix and the cheap-to-relax knobs.
#
# Top-level shape:
#   - permissions.defaultMode = "dontAsk" — auto-DENY every tool call
#     that isn't explicitly pre-approved.  This is the right semantic
#     for "lock the agent to MCP tools only"; bypassPermissions skips
#     the permission layer entirely (deny rules are silently ignored).
#   - permissions.allow = ["mcp__yugi-bench__*", "ToolSearch"] — yugi-
#     bench MCP tools and ToolSearch (Claude meta-tool needed for
#     schema lookup in --print mode) execute; every other Claude
#     builtin auto-denies under dontAsk.  ToolSearch was added after a
#     2026-05-07 smoke run showed the agent needed it to recover from
#     MCP-tool input-validation errors (string vs int).
#   - permissions.deny enumerates the dangerous builtins explicitly
#     (Bash, Read, Write, Edit, Glob, Grep, Task/Agent, WebFetch/
#     WebSearch, TodoWrite, etc.) so deny-precedence kicks in over the
#     "read-only Bash + cwd reads" carve-out where possible.  The
#     carve-out is built-in and unconfigurable; the deny block is
#     defence-in-depth, not a complete seal.
#   - effortLevel = "xhigh" — max reasoning depth (parity with codex's
#     model_reasoning_effort = "xhigh"; "max" is NOT documented).
#   - autoMemoryEnabled = false — no CWD-keyed memory injection.
#   - disableAllHooks = true — user-side PreToolUse/PostToolUse hooks
#     don't fire (parity with codex's [features].codex_hooks = false).
#   - enableAllProjectMcpServers = false — defence-in-depth; the
#     --strict-mcp-config CLI flag already enforces this, but the
#     setting hardens against accidental flag drop.
#   - includeCoAuthoredBy = false — no "Generated with Claude" trailer
#     if the agent ever commits.
#
# env block — parity with codex's config + a few Claude-only knobs:
#   CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = bundle that sets
#     DISABLE_AUTOUPDATER, DISABLE_FEEDBACK_COMMAND, DISABLE_TELEMETRY,
#     and DISABLE_ERROR_REPORTING.  Parity with codex
#     analytics.enabled/feedback.enabled/check_for_update_on_startup.
#   CLAUDE_CODE_SKIP_PROMPT_HISTORY = no transcript writes to
#     ~/.claude/projects/<hash>/<id>.jsonl (parity with codex
#     --ephemeral + history.persistence = "none").
#   CLAUDE_CODE_DISABLE_AUTO_MEMORY = belt-and-braces over the setting.
#   CLAUDE_CODE_DISABLE_ATTACHMENTS = no image attach (parity with
#     codex [tools].view_image = false).
#   CLAUDE_CODE_DISABLE_BACKGROUND_TASKS = parity with codex feature
#     toggles for prevent_idle_sleep / shell_snapshot / etc.
#   CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS = don't inject git context
#     into the system prompt; the puzzle workspace isn't a git repo.
#   CLAUDE_CODE_DISABLE_1M_CONTEXT = don't request the 1M extended
#     context (we cap at 256K below; matches codex setup).
#   CLAUDE_CODE_DISABLE_FAST_MODE = parity with codex fast_mode = false.
#   CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING = no automatic file undo
#     state; the agent shouldn't be writing files anyway under dontAsk.
#   DISABLE_COMPACT + CLAUDE_CODE_MAX_CONTEXT_TOKENS = auto-compact
#     OFF, context window pinned to 256K.  Per docs, DISABLE_COMPACT
#     only takes effect when MAX_CONTEXT_TOKENS is also set.  Parity
#     with codex model_auto_compact_token_limit + model_context_window.
settings = {
    "autoMemoryEnabled": False,
    "effortLevel": "xhigh",
    "disableAllHooks": True,
    "enableAllProjectMcpServers": False,
    "includeCoAuthoredBy": False,
    "permissions": {
        # dontAsk auto-denies anything not in `allow`, but the Claude
        # Code "read-only Bash + cwd reads" carve-out is built-in and
        # unconfigurable.  Per the permissions docs, deny rules are
        # evaluated FIRST (deny -> ask -> allow), so an explicit deny
        # SHOULD override the carve-out for these tool names.  Empirical
        # verification: smoke runs have seen the agent successfully
        # invoke `Bash` despite the dontAsk + allow shape below — the
        # deny block adds defence-in-depth even if Claude
        # Code's read-only Bash carve-out wins on `echo`-style calls.
        # ToolSearch is a Claude meta-tool used for schema lookup;
        # without it the agent in --print mode can't recover from
        # MCP-tool input-validation errors (observed empirically).
        "defaultMode": "dontAsk",
        "allow": ["mcp__yugi-bench__*", "ToolSearch"],
        "deny": [
            "Bash", "BashOutput", "KillShell",
            "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
            "Glob", "Grep",
            "Task", "Agent",
            "WebFetch", "WebSearch",
            "TodoWrite",
            "PowerShell",
        ],
    },
    "env": {
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_ATTACHMENTS": "1",
        "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
        "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
        # NOT setting CLAUDE_CODE_DISABLE_1M_CONTEXT — that env var would
        # force sonnet into the 200K standard window, but the V4 baseline
        # (api-eval/runner.py) imposes no explicit context cap, so V4
        # ran with its native 1M window available (peak input observed
        # ~130K).  For fair model-vs-model comparison, claude needs the
        # same headroom.  Empirically: ~80% of easy verified puzzles
        # overflowed at 200K because the per-turn game-state JSON ×
        # number-of-turns exceeded sonnet standard.
        "CLAUDE_CODE_DISABLE_FAST_MODE": "1",
        "CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING": "1",
        # Disable CLAUDE.md auto-discovery from cwd-up-to-root.  Without
        # this, a spawned `claude --print` from
        # ~/yugi-bench-runs/<ws>/ on a host that has a CLAUDE.md at ~
        # (e.g. an unrelated agent-doctrine file in the user's home)
        # walks the parent chain and injects that doctrine into the
        # agent's system prompt — even though the lockdown's
        # --tools=ToolSearch denies Read so the *files* the doctrine
        # references can't be loaded, the prose itself ends up in
        # context.  The launcher already pastes the workspace's
        # per-puzzle CLAUDE.md into the user prompt explicitly
        # (run-claude-exec.sh:PROMPT=$(cat CLAUDE.md)), so disabling
        # auto-discovery loses no useful behaviour.
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "DISABLE_COMPACT": "1",
        # 1M to match sonnet 4.6's extended context window (now that
        # CLAUDE_CODE_DISABLE_1M_CONTEXT is no longer forcing 200K).
        # DISABLE_COMPACT only takes effect when MAX_CONTEXT_TOKENS is
        # set, so we have to set this — and matching the V4 baseline's
        # implicit 1M lets puzzles use the same per-call ceiling on
        # both sides.  Codex caps at 256K because that's gpt-5.5's
        # actual native window in codex-cli; this is parity to the
        # MODEL's actual capacity, not numeric parity with the codex
        # value.
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000",
    },
}

# --- Bake auth into the workspace so it's self-contained for batch runs ---
# Parallel to codex's symlinking of ~/.codex/auth.json into the workspace's
# .codex/auth.json under CODEX_HOME.  Claude's case is platform-asymmetric:
#
# - On macOS, credentials live in the Keychain (per Anthropic's auth docs:
#   https://code.claude.com/docs/en/authentication).  The Keychain isn't a
#   file and can't be symlinked, AND the spawned `claude --print` subprocess
#   sometimes can't reach the Keychain in non-interactive contexts
#   (observed: "Not logged in" despite an interactive `claude` working).
# - On Linux/Windows, credentials are in ~/.claude/.credentials.json.  Could
#   be symlinked, but only if the launcher also sets CLAUDE_CONFIG_DIR to
#   redirect lookup — we don't currently, so a symlink alone wouldn't help.
#
# Universal fix: CLAUDE_CODE_OAUTH_TOKEN env var.  It sits at #5 in the
# auth-precedence chain (above #6 Keychain/file), works on every platform,
# and survives subprocess context.  User generates a long-lived token with
# `claude setup-token` (one-time).  We bake it into the workspace's
# settings.json env so the workspace is self-contained — no need to re-export
# the env var in every shell that runs the launcher.
oauth = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
if oauth:
    settings["env"]["CLAUDE_CODE_OAUTH_TOKEN"] = oauth

Path(f"{workspace}/.claude/settings.json").write_text(
    json.dumps(settings, indent=2) + "\n"
)

# Auth-detection note — print to stdout so the prep script's output makes
# the auth path obvious, same pattern as the codex prep.
if oauth:
    print(f"  auth: CLAUDE_CODE_OAUTH_TOKEN baked into settings.json env (self-contained)")
elif os.path.exists(os.path.expanduser("~/.claude/.credentials.json")):
    print(f"  auth: relying on ~/.claude/.credentials.json (Linux/Windows default)")
else:
    print(f"  auth: WARNING no obvious auth path detected.")
    print(f"        - macOS (Keychain auth doesn't survive subprocess): run `claude setup-token`")
    print(f"          to generate a 1-year OAuth token, then `export CLAUDE_CODE_OAUTH_TOKEN=<token>`")
    print(f"          in your shell rc and re-run prep.  See claude/README.md.")
    print(f"        - Linux/Windows: run `claude` to login (creates ~/.claude/.credentials.json),")
    print(f"          then re-run prep.")
PYEOF

# --- Write metadata ---
cat > "$WORKSPACE/metadata.json" <<EOF
{
  "puzzle_id": "$PUZZLE_ID",
  "started_at": "$TS",
  "agent": "claude",
  "max_tool_calls": $MAX_TOOL_CALLS,
  "auto_opponent": $([ "$AUTO_OPPONENT" -eq 1 ] && echo true || echo false),
  "image": "$IMAGE_TAG",
  "memory": "$MEMORY",
  "cpus": "$CPUS"
}
EOF

# --- Write CLAUDE.md (Claude Code auto-discovers this from cwd) ---
cat > "$WORKSPACE/CLAUDE.md" <<'CLAUDE_EOF'
# Yu-Gi-Oh puzzle

You're playing a single Yu-Gi-Oh puzzle through the `yugi-bench` MCP
server. The puzzle is winnable on this turn — ending your turn without
winning is an automatic loss.

When the user says "start", call `get_briefing` first. It returns the
puzzle's rules, the complete game state, the full card glossary, the
action grammar, and your initial pending decision.

Then play through using the engine response tools listed in the
briefing's grammar (`select_idlecmd`, `select_card`, `select_chain`,
`announce_race`, `select_battlecmd`, and the rest). Each tool call
returns the new game state.

Inspection tools (`get_state`, `pending_decision`, `get_glossary`)
don't consume the action budget; `restart` resets to puzzle initial
conditions.

The puzzle terminates with a `game_over` event from the engine — when
a tool result includes `_outcome`, you're done.

## Tool input typing — pass numbers, not strings

The MCP tool schemas use JSON Schema `integer`. Pass real integers
(`0`, `1`, `42`), not strings (`"0"`, `"1"`). Same for `null`: use
literal JSON `null`, not `"null"`. The engine returns
`Input validation error: '<value>' is not of type 'integer'` when
this is wrong; if you see that, re-emit the call with the correct
JSON type rather than retrying with the same shape.
CLAUDE_EOF

# --- Copy run-claude-exec.sh from the canonical exec launcher ---
cp "$EXEC_LAUNCHER" "$WORKSPACE/run-claude-exec.sh"
chmod +x "$WORKSPACE/run-claude-exec.sh"

# --- Print next steps ---
cat <<EOF
[prep] workspace prepared: $WORKSPACE

Next (manual):
  cd '$WORKSPACE'
  claude --strict-mcp-config --mcp-config ./.mcp.json
                            # launches Claude Code locked to this puzzle's
                            # MCP server only; CLAUDE.md is auto-discovered
                            # from cwd; .claude/settings.json applies the
                            # web-search/memory disables.
                            # type 'start' and let the agent play.

Next (automated, best-effort):
  bash '$WORKSPACE/run-claude-exec.sh'            # one workspace
  $REPO_ROOT/agent-mcp-eval/claude/run-batch.py   # all pending workspaces
                            # NOTE: claude --print historically misbehaved
                            # with MCP; verify on a single workspace before
                            # scaling up.

After the agent reaches game_over (or you decide to stop):
  - Close the Claude Code session (Ctrl-C, /exit, or Ctrl-D twice).
  - The container exits cleanly.
  - results/${PUZZLE_ID}.jsonl is the per-puzzle log.

Aggregate after all sessions:
  $REPO_ROOT/agent-mcp-eval/aggregate-results.py
EOF
