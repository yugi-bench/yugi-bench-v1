# Replay-verified machine solutions

This directory ships **89 per-puzzle action lists** extracted from
DeepSeek V4 Pro `effort=max` fully interactive sweeps and re-run
through `engine.replay` to confirm each replays deterministically to
a `MSG_WIN` for the scoring player on a fresh engine. Each
`<instance_id>.json` is a JSON list of `{"tool", "args"}` calls in
the engine's response-verb grammar (see *Solution format* in the
top-level `README.md`).

## Verifying

```bash
python src/engine/replay.py --solutions solutions/
```

Replays every action list deterministically through a fresh engine.
Expected outcome:

- **89 wins, 0 losses, 0 incomplete, 0 errors** out of the 89
  attempted (100% replay-verify). 128 of the 217 puzzles in the
  dataset have no shipped machine solution and are reported as
  `missing`.

## Note on cross-machine engine reproducibility

`setup.sh` pins the upstream `libocgcore`, BabelCDB, and CardScripts
commits, but the locally-built `libocgcore.so` is not necessarily
byte-identical (or behaviourally identical) across host environments.
Empirically, three independent rebuilds on the same machine produce
binaries that differ at ~13% of bytes (mostly debug-info and
build-path metadata) but yield identical runtime behaviour on every
puzzle in this set. Across machines, however, runtime behaviour can
diverge on a small number of edge-case puzzles — typically those
involving forced chain windows at phase boundaries — because the
order in which simultaneous trigger evaluations are dispatched can
depend on the host's compiler / linker / memory-layout side-effects
that libocgcore's source does not pin.

The shipped solutions in this directory have all been replay-verified
against the engine that this repo's `setup.sh` produces under the
documented system-tool versions (`g++` >= 12, `premake5` 5.0
beta7+, GNU make). On a host with substantially older or newer
toolchains, an individual solution may fail to replay if libocgcore's
behaviour at a specific decision boundary diverges; the runner's
**fully interactive mode** is robust to this (the model adapts to
whatever the live engine emits) and is the canonical evaluation
surface. Bulk-replay verification is a secondary smoke and should be
read as a "no regressions on this build" signal, not as a
build-portable claim.
