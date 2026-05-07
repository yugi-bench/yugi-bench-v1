"""Multi-attempt replay evaluator — n-attempts sequential resubmission.

Same solution format as the single-attempt evaluator (a JSON list of
``{"tool", "args"}`` calls), but the model gets up to **N attempts** per
puzzle.  After a failed or incomplete replay, the error + current pending
decision are handed back to the model's resubmit callback, and it returns
a fresh complete action list.

The benchmark scores a puzzle as WON on attempt N iff attempt N's replay
ends in MSG_WIN for the scoring player.  Earlier failed attempts do not
carry state across — each attempt starts from the puzzle's initial Lua
setup.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Allow direct script invocation (`python src/engine/multi_attempt.py ...`)
# by putting src/ on sys.path so internal `from engine.X import …` resolves
# without requiring `pip install -e .`.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = str(_REPO_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from engine.replay import (
    DEFAULT_DATASET,
    DEFAULT_RESULTS,
    EvalResult,
    SingleAttemptEvaluator,
    load_dataset,
)

ResubmitCallback = Callable[[int, EvalResult, dict | None], "list[dict] | None"]
"""Callback the caller supplies to produce a fresh action list after a failure.

Arguments:
    attempt_index: 0-based index of the attempt that just failed
                   (1 means "you've already used 2 attempts").
    previous_result: the ``EvalResult`` of the just-finished failed attempt.
    instance: the puzzle dict (``instance_id``, ``lua_setup``, ``prompt``, ...).

Return:
    * A fresh ``list[dict]`` solution to try on the next attempt, OR
    * ``None`` to give up and accept the current failure.
"""


@dataclass
class MultiAttemptResult:
    """Outcome of up to N attempts for one puzzle in n-attempts bulk mode."""

    status: str  # "win" | "loss" | "incomplete" | "error" | "gave_up"
    attempts_used: int
    per_attempt: list[dict] = field(default_factory=list)
    # Convenience: the final EvalResult from the last attempt
    final: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


class MultiAttemptEvaluator:
    """Wraps ``SingleAttemptEvaluator`` with a retry loop."""

    def __init__(
        self,
        *,
        max_attempts: int = 3,
        inner: SingleAttemptEvaluator | None = None,
        **engine_kwargs: Any,
    ):
        self.max_attempts = max_attempts
        self.inner = inner or SingleAttemptEvaluator(**engine_kwargs)

    def evaluate_one(
        self,
        instance: dict,
        first_solution: list[dict],
        resubmit: ResubmitCallback,
        *,
        perspective: int = 0,
    ) -> MultiAttemptResult:
        lua_setup = instance["lua_setup"]
        per_attempt: list[dict] = []
        current_solution: list[dict] | None = first_solution

        for attempt_idx in range(self.max_attempts):
            if current_solution is None:
                return MultiAttemptResult(
                    status="gave_up",
                    attempts_used=attempt_idx,
                    per_attempt=per_attempt,
                    final=(per_attempt[-1] if per_attempt else None),
                )

            result = self.inner.evaluate_one(
                lua_setup,
                current_solution,
                perspective=perspective,
            )
            per_attempt.append(
                {
                    "attempt": attempt_idx + 1,
                    "solution_len": len(current_solution),
                    "result": result.to_dict(),
                }
            )

            if result.status == "win":
                return MultiAttemptResult(
                    status="win",
                    attempts_used=attempt_idx + 1,
                    per_attempt=per_attempt,
                    final=result.to_dict(),
                )

            # Not a win; ask the resubmit callback for another attempt
            if attempt_idx == self.max_attempts - 1:
                # No more attempts — return the last status as final
                return MultiAttemptResult(
                    status=result.status,
                    attempts_used=attempt_idx + 1,
                    per_attempt=per_attempt,
                    final=result.to_dict(),
                )

            current_solution = resubmit(attempt_idx, result, instance)

        # If the loop fell through (shouldn't happen) mark as error
        return MultiAttemptResult(
            status="error",
            attempts_used=self.max_attempts,
            per_attempt=per_attempt,
            final=(per_attempt[-1] if per_attempt else None),
        )


# ---------------------------------------------------------------------------
# Convenience helper for non-LLM testing / fixed-solution replay
# ---------------------------------------------------------------------------
def run_three_attempt(
    evaluator: MultiAttemptEvaluator,
    instance: dict,
    candidate_solutions: Sequence[list[dict]],
    *,
    perspective: int = 0,
) -> MultiAttemptResult:
    """Fire-and-forget: try up to ``max_attempts`` canned solutions in order.

    Useful for smoke-testing n-attempts mode with a list of hand-authored attempts.
    Production use (LLM in the loop) should call ``MultiAttemptEvaluator.evaluate_one``
    directly and pass a real ``resubmit`` callback.
    """
    candidate_iter = iter(candidate_solutions)
    try:
        first = next(candidate_iter)
    except StopIteration:
        return MultiAttemptResult(status="gave_up", attempts_used=0)

    def cb(_idx: int, _prev: EvalResult, _inst: dict) -> list[dict] | None:
        try:
            return next(candidate_iter)
        except StopIteration:
            return None

    return evaluator.evaluate_one(instance, first, cb, perspective=perspective)


# ---------------------------------------------------------------------------
# CLI — batch-evaluate canned three-attempt solution bundles
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    """Evaluate pre-authored three-attempt bundles.

    Each input file is ``solutions/<run>/<instance_id>.json`` containing a
    JSON list of 1-3 action-lists (the attempts, in order).  The evaluator
    runs them until one wins or all fail.
    """
    ap = argparse.ArgumentParser(description=main.__doc__.splitlines()[0])
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument(
        "--solutions",
        type=Path,
        required=True,
        help="Directory of <instance_id>.json bundles (a bundle is a "
        "JSON list whose elements are each a full action-list).",
    )
    ap.add_argument(
        "--results",
        type=Path,
        default=None,
        help="Output path (default: results/<solutions-dir-name>.json)",
    )
    ap.add_argument(
        "--only",
        action="append",
        default=None,
        help="Restrict to the given instance_id (repeatable)",
    )
    ap.add_argument("--perspective", type=int, default=0)
    ap.add_argument("--max-attempts", type=int, default=3)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    instances = load_dataset(args.dataset)
    if args.only:
        instances = {k: v for k, v in instances.items() if k in set(args.only)}

    evaluator = MultiAttemptEvaluator(max_attempts=args.max_attempts, verbose=args.verbose)
    per_instance: dict[str, dict] = {}
    counts: dict[str, int] = {
        "win": 0,
        "loss": 0,
        "incomplete": 0,
        "error": 0,
        "gave_up": 0,
        "missing": 0,
    }

    for idx, (iid, inst) in enumerate(sorted(instances.items())):
        bundle_path = args.solutions / f"{iid}.json"
        if not bundle_path.exists():
            counts["missing"] += 1
            per_instance[iid] = {"status": "missing"}
            print(f"[{idx + 1}/{len(instances)}] {iid}: MISSING", file=sys.stderr)
            continue
        try:
            bundle = json.loads(bundle_path.read_text())
        except Exception as e:  # noqa: BLE001
            counts["error"] += 1
            per_instance[iid] = {"status": "error", "error": f"bad bundle file: {e}"}
            print(f"[{idx + 1}/{len(instances)}] {iid}: BAD FILE ({e})", file=sys.stderr)
            continue
        if (
            not isinstance(bundle, list)
            or not bundle
            or not all(isinstance(x, list) for x in bundle)
        ):
            counts["error"] += 1
            per_instance[iid] = {
                "status": "error",
                "error": "bundle must be a non-empty list of action-lists",
            }
            print(f"[{idx + 1}/{len(instances)}] {iid}: BAD SHAPE", file=sys.stderr)
            continue

        t0 = time.time()
        result = run_three_attempt(
            evaluator,
            inst,
            bundle[: args.max_attempts],
            perspective=args.perspective,
        )
        elapsed = round(time.time() - t0, 2)
        counts[result.status] = counts.get(result.status, 0) + 1
        row = result.to_dict()
        row["elapsed"] = elapsed
        per_instance[iid] = row
        print(
            f"[{idx + 1}/{len(instances)}] {iid}: {result.status.upper()} "
            f"(attempts={result.attempts_used}, {elapsed}s)",
            file=sys.stderr,
        )

    attempted = sum(counts[k] for k in ("win", "loss", "incomplete", "error", "gave_up"))
    summary = {
        "mode": "n-attempts",
        "dataset": str(args.dataset),
        "solutions": str(args.solutions),
        "max_attempts": args.max_attempts,
        "total_instances": len(instances),
        "attempted": attempted,
        "counts": counts,
        "win_rate_all": counts["win"] / len(instances) if instances else 0,
        "win_rate_attempted": counts["win"] / attempted if attempted else 0,
        "per_instance": per_instance,
    }

    results_path = args.results or (DEFAULT_RESULTS / f"{args.solutions.name}.json")
    results_path.parent.mkdir(parents=True, exist_ok=True)
    results_path.write_text(json.dumps(summary, indent=2))

    print(file=sys.stderr)
    print("=" * 60, file=sys.stderr)
    print(f"Total instances : {len(instances)}", file=sys.stderr)
    print(f"  attempted     : {attempted}", file=sys.stderr)
    for key in ("win", "loss", "incomplete", "error", "gave_up", "missing"):
        print(f"  {key:<14s}: {counts.get(key, 0)}", file=sys.stderr)
    print(f"Win rate (all) : {summary['win_rate_all']:.1%}", file=sys.stderr)
    print(f"Win rate (atmp): {summary['win_rate_attempted']:.1%}", file=sys.stderr)
    print(f"Wrote {results_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
