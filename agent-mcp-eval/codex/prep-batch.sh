#!/usr/bin/env bash
# Codex prep-batch — pick N puzzles by strategy, prep one codex
# workspace per puzzle.  Calls codex/prep-session.sh for each puzzle.
# Puzzle selection is delegated to ../_lib/pick_puzzles.py (shared
# with claude/prep-batch.sh).
#
# Usage:
#   ./prep-batch.sh --count 10
#   ./prep-batch.sh --count 5  --strategy random
#   ./prep-batch.sh --count 5  --strategy verified-easy
#   ./prep-batch.sh --strategy list:yugioh_puzzle_42ffb7a8,yugioh_puzzle_044c693a
#
# Strategies (operate on full 217 by default; --strategy verified-* opts in
# to the 133-puzzle Konami-gold subset):
#   easy            verified puzzles first (easiest -> hardest), then
#                   non-verified (easiest -> hardest), tie-break by id;
#                   take first --count                                      (default)
#   random          random sample of the full 217
#   all             every puzzle in the full 217 in easy-order (ignores --count)
#   verified-easy   sort the 133 verified-subset puzzles by complexity
#                   ascending, take the first --count                       (opt-in)
#   verified        random sample of the 133 verified-subset                (opt-in)
#   list:ID,ID,...  explicit comma-separated puzzle_id list                 (count = len(list))
#
# Common flags forwarded to codex/prep-session.sh:
#   --max-tool-calls N      tool budget per session       (default 500)
#   --no-auto-opponent      stricter: no passive-opponent (default ON)
#   --image TAG             container image tag           (default yugi-bench-env:latest)
#   --runs-root DIR         where to put workspaces       (default ~/yugi-bench-runs)
set -euo pipefail

COUNT=999
STRATEGY="easy"
declare -a FORWARD_ARGS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --count)            COUNT="$2"; shift 2 ;;
        --strategy)         STRATEGY="$2"; shift 2 ;;
        --max-tool-calls)   FORWARD_ARGS+=("$1" "$2"); shift 2 ;;
        --image)            FORWARD_ARGS+=("$1" "$2"); shift 2 ;;
        --runs-root)        FORWARD_ARGS+=("$1" "$2"); shift 2 ;;
        --memory)           FORWARD_ARGS+=("$1" "$2"); shift 2 ;;
        --cpus)             FORWARD_ARGS+=("$1" "$2"); shift 2 ;;
        --docker)           FORWARD_ARGS+=("$1" "$2"); shift 2 ;;
        --restrictions-template) FORWARD_ARGS+=("$1" "$2"); shift 2 ;;
        --no-auto-opponent) FORWARD_ARGS+=("$1"); shift ;;
        --auto-opponent)    FORWARD_ARGS+=("$1"); shift ;;
        --help|-h)          sed -n '1,/^set -euo pipefail/p' "$0" | sed -e '$d' -e 's/^# \?//'; exit 0 ;;
        *)                  echo "[codex/prep-batch] unknown flag: $1" >&2; exit 2 ;;
    esac
done

# Resolve repo root (this script at agent-mcp-eval/codex/prep-batch.sh, so ../../..)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
DATASET="$REPO_ROOT/data/yugioh_bench.jsonl"
VERIFIED="$REPO_ROOT/data/yugioh_bench_verified.jsonl"
PICK_PUZZLES="$SCRIPT_DIR/../_lib/pick_puzzles.py"
PREP="$SCRIPT_DIR/prep-session.sh"

[ -f "$DATASET" ] || { echo "[codex/prep-batch] ERROR: dataset missing at $DATASET" >&2; exit 1; }
[ -r "$PREP" ]    || { echo "[codex/prep-batch] ERROR: prep-session.sh not readable: $PREP" >&2; exit 1; }
[ -r "$PICK_PUZZLES" ] || { echo "[codex/prep-batch] ERROR: pick_puzzles.py not readable: $PICK_PUZZLES" >&2; exit 1; }

# --- Pick puzzle ids by strategy ---
PUZZLE_IDS=$(python3 "$PICK_PUZZLES" "$STRATEGY" "$COUNT" "$DATASET" "$VERIFIED" "$REPO_ROOT")
if [ -z "$PUZZLE_IDS" ]; then
    echo "[codex/prep-batch] no puzzles selected" >&2
    exit 1
fi

NUM=$(echo "$PUZZLE_IDS" | wc -l | tr -d ' ')
echo "[codex/prep-batch] strategy=$STRATEGY → $NUM puzzles"
echo

## Macs ship bash 3.2 (2007).  The ${arr[@]+"${arr[@]}"} conditional-
## array-expansion idiom parses inconsistently on 3.2 (#syntax-error-
## near-unexpected-token-paren).  Use explicit empty-array branching
## instead so the same script runs cleanly on linux bash 5.x + macOS.
i=0
declare -a WORKSPACES=()
while IFS= read -r pid; do
    i=$((i + 1))
    echo "[codex/prep-batch] [$i/$NUM] preparing $pid ..."
    if [ ${#FORWARD_ARGS[@]} -gt 0 ]; then
        out=$(bash "$PREP" "$pid" "${FORWARD_ARGS[@]}")
    else
        out=$(bash "$PREP" "$pid")
    fi
    ws=$(echo "$out" | awk '/workspace prepared:/ {print $4}')
    WORKSPACES+=("$ws")
done <<< "$PUZZLE_IDS"

echo
echo "[codex/prep-batch] ALL $NUM workspaces ready."
echo
echo "Manual flow — open a terminal per session, or one at a time:"
for ws in "${WORKSPACES[@]}"; do
    echo "  cd '$ws' && CODEX_HOME=\$PWD/.codex codex"
done
echo
echo "Auto-driven flow (single workspace):"
## Bash-3.2-safe iteration: avoid ${arr[@]:offset:length} array slicing
## which fails to parse on macOS's stock bash.
n_show=${#WORKSPACES[@]}
if [ "$n_show" -gt 3 ]; then n_show=3; fi
idx=0
while [ "$idx" -lt "$n_show" ]; do
    echo "  bash '${WORKSPACES[$idx]}/run-codex-exec.sh'"
    idx=$((idx + 1))
done
n_extra=$(( ${#WORKSPACES[@]} - 3 ))
if [ "$n_extra" -gt 0 ]; then echo "  ... ($n_extra more)"; fi
echo
echo "Auto-driven flow (every pending codex workspace, idempotent):"
echo "  $SCRIPT_DIR/run-batch.py"
echo
echo "When all sessions are done:"
echo "  $REPO_ROOT/agent-mcp-eval/aggregate-results.py"
