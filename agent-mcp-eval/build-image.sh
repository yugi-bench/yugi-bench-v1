#!/usr/bin/env bash
# Build the yugi-bench-env image from scratch on a fresh host (any
# machine with just Docker installed).
#
# Uses the multi-stage self-contained Dockerfile under
# agent-mcp-eval/Dockerfile — no pre-staged vendor/
# directory required, no premake5 / g++ on the host.
#
# Usage:
#   ./agent-mcp-eval/build-image.sh                    # tag yugi-bench-env:latest
#   ./agent-mcp-eval/build-image.sh mytag              # custom tag
#   DOCKER=podman ./agent-mcp-eval/build-image.sh      # podman drop-in
set -euo pipefail

TAG="${1:-yugi-bench-env:latest}"
DOCKER="${DOCKER:-docker}"

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DOCKERFILE="$REPO_ROOT/agent-mcp-eval/Dockerfile"

if ! command -v "$DOCKER" >/dev/null 2>&1; then
    cat >&2 <<EOF
[build-image] ERROR: '$DOCKER' not found on PATH.

On macOS, install Docker Desktop:
    https://www.docker.com/products/docker-desktop/

Or override the binary:
    DOCKER=podman ./agent-mcp-eval/build-image.sh
EOF
    exit 1
fi

if [ ! -f "$DOCKERFILE" ]; then
    echo "[build-image] ERROR: Dockerfile missing at $DOCKERFILE" >&2
    exit 1
fi

if [ ! -f "$REPO_ROOT/data/yugioh_bench.jsonl" ]; then
    cat >&2 <<EOF
[build-image] ERROR: data/yugioh_bench.jsonl is missing.

Nothing Konami-derived ships from this repo, so the dataset has to be
built locally before the image can copy it in.  Run:

    ./setup.sh

This populates vendor/ (libocgcore + BabelCDB + CardScripts +
ProjectIgnis/Puzzles) and produces data/yugioh_bench.jsonl from the
upstream Puzzles clone.  Then re-run this script.
EOF
    exit 1
fi

echo "[build-image] $DOCKER build → $TAG (via $DOCKERFILE)"
echo "[build-image] this clones + compiles libocgcore inside the build,"
echo "[build-image] takes ~3-5 min on first run, sub-second after layer cache."
echo

cd "$REPO_ROOT"
"$DOCKER" build \
    --tag "$TAG" \
    --file "$DOCKERFILE" \
    .

echo
echo "[build-image] image built: $TAG"
echo
echo "Quick smoke (no agent, just verify the image starts cleanly):"
echo "    $DOCKER run --rm $TAG --help"
echo
echo "Prep one puzzle session:"
echo "    ./agent-mcp-eval/codex/prep-session.sh yugioh_puzzle_42ffb7a8"
echo
echo "Prep a batch of N puzzles (full 217 by default; --strategy verified-easy"
echo "for the 133-puzzle Konami-gold subset):"
echo "    ./agent-mcp-eval/codex/prep-batch.sh --count 10"
