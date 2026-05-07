"""Print the prompt for a given puzzle.

Useful for prompt inspection / iteration without burning API calls.

Examples:

    python src/dataset/dump_prompt.py yugioh_puzzle_c55b6641
    python src/dataset/dump_prompt.py yugioh_puzzle_c55b6641 --section grammar
    python src/dataset/dump_prompt.py yugioh_puzzle_c55b6641 --interactive
    python src/dataset/dump_prompt.py yugioh_puzzle_c55b6641 --interactive --forage
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Make the repo importable when invoked from the repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))

from lib.glossary import render_full_glossary
from lib.grammar import render_action_grammar
from lib.prompt_builder import (
    build_bulk_prompt,
    carve_rules_reference,
)
from lib.puzzle_preamble import PUZZLE_PREAMBLE

DEFAULT_DATASET = _REPO_ROOT / "data" / "yugioh_bench.jsonl"


def _find_instance(dataset: Path, iid: str) -> dict | None:
    with open(dataset) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            inst = json.loads(line)
            if inst["instance_id"] == iid:
                return inst
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("instance_id")
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument(
        "--section",
        choices=["full", "preamble", "rules", "grammar", "glossary", "raw"],
        default="full",
        help=(
            "full = the full assembled prompt (default); "
            "preamble = the puzzle-framing intro; "
            "rules = the carved-out rules reference; "
            "grammar = the response-verb action grammar; "
            "glossary = the per-card oracle text; "
            "raw = the unmodified dataset prompt"
        ),
    )
    ap.add_argument(
        "--interactive",
        action="store_true",
        help="Render the fully-interactive system prompt "
        "(needs a live Harness to query state, so this "
        "renders the static portion only — use the "
        "runner for the engine-coupled sections).",
    )
    ap.add_argument(
        "--attempts",
        type=int,
        default=1,
        help="N-attempts bulk mode --attempts N (default 1 = single-shot).",
    )
    ap.add_argument(
        "--forage",
        action="store_true",
        help="Fully-interactive lean-prompt mode (no glossary, no state).",
    )
    ap.add_argument(
        "--show-solution",
        action="store_true",
        help="Inject the gold-solution walkthrough (oracle/ceiling-test mode).",
    )
    ap.add_argument("--show-length", action="store_true")
    args = ap.parse_args(argv)

    inst = _find_instance(args.dataset, args.instance_id)
    if inst is None:
        print(f"No instance {args.instance_id!r} in {args.dataset}", file=sys.stderr)
        return 1

    if args.section == "raw":
        text = inst.get("prompt", "")
    elif args.section == "preamble":
        text = PUZZLE_PREAMBLE
    elif args.section == "rules":
        text = carve_rules_reference(inst.get("prompt", ""))
    elif args.section == "grammar":
        mode = "interactive" if args.interactive else "bulk"
        text = render_action_grammar(mode=mode)
    elif args.section == "glossary":
        # Glossary needs a CardDB; load lazily so --section grammar/rules
        # don't require the engine to be installed.
        from engine.core import DB_DIR, CardDB

        text = render_full_glossary(inst, CardDB(Path(DB_DIR)))
    else:  # full
        if args.interactive:
            print(
                "[dump_prompt] --interactive --section full needs a live "
                "Harness for state-render. Use --section grammar / rules / "
                "preamble / glossary, or run runner.py --interactive --dry-run.",
                file=sys.stderr,
            )
            return 2
        text = build_bulk_prompt(
            instance=inst,
            attempts=args.attempts,
            show_solution=args.show_solution,
        )

    print(text)
    if args.show_length:
        print(file=sys.stderr)
        print(f"[{len(text)} chars / {len(text.splitlines())} lines]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
