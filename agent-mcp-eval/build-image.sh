#!/usr/bin/env bash
# Build the yugi-bench-env image from scratch on a fresh host (any
# machine with just Docker installed).
#
# Uses the multi-stage self-contained Dockerfile under
# agent-mcp-eval/Dockerfile — no pre-staged vendor/ or data/ required,
# no premake5 / g++ on the host.  The Dockerfile clones libocgcore,
# BabelCDB, CardScripts, and ProjectIgnis/Puzzles inside the build,
# then materialises data/yugioh_bench.jsonl in a Python stage so the
# runtime image is fully self-contained.
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

echo "[build-image] $DOCKER build → $TAG (via $DOCKERFILE)"
echo "[build-image] this clones + compiles libocgcore + builds the lean"
echo "[build-image] dataset inside the image; takes ~3-5 min on first run,"
echo "[build-image] sub-second after layer cache."
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
