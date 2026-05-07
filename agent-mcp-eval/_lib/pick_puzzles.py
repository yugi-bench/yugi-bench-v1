#!/usr/bin/env python3
"""Shared puzzle picker for codex/prep-batch.sh and claude/prep-batch.sh.

Reads the strategy, count, dataset paths from argv; prints one
selected puzzle id per line on stdout.  Errors go to stderr.

Strategies:
  - easy            (default) sort full 217-puzzle dataset with verified
                    puzzles first (easiest -> hardest), then non-verified
                    (easiest -> hardest); tie-break by puzzle hash.  Take
                    first N.
  - random          random sample of full 217
  - all             every puzzle in the full 217 in the same easy-order
                    (ignores N)
  - verified-easy   sort verified subset by complexity ascending,
                    take first N (opt-in: Konami-gold puzzles only)
  - verified        random sample of verified subset (opt-in)
  - list:ID,ID,...  explicit comma-separated list (count = len)

Invocation:
    python3 _lib/pick_puzzles.py <strategy> <count> <dataset> <verified> <repo_root>
"""
import json
import random
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]
    if len(argv) != 5:
        print(__doc__, file=sys.stderr)
        return 2

    strategy, count_str, dataset, verified, repo_root = argv
    count = int(count_str)
    ds = [json.loads(line) for line in open(dataset) if line.strip()]
    ds_by_id = {d["instance_id"]: d for d in ds}

    # SELECT_SUM ritual exclusions, mirrors build_benchmark.EXCLUDED_PUZZLES.
    sys.path.insert(0, str(Path(repo_root) / "src"))
    try:
        from dataset.build_benchmark import EXCLUDED_PUZZLES
        excluded = set(EXCLUDED_PUZZLES)
    except Exception:
        excluded = set()

    # complexity is a "N/10" string under .metadata; parse the
    # leading int. Default to 99 when absent so missing-metadata
    # puzzles sort to the end. Tiebreak by id for determinism.
    def _cx(d):
        raw = (d.get("metadata") or {}).get("complexity", "")
        try:
            return int(str(raw).split("/")[0])
        except Exception:
            return 99

    if strategy.startswith("list:"):
        ids = [s.strip() for s in strategy[5:].split(",") if s.strip()]
        for iid in ids:
            if iid not in ds_by_id:
                print(f"ERROR: '{iid}' not in dataset", file=sys.stderr)
                return 1
    elif strategy in ("easy", "random", "all"):
        pool = [d for d in ds if d["instance_id"] not in excluded]
        if strategy in ("easy", "all"):
            # Verified puzzles first (have a Konami-shipped gold
            # solution we can replay-verify), then the rest.  Within
            # each tier sort by complexity ascending, tie-break by
            # puzzle id (which IS the content hash, so stable across
            # rebuilds).  Same ordering for `easy` (capped at --count)
            # and `all` (full pool).
            verified_ids: set[str] = set()
            if Path(verified).exists():
                verified_ids = {
                    json.loads(line)["instance_id"]
                    for line in open(verified) if line.strip()
                }
            pool.sort(key=lambda d: (
                0 if d["instance_id"] in verified_ids else 1,
                _cx(d),
                d["instance_id"],
            ))
        elif strategy == "random":
            random.seed(0)
            random.shuffle(pool)
        # `all` ignores --count and returns the whole pool in easy-order.
        cap = len(pool) if strategy == "all" else count
        ids = [d["instance_id"] for d in pool[:cap]]
    elif strategy in ("verified-easy", "verified"):
        if not Path(verified).exists():
            print(
                f"ERROR: verified subset missing at {verified}",
                file=sys.stderr,
            )
            return 1
        vs = [json.loads(line) for line in open(verified) if line.strip()]
        pool = [v for v in vs if v["instance_id"] not in excluded]
        if strategy == "verified-easy":
            pool.sort(key=lambda d: (_cx(d), d["instance_id"]))
        else:
            random.seed(0)
            random.shuffle(pool)
        ids = [d["instance_id"] for d in pool[:count]]
    else:
        print(
            f"ERROR: unknown strategy '{strategy}' "
            "(try easy / random / verified-easy / verified / list:ID,ID,...)",
            file=sys.stderr,
        )
        return 1

    for iid in ids:
        print(iid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
