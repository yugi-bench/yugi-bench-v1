"""Build yugi-bench Verified — the SWE-bench-style curated subset.

Verified is the set of puzzles that have a gold solution in the
upstream Lua and are not in the engine-bug exclusion list.
Membership is intrinsic to the puzzle (does it have a gold
solution?), not contingent on whether we have re-derived a
machine-format replay yet.

Aspirationally, every Verified puzzle should also have a
replay-checked machine-format solution under
``solutions/<instance_id>.json``.  Today that is incomplete —
TASK #6 tracks closing the gap.

Usage:
    python src/dataset/build_verified_subset.py
    python src/dataset/build_verified_subset.py --reverify-replays
    python src/dataset/build_verified_subset.py --output PATH

What this script writes:

  - ``data/yugioh_bench_verified.jsonl`` (default --output): all
    puzzles from the full benchmark that satisfy the Verified
    criteria.

What ``--reverify-replays`` adds: re-runs ``engine.replay`` on
every puzzle that has a ``solutions/<id>.json`` file and prints
the win/error/incomplete tally.  Does not affect the Verified
membership — it just reports replay coverage.
"""
from __future__ import annotations

import sys
from pathlib import Path as _Path
_REPO_ROOT = _Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))


import argparse
import json
import sys
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", type=Path,
                    default=REPO_ROOT / "data" / "yugioh_bench.jsonl",
                    help="Full benchmark JSONL")
    ap.add_argument("--solutions", type=Path,
                    default=REPO_ROOT / "solutions",
                    help="Directory of <instance_id>.json action lists")
    ap.add_argument("--output", type=Path,
                    default=REPO_ROOT / "data" / "yugioh_bench_verified.jsonl",
                    help="Verified subset JSONL output")
    ap.add_argument("--reverify-replays", action="store_true",
                    help="Re-run engine.replay on every puzzle that "
                         "has a solutions/<id>.json file and report "
                         "the win/error tally.  Doesn't affect "
                         "Verified membership — just visibility into "
                         "replay-coverage progress.")
    args = ap.parse_args(argv)

    full = []
    with args.input.open() as f:
        for line in f:
            line = line.strip()
            if line:
                full.append(json.loads(line))
    print(f"full benchmark: {len(full)} instances", file=sys.stderr)

    verified = [r for r in full
                if r["metadata"].get("has_gold_solution") is True]
    # Re-number seq_id within the subset so consumers can index 0..N-1.
    for i, r in enumerate(verified):
        r["seq_id"] = i

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w") as f:
        for r in verified:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"yugi-bench Verified: {len(verified)} / {len(full)} puzzles "
          f"(filter: metadata.has_gold_solution == true)", file=sys.stderr)
    print(f"Wrote {args.output}", file=sys.stderr)

    # Replay-coverage report — informational only.
    have_replay = {p.stem for p in args.solutions.glob("yugioh_puzzle_*.json")}
    in_verified = {r["instance_id"] for r in verified}
    coverage = have_replay & in_verified
    missing = in_verified - have_replay
    print(f"\nReplay coverage of the Verified subset:", file=sys.stderr)
    print(f"  with machine-format solution in solutions/ : "
          f"{len(coverage)} / {len(verified)} "
          f"({100*len(coverage)/max(len(verified),1):.0f}%)",
          file=sys.stderr)
    print(f"  missing                                    : {len(missing)} "
          f"(see TASK #6 — replay-verify the rest)", file=sys.stderr)

    if args.reverify_replays and coverage:
        print(f"\n--reverify-replays: running engine.replay on the "
              f"{len(coverage)} puzzles with replays...", file=sys.stderr)
        only_args = []
        for iid in sorted(coverage):
            only_args += ["--only", iid]
        res = subprocess.run(
            [sys.executable, "-m", "engine.replay",
             "--solutions", str(args.solutions),
             "--results", "/tmp/verified-reverify-check.json",
             *only_args],
            cwd=str(REPO_ROOT),
        )
        if res.returncode != 0:
            print("  engine.replay exited non-zero", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
