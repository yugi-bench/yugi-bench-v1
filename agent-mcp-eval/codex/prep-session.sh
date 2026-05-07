#!/usr/bin/env bash
# Prepare one isolated workspace for a single codex puzzle session.
# Cd into the workspace, then either:
#   - launch `codex` manually (with CODEX_HOME=$PWD/.codex), say "start"
#   - run ./run-codex-exec.sh for the same flow, fully scripted
#
# Each workspace contains:
#   - AGENTS.md — codex auto-discovers this from cwd; the agent's
#     instructions for this puzzle live here.
#   - .codex/config.toml — lockdown template (../codex/restrictions.toml.
#     template) merged with per-puzzle dynamic blocks (mcp_servers,
#     project trust, per-tool approval entries).  See the template's
#     header comment for the lockdown rationale + how to relax it.
#   - .codex/auth.json — symlink into ~/.codex/auth.json so OAuth login
#     is shared across workspaces.
#   - results/ — bind-mounted into the container; per-puzzle JSONL log
#     lands there automatically.
#   - metadata.json — kept for aggregate-results.py.
#   - run-codex-exec.sh — copied from ../codex/exec-launcher.sh.
#
# Multiple workspaces in different terminals run fully in parallel —
# each session has its own cwd, no shared state.
#
# Authentication (ChatGPT OAuth — preferred):
#   Run `codex login` once globally on the host.  That writes
#   ~/.codex/auth.json.  This prep script symlinks that file into
#   $WORKSPACE/.codex/auth.json.  Each workspace shares the global
#   OAuth credential — no per-session re-login.
#
# Authentication (OPENAI_API_KEY fallback):
#   If you don't want OAuth, set OPENAI_API_KEY in your shell instead.
#   Codex picks it up from the env when no auth.json is present.
#
# Usage:
#   ./prep-session.sh <puzzle_id> [opts]
#
# Options:
#   --max-tool-calls N          tool-call budget per session  (default 500)
#   --no-auto-opponent          skip opponent-side auto-decisions
#                               (default: --auto-opponent ON)
#   --image TAG                 container image tag           (default yugi-bench-env:latest)
#   --runs-root DIR             where to put workspaces       (default ~/yugi-bench-runs)
#   --memory SIZE               container memory cap          (default 1g)
#   --cpus N                    container CPU cap             (default 1.0)
#   --docker BIN                container runtime             (default $DOCKER env or 'docker')
#   --restrictions-template F   override the lockdown template path
#                               (default: agent-mcp-eval/codex/restrictions.toml.template)
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
RESTRICTIONS_TEMPLATE=""

while [ $# -gt 0 ]; do
    case "$1" in
        --max-tool-calls)         MAX_TOOL_CALLS="$2"; shift 2 ;;
        --no-auto-opponent)       AUTO_OPPONENT=0; shift ;;
        --auto-opponent)          AUTO_OPPONENT=1; shift ;;
        --image)                  IMAGE_TAG="$2"; shift 2 ;;
        --runs-root)              RUNS_ROOT="$2"; shift 2 ;;
        --memory)                 MEMORY="$2"; shift 2 ;;
        --cpus)                   CPUS="$2"; shift 2 ;;
        --docker)                 DOCKER_BIN="$2"; shift 2 ;;
        --restrictions-template)  RESTRICTIONS_TEMPLATE="$2"; shift 2 ;;
        *)                        echo "[prep-codex] unknown flag: $1" >&2; exit 2 ;;
    esac
done

# Resolve repo root (this script lives at agent-mcp-eval/codex/prep-session.sh)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DATASET="$REPO_ROOT/data/yugioh_bench.jsonl"
EXEC_LAUNCHER="$SCRIPT_DIR/exec-launcher.sh"
if [ -z "$RESTRICTIONS_TEMPLATE" ]; then
    RESTRICTIONS_TEMPLATE="$SCRIPT_DIR/restrictions.toml.template"
fi

# --- Validate puzzle id is in the dataset ---
if [ ! -f "$DATASET" ]; then
    echo "[prep-codex] ERROR: dataset missing at $DATASET" >&2
    exit 1
fi
if ! grep -q "\"instance_id\": \"$PUZZLE_ID\"" "$DATASET"; then
    echo "[prep-codex] ERROR: puzzle '$PUZZLE_ID' not found in $DATASET" >&2
    grep -o '"instance_id": "[^"]*"' "$DATASET" | head -5 >&2
    exit 1
fi

# --- Restrictions template + exec launcher presence ---
if [ ! -r "$RESTRICTIONS_TEMPLATE" ]; then
    echo "[prep-codex] ERROR: restrictions template not readable: $RESTRICTIONS_TEMPLATE" >&2
    exit 1
fi
if [ ! -r "$EXEC_LAUNCHER" ]; then
    echo "[prep-codex] ERROR: exec launcher missing: $EXEC_LAUNCHER" >&2
    exit 1
fi

# --- Image presence check ---
if ! "$DOCKER_BIN" image inspect "$IMAGE_TAG" >/dev/null 2>&1; then
    cat >&2 <<EOF
[prep-codex] ERROR: image '$IMAGE_TAG' not found.

Build it first:
    $REPO_ROOT/agent-mcp-eval/build-image.sh
EOF
    exit 1
fi

# --- Auth detection: prefer OAuth (~/.codex/auth.json), fall back to env var ---
GLOBAL_AUTH="$HOME/.codex/auth.json"
HAS_OAUTH=0
[ -f "$GLOBAL_AUTH" ] && HAS_OAUTH=1
HAS_API_KEY=0
[ -n "${OPENAI_API_KEY:-}" ] && HAS_API_KEY=1

if [ "$HAS_OAUTH" -eq 0 ] && [ "$HAS_API_KEY" -eq 0 ]; then
    cat >&2 <<EOF
[prep-codex] WARNING: no auth detected.

Codex CLI needs either:
  - ChatGPT OAuth (recommended): run \`codex login\` once globally.
    That writes ~/.codex/auth.json which this script will symlink
    into each workspace's .codex/.
  - OPENAI_API_KEY env var: export it in your shell before launching.

Workspace will be prepared anyway; resolve auth before launching.
EOF
fi

# --- Codex binary check ---
if ! command -v codex >/dev/null 2>&1; then
    cat >&2 <<EOF
[prep-codex] WARNING: 'codex' binary not on PATH.

Install with:    npm i -g @openai/codex     # or:    brew install codex
EOF
fi

# --- Prepare workspace ---
TS="$(date +%Y-%m-%dT%H-%M-%S)"
WORKSPACE="$RUNS_ROOT/${PUZZLE_ID}-${TS}-codex"
mkdir -p "$WORKSPACE/results" "$WORKSPACE/.codex"

# --- Symlink the global OAuth credential into the workspace's .codex/ ---
if [ "$HAS_OAUTH" -eq 1 ]; then
    ln -sf "$GLOBAL_AUTH" "$WORKSPACE/.codex/auth.json"
    echo "[prep-codex] linked global auth.json into workspace's .codex/"
fi

# --- Build .codex/config.toml = restrictions template + dynamic block ---
python3 - "$WORKSPACE" "$DOCKER_BIN" "$IMAGE_TAG" "$PUZZLE_ID" \
        "$MAX_TOOL_CALLS" "$AUTO_OPPONENT" "$MEMORY" "$CPUS" \
        "$RESTRICTIONS_TEMPLATE" <<'PYEOF'
import os
import sys
from pathlib import Path

(workspace, docker_bin, image_tag, puzzle_id,
 max_tool_calls, auto_opp, memory, cpus,
 restrictions_template) = sys.argv[1:]

# ─── Read + substitute the lockdown template ───
template = Path(restrictions_template).read_text()
template = template.replace("__WORKSPACE__", workspace)

# ─── Build the dynamic per-puzzle block ───
docker_args = ["run", "-i", "--rm", "--network", "none"]
# Rootless podman uid-map fix; Docker Desktop on macOS doesn't need it.
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

# Canonical yugi-bench tool surface — every tool exposed by the
# container in default mode (engine.tools.TOOLS filtered through
# agent-mcp-eval.server._container_tool_set: drops inspect_card; adds
# get_briefing). 25 entries.
yugi_bench_tools = [
    # Bootstrap
    "get_briefing",
    # Inspection (free, no budget cost)
    "get_state", "pending_decision", "get_glossary",
    # Meta
    "restart",
    # Response verbs (consume the action budget)
    "select_battlecmd", "select_idlecmd", "select_effectyn",
    "select_yesno", "select_option", "select_card",
    "select_card_codes", "select_unselect_card", "select_chain",
    "select_place", "select_position", "select_tribute",
    "select_counter", "select_sum", "sort_card",
    "announce_race", "announce_attribute", "announce_card",
    "announce_number", "rock_paper_scissors",
]

def toml_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

def toml_array(items):
    return "[" + ", ".join(toml_str(x) for x in items) + "]"

dynamic_lines = [
    "",
    "# ============================================================",
    "# Dynamic per-puzzle block (generated by codex/prep-session.sh)",
    "# Workspace path + MCP server wiring + project trust + per-tool",
    "# approval entries.  The lockdown profile above is from the static",
    "# template at agent-mcp-eval/codex/restrictions.toml.template.",
    "# ============================================================",
    "",
    "# ─── Trust THIS workspace (skip first-launch trust prompt) ───",
    f"[projects.{toml_str(workspace)}]",
    'trust_level = "trusted"',
    "",
    "# ─── MCP server: yugi-bench (the ONLY tool surface) ───",
    "[mcp_servers.yugi-bench]",
    f"command = {toml_str(docker_bin)}",
    f"args = {toml_array(docker_args)}",
    "startup_timeout_sec = 120",
    'default_tools_approval_mode = "approve"',
    f"enabled_tools = {toml_array(yugi_bench_tools)}",
    "",
]

# Per-tool pre-approval entries (belt-and-braces with default).
for tool_name in yugi_bench_tools:
    dynamic_lines.append(f"[mcp_servers.yugi-bench.tools.{tool_name}]")
    dynamic_lines.append('approval_mode = "approve"')
    dynamic_lines.append("")

merged = template.rstrip() + "\n" + "\n".join(dynamic_lines)
Path(f"{workspace}/.codex/config.toml").write_text(merged)
PYEOF

# --- Write metadata (kept for aggregate-results.py) ---
cat > "$WORKSPACE/metadata.json" <<EOF
{
  "puzzle_id": "$PUZZLE_ID",
  "started_at": "$TS",
  "agent": "codex",
  "max_tool_calls": $MAX_TOOL_CALLS,
  "auto_opponent": $([ "$AUTO_OPPONENT" -eq 1 ] && echo true || echo false),
  "image": "$IMAGE_TAG",
  "memory": "$MEMORY",
  "cpus": "$CPUS"
}
EOF

# --- Write AGENTS.md (codex auto-discovers this from cwd) ---
cat > "$WORKSPACE/AGENTS.md" <<'AGENTS_EOF'
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
AGENTS_EOF

# --- Copy run-codex-exec.sh from the canonical exec launcher ---
cp "$EXEC_LAUNCHER" "$WORKSPACE/run-codex-exec.sh"
chmod +x "$WORKSPACE/run-codex-exec.sh"

# --- Print next steps ---
cat <<EOF
[prep-codex] workspace prepared: $WORKSPACE

Next (manual):
  cd '$WORKSPACE'
  CODEX_HOME=\$PWD/.codex codex   # launches the codex TUI
                                 # type 'start' and let the agent play

Next (automated, equivalent flow):
  bash '$WORKSPACE/run-codex-exec.sh'             # one workspace
  $REPO_ROOT/agent-mcp-eval/codex/run-batch.py    # all pending workspaces

Auth: $([ "$HAS_OAUTH" -eq 1 ] && echo "OAuth (~/.codex/auth.json) symlinked into workspace." \
       || ([ "$HAS_API_KEY" -eq 1 ] && echo "OPENAI_API_KEY in env will be used." \
           || echo "NO AUTH DETECTED — run \`codex login\` globally before launching."))

When the agent reaches game_over (or you decide to stop):
  - Close the codex session.
  - The container exits cleanly.
  - results/${PUZZLE_ID}.jsonl is the per-puzzle log.

Aggregate after all sessions:
  $REPO_ROOT/agent-mcp-eval/aggregate-results.py
EOF
