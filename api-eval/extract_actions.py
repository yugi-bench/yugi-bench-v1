"""Extract a replayable action list from a fully-interactive episode JSONL.

A fully-interactive ``run_inference`` log records every model turn and
every engine event for one puzzle.  This tool reads such a log and emits
the ``[{"tool": ..., "args": ...}, ...]`` sequence that
``engine.replay.replay_solution`` consumes — i.e. the model's
response-tool calls only, with inspection-tool calls stripped, and any
actions made before the LAST ``restart`` discarded (since restart
resets the engine to puzzle initial conditions).

Typical use: take a winning fully-interactive run and write the action
list as ``solutions/<instance_id>.json`` so the engine wiring can be
re-verified offline (``python src/engine/replay.py --solutions
solutions --only <instance_id>``) without spending API credits.

Usage:
    python api-eval/extract_actions.py <path/to/run.jsonl>
    python api-eval/extract_actions.py <jsonl> --output <path>
    python api-eval/extract_actions.py <jsonl> --allow-non-win
"""
from __future__ import annotations

import sys
from pathlib import Path as _Path
_REPO_ROOT = _Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))


import argparse
import json
import sys
from pathlib import Path


# Inspection tools never affect engine state, so they're stripped from
# the replay action list.  Keep this in sync with engine.tools.
INSPECTION_TOOLS = frozenset({
    "get_state",
    "pending_decision",
    "inspect_card",
    "get_glossary",
})


def extract(jsonl_path: Path) -> tuple[list[dict], dict | None, str | None]:
    """Return (actions, outcome, instance_id).

    ``actions`` is the post-last-restart, inspection-stripped tool-call
    sequence.  ``outcome`` is the JSONL's terminal record (or None if
    the run did not finish).  ``instance_id`` is parsed from the JSONL
    filename (``yugioh_puzzle_<id>.jsonl`` → ``yugioh_puzzle_<id>``).
    """
    # Two-pass: first walk records to map each tool_call to its
    # corresponding tool_result (matched by id with positional fallback),
    # so we can skip tool_calls the harness rejected (is_error=true) on
    # the live run.  Replaying a rejected call would re-error and
    # diverge from the conversation that actually won.
    records = []
    for line in jsonl_path.open():
        line = line.strip()
        if not line:
            continue
        records.append(json.loads(line))

    # Build error map: tool_call_id -> bool(is_error)
    err_by_id: dict[str, bool] = {}
    pending_unknown_ids: list[int] = []  # FIFO of model_turn record indices missing ids
    for i, rec in enumerate(records):
        if rec.get("type") == "model_turn":
            for tc in rec.get("tool_calls", []):
                if tc.get("id"):
                    err_by_id.setdefault(tc["id"], False)
        elif rec.get("type") == "tool_result":
            tcid = rec.get("tool_use_id")
            if tcid:
                err_by_id[tcid] = bool(rec.get("is_error"))
    # Fallback: when ids weren't logged, match by ORDER of (call_seq, result_seq)
    call_seq: list[tuple[int, int]] = []  # (record_idx, tc_idx)
    for i, rec in enumerate(records):
        if rec.get("type") == "model_turn":
            for j, tc in enumerate(rec.get("tool_calls", [])):
                if not tc.get("id"):
                    call_seq.append((i, j))
    result_errs: list[bool] = []
    for rec in records:
        if rec.get("type") == "tool_result" and not rec.get("tool_use_id"):
            result_errs.append(bool(rec.get("is_error")))
    positional_errs: dict[tuple[int, int], bool] = {
        c: e for c, e in zip(call_seq, result_errs)
    }

    actions: list[dict] = []
    outcome: dict | None = None
    for i, rec in enumerate(records):
        t = rec.get("type")
        if t == "model_turn":
            for j, tc in enumerate(rec.get("tool_calls", [])):
                name = tc.get("name")
                if name == "restart":
                    actions.clear()
                    continue
                if name in INSPECTION_TOOLS:
                    continue
                # Drop calls the harness rejected on the live run.
                tcid = tc.get("id")
                if tcid and err_by_id.get(tcid, False):
                    continue
                if not tcid and positional_errs.get((i, j), False):
                    continue
                actions.append({
                    "tool": name,
                    "args": tc.get("arguments", {}),
                })
        elif t == "outcome":
            outcome = rec
    iid = jsonl_path.stem
    return actions, outcome, iid


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("jsonl", type=Path,
                    help="Fully-interactive run JSONL (one puzzle's full log)")
    ap.add_argument("--output", type=Path, default=None,
                    help="Output path (default: "
                         "solutions/<instance_id>.json relative to the "
                         "yugi-bench repo root)")
    ap.add_argument("--allow-non-win", action="store_true",
                    help="Write the file even if the run did not win.  "
                         "Default: refuse, to prevent shipping broken "
                         "solutions as canonical.")
    args = ap.parse_args(argv)

    if not args.jsonl.exists():
        print(f"error: {args.jsonl} does not exist", file=sys.stderr)
        return 2

    actions, outcome, iid = extract(args.jsonl)

    if outcome is None:
        print(f"warning: {args.jsonl.name} has no terminal outcome record "
              f"(run did not complete)", file=sys.stderr)
        won = False
    else:
        won = bool(outcome.get("game_over")) and outcome.get("winner") == 0

    print(f"  instance_id : {iid}", file=sys.stderr)
    print(f"  actions     : {len(actions)}", file=sys.stderr)
    print(f"  outcome     : termination={outcome and outcome.get('termination')!r} "
          f"winner={outcome and outcome.get('winner')} "
          f"lp={outcome and outcome.get('lp')}", file=sys.stderr)

    if not won and not args.allow_non_win:
        print("refusing to write: run did not win.  Pass --allow-non-win "
              "to override.", file=sys.stderr)
        return 1

    if args.output is None:
        repo_root = Path(__file__).resolve().parent.parent
        out_path = repo_root / "solutions" / f"{iid}.json"
    else:
        out_path = args.output
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(actions, indent=2))
    print(f"  wrote {out_path} ({out_path.stat().st_size} bytes)",
          file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
