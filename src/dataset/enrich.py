"""Reconstitute Konami-derived bulk text fields in a lean dataset JSONL.

The released ``data/yugioh_bench.jsonl`` is built with
``build_benchmark.py --lean``, which omits two Konami-derived bulk
text fields:

  - ``card_details``: per-card name + description + stats, joined
    from BabelCDB (Project Ignis card database).
  - ``prompt``: the rendered LLM prompt that embeds card_details
    inline.

This script reconstitutes both fields from the user's local
BabelCDB clone (cloned by ``setup.sh`` into
``vendor/distribution/expansions/``) and writes an enriched JSONL
that the runner consumes at evaluation time.

This split keeps the released artefact free of redistributed
Konami text while preserving full reproducibility: the lean
JSONL plus the user's own BabelCDB clone reconstitutes the
exact dataset bit-for-bit.

Usage::

    python src/dataset/enrich.py                       # default paths
    python src/dataset/enrich.py --input data/yugioh_bench.jsonl \
                             --output data/yugioh_bench.enriched.jsonl
    python src/dataset/enrich.py --cdb-dir <path>      # override BabelCDB

The runner looks for ``data/yugioh_bench.enriched.jsonl`` first
and falls back to the lean JSONL if the enriched copy is absent.
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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_benchmark import (  # noqa: E402
    CardDatabase, _default_cdb_dir, format_card_details, build_prompt,
)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path,
                    default=REPO_ROOT / "data" / "yugioh_bench.jsonl",
                    help="Lean JSONL produced by build_benchmark.py --lean")
    ap.add_argument("--output", type=Path,
                    default=REPO_ROOT / "data" / "yugioh_bench.enriched.jsonl",
                    help="Enriched JSONL with card_details + prompt restored")
    ap.add_argument("--cdb-dir", type=Path, default=_default_cdb_dir(),
                    help="Directory containing EDOPro card .cdb SQLite files "
                         "(default: vendor/distribution/expansions, populated "
                         "by setup.sh's BabelCDB clone)")
    ap.add_argument("--no-prompt", action="store_true",
                    help="Reconstruct card_details only; skip the rendered "
                         "prompt (the runner can rebuild it on demand from "
                         "card_details + game_state).")
    args = ap.parse_args()

    if not args.input.exists():
        print(f"error: input {args.input} does not exist", file=sys.stderr)
        return 1

    cdb_paths = [p for p in [
        args.cdb_dir / "cards.cdb", args.cdb_dir / "cards-rush.cdb",
        args.cdb_dir / "cards-unofficial.cdb",
        args.cdb_dir / "cards-unofficial-new.cdb",
        args.cdb_dir / "goat-entries.cdb", args.cdb_dir / "cards-skills.cdb",
        args.cdb_dir / "cards-skills-unofficial.cdb",
    ] if p.exists()]
    if not cdb_paths:
        print(f"error: no card .cdb files found in {args.cdb_dir}",
              file=sys.stderr)
        print("hint: run setup.sh to populate vendor/distribution/expansions",
              file=sys.stderr)
        return 1
    db = CardDatabase(cdb_paths)
    print(f"Loaded {len(db._texts)} cards from {len(cdb_paths)} databases",
          file=sys.stderr)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    n_enriched = 0
    with open(args.input) as fin, open(args.output, "w") as fout:
        for line in fin:
            inst = json.loads(line)
            card_ids = inst.get("card_ids", [])
            details = format_card_details(card_ids, db)
            inst["card_details"] = details
            if not args.no_prompt:
                inst["prompt"] = build_prompt(
                    inst["game_state"], details, inst["metadata"],
                    hints=[],  # hints are not stored in the lean JSONL
                )
            fout.write(json.dumps(inst, ensure_ascii=False) + "\n")
            n_enriched += 1

    print(f"Wrote {n_enriched} enriched instances to {args.output}",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
