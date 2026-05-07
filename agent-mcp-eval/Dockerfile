# syntax=docker/dockerfile:1.6
#
# Self-contained yugi-bench environment image — for host installs
# (any machine with just Docker installed; no host build tooling needed).
#
# Builds libocgcore.so + clones BabelCDB + CardScripts + Puzzles inside
# the build, then materialises the lean data/yugioh_bench.jsonl in a
# Python stage so the runtime image is self-contained: nothing needs to
# be pre-staged on the host. Only Docker Desktop required.
#
# Build:
#   docker build -f agent-mcp-eval/Dockerfile \
#       -t yugi-bench-env:latest .
#
# Pinned upstream commits match setup.sh.  To update, bump the ARGs
# below intentionally — replay-result reproducibility depends on stable
# engine + script + DB + puzzles pins.

# ---------- Stage 1: builder (engine + assets) ----------
FROM debian:13-slim AS builder

# Note: premake5 is NOT in Debian's package archives (as of Debian 13).
# Upstream setup.sh tells the user to install it from premake.github.io
# manually. We mirror that here by building premake5 from source — safer
# than trusting an Ubuntu universe package whose version drift could
# break ygopro-core's premake script in the future.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        git \
        ca-certificates \
        uuid-dev \
    && rm -rf /var/lib/apt/lists/*

ARG PREMAKE_REPO=https://github.com/premake/premake-core.git
ARG PREMAKE_REF=v5.0.0-beta3
ARG OCGCORE_REPO=https://github.com/edo9300/ygopro-core.git
ARG OCGCORE_REF=a7992f4563a4cf992369f0e5b7efb8dd4a0b1c4a
ARG BABEL_REPO=https://github.com/ProjectIgnis/BabelCDB.git
ARG BABEL_REF=554dc41016dd02a74848df5fe54bda3567c86e78
ARG SCRIPTS_REPO=https://github.com/ProjectIgnis/CardScripts.git
ARG SCRIPTS_REF=2d9697a56387c1b3dabd4780eea5aaf0bcbc39af
ARG PUZZLES_REPO=https://github.com/ProjectIgnis/Puzzles.git
ARG PUZZLES_REF=1177a180dd237da7f9703c846d533c2116ca1439
# ocgcore's field.cpp compile peaks at 2-4 GB RAM per job. Default to 2
# parallel jobs — fits comfortably in a 4 GB VM (Podman Machine's
# default is 2 GB which is too small even single-job). Override with
# `--build-arg JOBS=$(nproc)` on hosts with 16+ GB.
ARG JOBS=2

WORKDIR /build

# 0. Build premake5 from source. Bootstrap.mak's `linux` target uses
#    only gcc + make + a vendored Lua, so this works on linux/amd64
#    AND linux/arm64 with no extra deps.
RUN git -c core.filemode=false init premake \
 && cd premake \
 && git remote add origin "$PREMAKE_REPO" \
 && git fetch --depth 1 origin "$PREMAKE_REF" \
 && git checkout FETCH_HEAD \
 && git submodule update --init --recursive --depth 1 \
 && make -f Bootstrap.mak linux \
 && install -m 0755 bin/release/premake5 /usr/local/bin/premake5 \
 && cd .. && rm -rf premake

# 1. ygopro-core (with the lua submodule) at the pinned SHA.
RUN git -c core.filemode=false init ygopro-core \
 && cd ygopro-core \
 && git remote add origin "$OCGCORE_REPO" \
 && git fetch --depth 1 origin "$OCGCORE_REF" \
 && git checkout FETCH_HEAD \
 && git submodule update --init --recursive --depth 1

# 2. Build libocgcore.so via premake5 + make. Parallelism is bounded by
#    the JOBS ARG (default 2) — ocgcore's field.cpp peaks at 2-4 GB RAM
#    per parallel job, so going wide on a memory-constrained VM
#    (Podman Machine's 2 GB default in particular) gets OOM-killed.
RUN cd ygopro-core \
 && premake5 gmake2 \
 && make -C build ocgcoreshared config=release -j"${JOBS}"

# 3. BabelCDB for the card DB.
RUN git -c core.filemode=false init babel \
 && cd babel \
 && git remote add origin "$BABEL_REPO" \
 && git fetch --depth 1 origin "$BABEL_REF" \
 && git checkout FETCH_HEAD

# 4. CardScripts for the per-card Lua effects.
RUN git -c core.filemode=false init scripts \
 && cd scripts \
 && git remote add origin "$SCRIPTS_REPO" \
 && git fetch --depth 1 origin "$SCRIPTS_REF" \
 && git checkout FETCH_HEAD

# 5. Puzzles bundle — upstream Lua initial-state scripts that
#    build_benchmark.py walks to produce data/yugioh_bench.jsonl.
RUN git -c core.filemode=false init puzzles \
 && cd puzzles \
 && git remote add origin "$PUZZLES_REPO" \
 && git fetch --depth 1 origin "$PUZZLES_REF" \
 && git checkout FETCH_HEAD

# ---------- Stage 2: dataset-builder (Python; produces lean JSONL) ----------
FROM python:3.11-slim AS dataset-builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        libstdc++6 \
    && rm -rf /var/lib/apt/lists/*

# Lay out vendor/ in the same shape engine.core's _first_existing()
# search expects, so build_benchmark.py runs unmodified.
WORKDIR /work
COPY --from=builder /build/ygopro-core/bin/release/libocgcore.so \
                    /work/vendor/ygopro-core/bin/release/libocgcore.so
COPY --from=builder /build/babel/cards.cdb \
                    /work/vendor/distribution/expansions/cards.cdb
COPY --from=builder /build/scripts/ /work/vendor/distribution/script/
COPY --from=builder /build/puzzles/ /work/vendor/puzzles/

# Pull in the dataset-author code from the build context.
COPY src/ /work/src/

ENV PYTHONPATH=/work/src
RUN python /work/src/dataset/build_benchmark.py \
        --output /work/data/yugioh_bench.jsonl \
        --lean \
        --no-example \
 && python /work/src/dataset/build_verified_subset.py

# ---------- Stage 3: runtime ----------
FROM python:3.11-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    YGO_BENCH_ROOT=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libstdc++6 \
        tini \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        "mcp>=1.0,<2.0" \
        "pytest>=8.0,<10.0"

WORKDIR /app
ENV PYTHONPATH=/app/src

# Pull built engine + assets from the builder stage. The on-disk layout
# matches what engine.core's _first_existing() default-path search
# expects (REPO_ROOT/vendor/...), so no env-var configuration is needed
# at runtime.
COPY --from=builder /build/ygopro-core/bin/release/libocgcore.so \
                    /app/vendor/ygopro-core/bin/release/libocgcore.so
COPY --from=builder /build/babel/cards.cdb \
                    /app/vendor/distribution/expansions/cards.cdb
COPY --from=builder /build/scripts/ /app/vendor/distribution/script/

# Benchmark code + dataset built in the dataset-builder stage (NOT
# providers/ — agent lives outside).
COPY src/engine/    /app/src/engine/
COPY src/lib/       /app/src/lib/
COPY --from=dataset-builder /work/data/ /app/data/
COPY agent-mcp-eval/server.py /app/agent-mcp-eval/server.py
COPY tests/          /app/tests/

# Non-root user. /work/results is the bind-mount target for per-puzzle
# JSONL output.
RUN useradd --uid 1000 --create-home --shell /bin/bash ygo \
 && mkdir -p /work/results \
 && chown -R ygo:ygo /work /app

USER ygo

# Build-time smoke: confirm engine assets load. Failure here means the
# image is broken — surface it now, not at first `docker run`.
RUN python -c "from engine.core import CardDB, OCGEngine, DB_DIR, DYLIB_PATH, SCRIPT_DIR, CARD_SCRIPT_DIR; \
               from pathlib import Path; \
               db = CardDB(Path(DB_DIR)); \
               eng = OCGEngine(Path(DYLIB_PATH), db, Path(SCRIPT_DIR), Path(CARD_SCRIPT_DIR)); \
               eng.destroy(); \
               print('[selfcontained] engine assets load OK')"

ENTRYPOINT ["/usr/bin/tini", "--", "python", "/app/agent-mcp-eval/server.py"]
CMD ["--help"]
