#!/usr/bin/env bash
# Yugi-bench one-step setup.
#
# Clones pinned upstream sources into ./vendor/, builds libocgcore,
# installs Python deps via uv (or pip), and verifies the harness
# end-to-end via engine.replay against the shipped solutions/ —
# zero API calls, zero spend.
#
# All steps are idempotent. Safe to re-run.
#
# Usage:
#   ./setup.sh                          # install + verify
#   ./setup.sh --skip-pip               # skip Python dep install
#   ./setup.sh --no-verify              # skip the post-install replay
#   ./setup.sh --force                  # re-clone + rebuild even if present
#   ./setup.sh --help
#
# Env-var overrides (rarely needed):
#   OCGCORE_REF / BABEL_REF / SCRIPTS_REF / PUZZLES_REF — pin a different commit
#   OCGCORE_REPO / BABEL_REPO / SCRIPTS_REPO / PUZZLES_REPO — point at a fork
#   JOBS — parallel build jobs (default: nproc)
#   PYTHON — python interpreter (default: python3)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")" && pwd)"
VENDOR="$REPO_ROOT/vendor"

# Pinned upstream commits captured 2026-05-03 against current master.
# Bump these intentionally; do not silently follow upstream master, or
# replay results stop being reproducible.
OCGCORE_REPO="${OCGCORE_REPO:-https://github.com/edo9300/ygopro-core.git}"
OCGCORE_REF="${OCGCORE_REF:-a7992f4563a4cf992369f0e5b7efb8dd4a0b1c4a}"
BABEL_REPO="${BABEL_REPO:-https://github.com/ProjectIgnis/BabelCDB.git}"
BABEL_REF="${BABEL_REF:-554dc41016dd02a74848df5fe54bda3567c86e78}"
SCRIPTS_REPO="${SCRIPTS_REPO:-https://github.com/ProjectIgnis/CardScripts.git}"
SCRIPTS_REF="${SCRIPTS_REF:-2d9697a56387c1b3dabd4780eea5aaf0bcbc39af}"
PUZZLES_REPO="${PUZZLES_REPO:-https://github.com/ProjectIgnis/Puzzles.git}"
PUZZLES_REF="${PUZZLES_REF:-1177a180dd237da7f9703c846d533c2116ca1439}"

JOBS="${JOBS:-$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null || echo 4)}"
PYTHON="${PYTHON:-python3}"

SKIP_PIP=0
NO_VERIFY=0
FORCE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --skip-pip) SKIP_PIP=1 ;;
        --no-verify) NO_VERIFY=1 ;;
        --force) FORCE=1 ;;
        --help|-h) sed -n '1,/^set -euo pipefail/p' "$0" \
                       | sed -e '$d' -e 's/^# \?//'; exit 0 ;;
        *) echo "unknown flag: $1 (try --help)" >&2; exit 2 ;;
    esac
    shift
done

log()  { printf '[setup] %s\n' "$*"; }
die()  { printf '[setup] ERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing required tool: $1${2:+ ($2)}"; }

# --- 1. Tool check ---
log "checking required tools..."
need git
need g++       "C++17 compiler — Debian/Ubuntu: 'apt install build-essential' / Fedora: 'dnf install gcc-c++' / macOS: 'xcode-select --install'"
need make
need premake5  "premake5 — Debian: 'apt install premake4' isn't enough, install premake5 from https://premake.github.io/ or your package manager"
need "$PYTHON" "Python 3.11 or newer"

DYLIB_SUFFIX="libocgcore.so"
[ "$(uname -s)" = "Darwin" ] && DYLIB_SUFFIX="libocgcore.dylib"

# --- 2. ygopro-core: clone (recursively, for the lua submodule) + build ---
OCGCORE_DIR="$VENDOR/ygopro-core"
DYLIB_OUT="$OCGCORE_DIR/bin/release/$DYLIB_SUFFIX"

if [ -f "$DYLIB_OUT" ] && [ "$FORCE" -eq 0 ]; then
    log "ocgcore already built: $DYLIB_OUT (skip; --force to rebuild)"
else
    mkdir -p "$VENDOR" "$OCGCORE_DIR"
    if [ ! -d "$OCGCORE_DIR/.git" ]; then
        log "fetching ygopro-core @ ${OCGCORE_REF:0:8} (shallow, by SHA)..."
        ( cd "$OCGCORE_DIR" \
            && git -c core.filemode=false init --quiet \
            && git -c core.filemode=false remote add origin "$OCGCORE_REPO" \
            && git -c core.filemode=false fetch --depth 1 --quiet origin "$OCGCORE_REF" \
            && git -c core.filemode=false checkout --quiet FETCH_HEAD \
            && git -c core.filemode=false submodule update --init --recursive --depth 1 --quiet )
    else
        log "ygopro-core already populated"
    fi

    log "generating build files (premake5 gmake2)..."
    ( cd "$OCGCORE_DIR" && premake5 gmake2 >/dev/null )
    log "building ocgcoreshared (config=release, jobs=$JOBS)..."
    make -C "$OCGCORE_DIR/build" ocgcoreshared config=release -j"$JOBS"
    [ -f "$DYLIB_OUT" ] || die "build finished but $DYLIB_OUT is missing"
fi

# --- 3. BabelCDB: copy the .cdb files we use into vendor/distribution/expansions/ ---
DB_DIR="$VENDOR/distribution/expansions"
if [ -f "$DB_DIR/cards.cdb" ] && [ "$FORCE" -eq 0 ]; then
    log "card DB already in place (skip)"
else
    log "fetching BabelCDB @ ${BABEL_REF:0:8} (shallow, by SHA)..."
    tmp="$(mktemp -d)"
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp'" EXIT
    ( cd "$tmp" \
        && git -c core.filemode=false init --quiet \
        && git -c core.filemode=false remote add origin "$BABEL_REPO" \
        && git -c core.filemode=false fetch --depth 1 --quiet origin "$BABEL_REF" \
        && git -c core.filemode=false checkout --quiet FETCH_HEAD )
    mkdir -p "$DB_DIR"
    cp "$tmp/cards.cdb" "$DB_DIR/"
    for opt in cards-rush.cdb cards-skills.cdb cards-skills-unofficial.cdb \
               cards-unofficial.cdb cards-unofficial-new.cdb \
               goat-entries.cdb; do
        [ -f "$tmp/$opt" ] && cp "$tmp/$opt" "$DB_DIR/" || true
    done
    log "  installed $(ls "$DB_DIR" | wc -l) .cdb files"
fi

# --- 4. CardScripts: clone directly as distribution/script ---
SCRIPT_DIR="$VENDOR/distribution/script"
if [ -d "$SCRIPT_DIR/.git" ] && [ "$FORCE" -eq 0 ]; then
    log "scripts already in place (skip; --force to refresh)"
else
    [ -e "$SCRIPT_DIR" ] && rm -rf "$SCRIPT_DIR"
    log "fetching CardScripts @ ${SCRIPTS_REF:0:8} (shallow, by SHA)..."
    mkdir -p "$SCRIPT_DIR"
    ( cd "$SCRIPT_DIR" \
        && git -c core.filemode=false init --quiet \
        && git -c core.filemode=false remote add origin "$SCRIPTS_REPO" \
        && git -c core.filemode=false fetch --depth 1 --quiet origin "$SCRIPTS_REF" \
        && git -c core.filemode=false checkout --quiet FETCH_HEAD )
fi

# --- 5. Puzzles: clone ProjectIgnis/Puzzles as the upstream source the
#        full benchmark builds from (build_benchmark.py defaults to
#        --puzzle-root vendor/puzzles).
PUZZLES_DIR="$VENDOR/puzzles"
if [ -d "$PUZZLES_DIR/.git" ] && [ "$FORCE" -eq 0 ]; then
    log "puzzles already in place (skip; --force to refresh)"
else
    [ -e "$PUZZLES_DIR" ] && rm -rf "$PUZZLES_DIR"
    log "fetching Puzzles @ ${PUZZLES_REF:0:8} (shallow, by SHA)..."
    mkdir -p "$PUZZLES_DIR"
    ( cd "$PUZZLES_DIR" \
        && git -c core.filemode=false init --quiet \
        && git -c core.filemode=false remote add origin "$PUZZLES_REPO" \
        && git -c core.filemode=false fetch --depth 1 --quiet origin "$PUZZLES_REF" \
        && git -c core.filemode=false checkout --quiet FETCH_HEAD )
fi

# --- 6. Python deps (uv if available, else pip) ---
if [ "$SKIP_PIP" -eq 1 ]; then
    log "skipping Python deps (--skip-pip)"
else
    if command -v uv >/dev/null 2>&1; then
        log "installing Python deps via uv..."
        uv pip install -r "$REPO_ROOT/requirements.txt"
    else
        log "uv not found, falling back to pip (consider installing uv: https://docs.astral.sh/uv/)..."
        "$PYTHON" -m pip install -r "$REPO_ROOT/requirements.txt"
    fi
fi

# --- 7. Build the lean dataset locally from upstream Puzzles ---
# Nothing Konami-derived ships from this repo.  build_benchmark.py
# walks vendor/puzzles/ (ProjectIgnis/Puzzles upstream cloned in step 5)
# and produces the full 217-puzzle data/yugioh_bench.jsonl.  --lean
# omits card_details + the rendered prompt; those Konami-derived bulk
# text fields are joined back locally in step 8 from the user's
# BabelCDB clone in vendor/distribution/expansions.
LEAN_JSONL="$REPO_ROOT/data/yugioh_bench.jsonl"
VERIFIED_JSONL="$REPO_ROOT/data/yugioh_bench_verified.jsonl"
mkdir -p "$REPO_ROOT/data"
if [ ! -f "$LEAN_JSONL" ] || [ "$FORCE" -eq 1 ]; then
    log "building lean dataset from vendor/puzzles ($LEAN_JSONL)..."
    OW=""
    [ -f "$LEAN_JSONL" ] && OW="--overwrite"
    "$PYTHON" "$REPO_ROOT/src/dataset/build_benchmark.py" --lean $OW
else
    log "lean dataset already present: $LEAN_JSONL (skip; --force to regenerate)"
fi
if [ ! -f "$VERIFIED_JSONL" ] || [ "$LEAN_JSONL" -nt "$VERIFIED_JSONL" ] || [ "$FORCE" -eq 1 ]; then
    log "building verified subset ($VERIFIED_JSONL)..."
    "$PYTHON" "$REPO_ROOT/src/dataset/build_verified_subset.py"
else
    log "verified subset already present: $VERIFIED_JSONL (skip; --force to regenerate)"
fi

# --- 8. Reconstitute the Konami-derived bulk text fields ---
# src/dataset/enrich.py joins card_details + the rendered prompt back
# in from the user's local BabelCDB clone (vendor/distribution/expansions).
# The runner prefers data/yugioh_bench.enriched.jsonl when it exists.
ENRICHED_JSONL="$REPO_ROOT/data/yugioh_bench.enriched.jsonl"
if [ -f "$LEAN_JSONL" ]; then
    if [ ! -f "$ENRICHED_JSONL" ] || [ "$LEAN_JSONL" -nt "$ENRICHED_JSONL" ] || [ "$FORCE" -eq 1 ]; then
        log "enriching dataset from local BabelCDB ($LEAN_JSONL -> $ENRICHED_JSONL)..."
        "$PYTHON" "$REPO_ROOT/src/dataset/enrich.py" \
            --input "$LEAN_JSONL" --output "$ENRICHED_JSONL"
    else
        log "enriched dataset already present: $ENRICHED_JSONL (skip; --force to regenerate)"
    fi
else
    log "no lean dataset at $LEAN_JSONL; skip enrichment"
fi

# --- 9. Verify via offline replay (no API key needed) ---
if [ "$NO_VERIFY" -eq 1 ]; then
    log "skipping verification (--no-verify)"
else
    log "verifying harness end-to-end via engine.replay (replay, no API)..."
    cd "$REPO_ROOT"
    if ! "$PYTHON" src/engine/replay.py \
            --solutions solutions \
            --only yugioh_puzzle_42ffb7a8 ; then
        die "verification failed — engine wiring problem.  Re-run with --no-verify to skip and investigate manually."
    fi
fi

log "DONE"
cat <<EOF

vendor/ artifacts:
  $DYLIB_OUT
  $DB_DIR/cards.cdb
  $SCRIPT_DIR/
  $PUZZLES_DIR/

data/ artifacts (gitignored, locally built):
  $LEAN_JSONL
  $VERIFIED_JSONL
  $ENRICHED_JSONL

Next:
  export DEEPSEEK_API_KEY=...   (or ANTHROPIC_API_KEY / OPENAI_API_KEY)
  python api-eval/runner.py --provider deepseek --model deepseek-v4-pro --offset 0 --limit 5

EOF
