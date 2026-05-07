"""Replay evaluator for action-list (bulk-mode) submissions.

A bulk-mode **solution** for a puzzle is a JSON list of response-verb calls::

    [
      {"tool": "select_chain",      "args": {"index": null}},
      {"tool": "select_idlecmd",    "args": {"command": "summon", "index": 0}},
      {"tool": "select_option",     "args": {"index": 1}},
      {"tool": "select_card",       "args": {"indices": [2]}},
      ...
    ]

where ``tool`` is one of the 20 response verbs (see ``engine.tools``) and
``args`` matches that verb's JSON-Schema (see the ``TOOLS`` registry).

Replay is deterministic: the solution is fed sequentially into a fresh
``engine.harness.Harness`` — the solver gets no intermediate observations
(that's interactive mode).  A solution PASSES iff the duel terminates
with MSG_WIN and the scoring player (default ``perspective=0``) wins.

Used by ``api-eval/runner.py`` in bulk mode (--attempts N) and exposed via
the CLI ``python src/engine/replay.py --solutions <dir>`` for offline batch
evaluation of action-list bundles.
"""

from __future__ import annotations

import json
import re


# Action-list extraction from raw LLM text response.  Used by n-attempts
# bulk mode in runner.py to parse a model output into the action list
# this module then replays.
_SOLUTION_TAG = re.compile(r"<solution>\s*(\[.*?\])\s*</solution>", re.DOTALL)
_FENCED = re.compile(r"```(?:json)?\s*(\[.*?\])\s*```", re.DOTALL)
# Lenient form used by _classify_parse_failure — matches any content
# between solution tags so we can detect "tag present, content empty"
# vs "tag present, content non-empty but malformed".
_SOLUTION_TAG_ANY = re.compile(r"<solution>(.*?)</solution>", re.DOTALL)


def extract_actions(text: str) -> list[dict]:
    """Pull the first JSON array of action objects out of model text.

    Accepts (in order):
      * ``<solution>[...]</solution>`` tags (the format the prompt requests)
      * Fenced ```json blocks
      * A bare JSON array somewhere in the text
    """
    for pattern in (_SOLUTION_TAG, _FENCED):
        m = pattern.search(text)
        if m:
            return json.loads(m.group(1))
    first = text.find("[")
    last = text.rfind("]")
    if first != -1 and last > first:
        try:
            return json.loads(text[first:last + 1])
        except json.JSONDecodeError:
            pass
    raise ValueError("no JSON action list found in response")


def classify_parse_failure(text: str) -> str:
    """After ``extract_actions(text)`` raised, decide whether the model
    attempted to submit a structured solution or didn't try at all.

    Returns one of:
      * ``'attempted_invalid_json'`` — text contains a ``<solution>``
        tag pair (or fenced ```json block) with non-empty trimmed
        content.  The model tried; the JSON just didn't parse.
        Recoverable — retry with parse-error feedback.
      * ``'no_json_attempted'`` — no recognizable structured-solution
        wrapper, or wrappers are empty.  Treat as genuine surrender.

    The classifier looks at intent (did the model use the instructed
    output format with non-empty content?) rather than content (did
    they say 'I give up'?), per the project preference for
    programmatic-attempt detection over keyword matching.
    """
    sol = _SOLUTION_TAG_ANY.search(text)
    if sol and sol.group(1).strip():
        return "attempted_invalid_json"
    fenced = _FENCED.search(text)
    if fenced and fenced.group(1).strip():
        return "attempted_invalid_json"
    return "no_json_attempted"


import argparse
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable

# Allow direct script invocation (`python src/engine/replay.py ...`) by
# putting src/ on sys.path so internal `from engine.X import …` resolves
# without requiring `pip install -e .`.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_SRC = str(_REPO_ROOT / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from engine.core import (
    CARD_SCRIPT_DIR,
    CardDB,
    DB_DIR,
    DYLIB_PATH,
    MSG_ANNOUNCE_ATTRIB,
    MSG_ANNOUNCE_CARD,
    MSG_ANNOUNCE_NUMBER,
    MSG_ANNOUNCE_RACE,
    MSG_ROCK_PAPER_SCISSORS,
    MSG_SELECT_BATTLECMD,
    MSG_SELECT_CARD,
    MSG_SELECT_CHAIN,
    MSG_SELECT_COUNTER,
    MSG_SELECT_DISFIELD,
    MSG_SELECT_EFFECTYN,
    MSG_SELECT_IDLECMD,
    MSG_SELECT_OPTION,
    MSG_SELECT_PLACE,
    MSG_SELECT_POSITION,
    MSG_SELECT_SUM,
    MSG_SELECT_TRIBUTE,
    MSG_SELECT_UNSELECT_CARD,
    MSG_SELECT_YESNO,
    MSG_SORT_CARD,
    MSG_SORT_CHAIN,
    OCGEngine,
    SCRIPT_DIR,
)
from engine.harness import (
    Harness,
    HarnessError,
    InvalidResponseError,
    PendingDecision,
)
from engine.tools import TOOL_TO_HARNESS_METHOD, coerce_args


REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = REPO_ROOT / "data" / "yugioh_bench.jsonl"
DEFAULT_SOLUTIONS = REPO_ROOT / "solutions"
DEFAULT_RESULTS = REPO_ROOT / "results"


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------
@dataclass
class EvalResult:
    """Outcome of replaying one solution."""

    status: str  # "win" | "loss" | "incomplete" | "error"
    steps_applied: int
    total_steps: int
    winner: int | None = None
    lp: list[int] = field(default_factory=lambda: [8000, 8000])
    turn_count: int = 0
    error: str | None = None
    # For diagnostics when the run didn't win:
    failure_step: int | None = None
    pending_after: dict | None = None

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Core replay
# ---------------------------------------------------------------------------
def replay_solution(
    engine: OCGEngine,
    lua_setup: str,
    solution: list[dict],
    *,
    perspective: int = 0,
    auto_opponent: bool = True,
    tolerant_chains: bool = True,
) -> EvalResult:
    """Replay one solution through a fresh Harness.

    ``engine`` is owned by the caller — this function does NOT call
    ``engine.destroy()``.  A new ``Harness`` is built per call, so the
    engine must be in a pre-duel state (freshly constructed, or the caller
    destroyed and recreated it).

    When ``auto_opponent=True`` (the default), opponent-side decisions
    (``pending.player != perspective``) are auto-answered with a passive
    policy — decline chains, decline optional triggers, first-legal for
    forced choices.  The model's ``solution`` list therefore only needs
    to contain decisions for the scoring player.  This matches the
    "solve your turn" framing of the puzzles: most puzzles have the
    opponent as a passive wall (set traps, continuous cards, no
    strategic decisions of their own).

    When ``tolerant_chains=True`` (the default), the evaluator smooths
    over two predictable mismatches in chain-window placement between
    the model's predicted stream and the engine's actual one:

      - If the model submits ``{"tool": "select_chain", "args": {"index":
        null}}`` but the engine isn't currently asking for a chain, the
        action is **silently skipped** (a null chain is a no-op — the
        engine omitted this window because no triggers fired).
      - If the engine is asking for a chain (optional, ``forced=false``)
        and the model's next action is NOT a ``select_chain``, the
        evaluator **auto-declines** (emits ``null`` itself) and then
        tries the model's next action against the resulting pending
        state.

    Together these handle the common case where phase-transitions and
    post-resolution chain windows appear or disappear based on whether
    any card could chain to them — behaviour that's unpredictable from
    rules alone.  Pass ``tolerant_chains=False`` for strict replay.
    """
    harness = Harness(engine)
    try:
        first = harness.start(lua_setup)
    except Exception as e:  # noqa: BLE001
        return EvalResult(
            status="error",
            steps_applied=0,
            total_steps=len(solution),
            error=f"harness.start: {type(e).__name__}: {e}",
        )

    if harness.state.game_over:
        won = harness.state.winner == perspective
        return EvalResult(
            status="win" if won else "loss",
            steps_applied=0,
            total_steps=len(solution),
            winner=harness.state.winner,
            lp=list(harness.state.lp),
            turn_count=harness.state.turn_count,
        )

    # Drain any opponent-side decisions at the top of the duel.
    if auto_opponent:
        _auto_advance_opponent(harness, perspective)
        if harness.state.game_over:
            won = harness.state.winner == perspective
            return EvalResult(
                status="win" if won else "loss",
                steps_applied=0,
                total_steps=len(solution),
                winner=harness.state.winner,
                lp=list(harness.state.lp),
                turn_count=harness.state.turn_count,
            )

    steps_applied = 0
    for i, action in enumerate(solution):
        if harness.state.game_over:
            break
        if harness.pending is None:
            return EvalResult(
                status="error",
                steps_applied=steps_applied,
                total_steps=len(solution),
                lp=list(harness.state.lp),
                turn_count=harness.state.turn_count,
                error=f"no pending decision at step {i}",
                failure_step=i,
            )
        if auto_opponent and harness.pending.player != perspective:
            # Shouldn't happen — we auto-advanced at the top of this
            # iteration in the last cycle.  Defend by advancing again.
            _auto_advance_opponent(harness, perspective)
            if harness.state.game_over or harness.pending is None:
                break

        tool_name = action.get("tool") or action.get("name")

        # --- tolerant-chain smoothing -------------------------------------
        if tolerant_chains:
            args_for_skip = action.get("args") or action.get("arguments") or {}
            # Case 1: model's null-chain but engine isn't in a chain window.
            # The null chain is a no-op; silently skip it and try the next
            # action at the same pending position.
            if (tool_name == "select_chain"
                    and harness.pending.msg_name != "MSG_SELECT_CHAIN"
                    and isinstance(args_for_skip, dict)
                    and args_for_skip.get("index") is None):
                continue
            # Case 2: engine IS asking for a chain but model's next action
            # is NOT select_chain.  If the chain is optional (not forced),
            # auto-decline on behalf of the model and fall through to
            # dispatch the model's action against the resulting pending.
            #
            # Multi-round chain resolutions can leave the engine at
            # ANOTHER optional MSG_SELECT_CHAIN immediately after we
            # decline — keep declining until the engine moves on or
            # gives us a forced chain.  This matches the runner's
            # _auto_decline_chains_until_real semantics, so replays of
            # interactive-mode traces stay aligned with the live engine path.
            while (harness.pending is not None
                   and harness.pending.msg_name == "MSG_SELECT_CHAIN"
                   and tool_name != "select_chain"
                   and not getattr(harness.pending.parsed, "forced", False)):
                try:
                    harness.respond_select_chain(index=None)
                except (InvalidResponseError, HarnessError):
                    break  # fall through to the regular dispatch below
                if auto_opponent:
                    _auto_advance_opponent(harness, perspective)
                if harness.state.game_over or harness.pending is None:
                    break

        args = action.get("args") or action.get("arguments") or {}
        if not isinstance(args, dict):
            return EvalResult(
                status="error",
                steps_applied=steps_applied,
                total_steps=len(solution),
                lp=list(harness.state.lp),
                turn_count=harness.state.turn_count,
                error=f"step {i}: args must be a dict, got {type(args).__name__}",
                failure_step=i,
                pending_after=_pending_summary(harness.pending),
            )
        if tool_name not in TOOL_TO_HARNESS_METHOD:
            return EvalResult(
                status="error",
                steps_applied=steps_applied,
                total_steps=len(solution),
                lp=list(harness.state.lp),
                turn_count=harness.state.turn_count,
                error=f"step {i}: unknown tool {tool_name!r}",
                failure_step=i,
                pending_after=_pending_summary(harness.pending),
            )

        method = getattr(harness, TOOL_TO_HARNESS_METHOD[tool_name])
        try:
            kwargs = coerce_args(tool_name, args)
            method(**kwargs)
        except (InvalidResponseError, HarnessError) as e:
            return EvalResult(
                status="error",
                steps_applied=steps_applied,
                total_steps=len(solution),
                lp=list(harness.state.lp),
                turn_count=harness.state.turn_count,
                error=f"step {i} ({tool_name}): {e}",
                failure_step=i,
                pending_after=_pending_summary(harness.pending),
            )
        except Exception as e:  # noqa: BLE001
            return EvalResult(
                status="error",
                steps_applied=steps_applied,
                total_steps=len(solution),
                lp=list(harness.state.lp),
                turn_count=harness.state.turn_count,
                error=f"step {i} ({tool_name}): {type(e).__name__}: {e}",
                failure_step=i,
                pending_after=_pending_summary(harness.pending),
            )
        steps_applied += 1
        # Drain any opponent decisions that arose from this action.
        if auto_opponent and not harness.state.game_over:
            _auto_advance_opponent(harness, perspective)

    # All actions applied (or broke early on game_over).  If the engine
    # is sitting at an optional MSG_SELECT_CHAIN (battle-damage step
    # chain windows after a winning attack, end-of-turn triggers, etc),
    # drain those just like the runner's auto-decline mechanism does
    # mid-action — otherwise we report "incomplete" for runs that the
    # live engine actually resolved to a win.  Mirrors the same
    # forced-chain check used inside the action loop.
    if (tolerant_chains and not harness.state.game_over
            and harness.pending is not None):
        while (harness.pending is not None
               and harness.pending.msg_name == "MSG_SELECT_CHAIN"
               and not getattr(harness.pending.parsed, "forced", False)):
            try:
                harness.respond_select_chain(index=None)
            except (InvalidResponseError, HarnessError):
                break
            if auto_opponent:
                _auto_advance_opponent(harness, perspective)
            if harness.state.game_over:
                break

    if harness.state.game_over:
        status = "win" if harness.state.winner == perspective else "loss"
        pending_after = None
    else:
        status = "incomplete"
        pending_after = _pending_summary(harness.pending)
    return EvalResult(
        status=status,
        steps_applied=steps_applied,
        total_steps=len(solution),
        winner=harness.state.winner,
        lp=list(harness.state.lp),
        turn_count=harness.state.turn_count,
        pending_after=pending_after,
    )


def _pending_summary(p: PendingDecision | None) -> dict | None:
    if p is None:
        return None
    return {"msg_type": p.msg_type, "msg_name": p.msg_name, "player": p.player}


# ---------------------------------------------------------------------------
# Auto-opponent policy — drain opponent-side decisions with a passive choice.
# ---------------------------------------------------------------------------
#
# Matches the "passive opponent" framing of the EDOPro puzzles:
#   - decline every optional chain / yes-no / effectYN prompt
#   - for forced chains (no decline possible), take index 0
#   - for targeting / place / position etc., pick the first legal option
#
# This helper is an intentional mirror of engine.tests.test_episode_smoke's
# first-legal stub, tuned to "passive" for the opponent. Lives here in
# engine.replay (not engine.episode) because the interactive Episode
# loop lets the model drive both sides naturally — only the n-attempts
# bulk replay path needs an auto-pilot for the opponent.

def _auto_advance_opponent(harness: Harness, perspective: int) -> None:
    """Drain opponent-side decisions until pending.player == perspective
    or the game ends.  Safe to call at any point: if the current pending
    is already for ``perspective``, it's a no-op.
    """
    safety = 0
    while (harness.pending is not None
           and harness.pending.player != perspective
           and not harness.state.game_over):
        safety += 1
        if safety > 400:
            raise HarnessError("auto-opponent exceeded 400 consecutive decisions")
        tool_name, kwargs = _pick_passive_opponent_response(harness.pending)
        method = getattr(harness, TOOL_TO_HARNESS_METHOD[tool_name])
        try:
            method(**kwargs)
        except (InvalidResponseError, HarnessError):
            # Fall back to a deterministic "accept index 0" for any decision
            # the passive policy mis-handled (rare but possible for weird
            # cards).
            tool_name, kwargs = _pick_fallback_opponent_response(harness.pending)
            method = getattr(harness, TOOL_TO_HARNESS_METHOD[tool_name])
            method(**kwargs)


def _pick_passive_opponent_response(p: PendingDecision) -> tuple[str, dict]:
    """Choose a passive response to one pending decision.  The goal is
    zero strategic activity — if the opponent has a choice to do nothing,
    they take it."""
    mt = p.msg_type
    d = p.parsed

    if mt == MSG_SELECT_IDLECMD:
        # Opponent should usually never get an idlecmd in these puzzles
        # (it'd mean we passed to their turn).  If so, end the turn.
        if getattr(d, "to_ep", False):
            return "select_idlecmd", {"command": "to_end_phase"}
        if getattr(d, "to_bp", False):
            return "select_idlecmd", {"command": "to_battle_phase"}
        return "select_idlecmd", {"command": "to_end_phase"}

    if mt == MSG_SELECT_BATTLECMD:
        if getattr(d, "to_ep", False):
            return "select_battlecmd", {"command": "to_end_phase"}
        if getattr(d, "to_m2", False):
            return "select_battlecmd", {"command": "to_main_phase_2"}
        return "select_battlecmd", {"command": "to_end_phase"}

    if mt == MSG_SELECT_EFFECTYN:
        return "select_effectyn", {"accept": False}
    if mt == MSG_SELECT_YESNO:
        return "select_yesno", {"accept": False}

    if mt == MSG_SELECT_OPTION:
        # Engine is asking opponent to resolve an effect — pick index 0.
        return "select_option", {"index": 0}

    if mt == MSG_SELECT_CHAIN:
        if getattr(d, "forced", False):
            return "select_chain", {"index": 0}
        return "select_chain", {"index": None}

    if mt in (MSG_SELECT_CARD,):
        min_ = getattr(d, "min_", 1)
        if getattr(d, "cancelable", False) and min_ == 0:
            return "select_card", {"indices": [], "cancel": True}
        return "select_card", {"indices": list(range(min_)), "cancel": False}

    if mt == MSG_SELECT_TRIBUTE:
        min_ = getattr(d, "min_", 1)
        return "select_tribute", {"indices": list(range(min_)), "cancel": False}

    if mt == MSG_SELECT_UNSELECT_CARD:
        if getattr(d, "finishable", False) or getattr(d, "cancelable", False):
            return "select_unselect_card", {"index": None}
        return "select_unselect_card", {"index": 0}

    if mt in (MSG_SELECT_PLACE, MSG_SELECT_DISFIELD):
        from engine.state import _available_places  # noqa: WPS437
        flag = getattr(d, "flag", 0)
        player = getattr(d, "player", p.player)
        places = _available_places(flag, player)
        min_ = getattr(d, "min_", 1)
        picks = [
            {"player": pl["player"], "location": pl["location"],
             "sequence": pl["sequence"]}
            for pl in places[:min_]
        ]
        return "select_place", {"places": picks}

    if mt == MSG_SELECT_POSITION:
        mask = getattr(d, "positions", 0)
        # Prefer DEF for opponent's forced summons if allowed (less damage).
        for bit in (0x4, 0x1, 0x8, 0x2):
            if mask & bit:
                return "select_position", {"position": bit}
        return "select_position", {"position": 0x1}

    if mt == MSG_SELECT_COUNTER:
        cards = getattr(d, "cards", []) or []
        remaining = getattr(d, "count", 0)
        counts = []
        for c in cards:
            take = min(remaining, c.get("counter", 0))
            counts.append(take)
            remaining -= take
        return "select_counter", {"counts": counts}

    if mt == MSG_SELECT_SUM:
        min_ = getattr(d, "min_", 1)
        return "select_sum", {"indices": list(range(min_))}

    if mt in (MSG_SORT_CARD, MSG_SORT_CHAIN):
        n = len(getattr(d, "cards", []) or [])
        return "sort_card", {"ordering": list(range(n))}

    if mt == MSG_ANNOUNCE_RACE:
        mask = getattr(d, "available", 0)
        count = getattr(d, "count", 1)
        out, picked = 0, 0
        for i in range(64):
            if picked >= count:
                break
            if mask & (1 << i):
                out |= 1 << i
                picked += 1
        return "announce_race", {"races_mask": out}

    if mt == MSG_ANNOUNCE_ATTRIB:
        mask = getattr(d, "available", 0)
        count = getattr(d, "count", 1)
        out, picked = 0, 0
        for i in range(16):
            if picked >= count:
                break
            if mask & (1 << i):
                out |= 1 << i
                picked += 1
        return "announce_attribute", {"attribs_mask": out}

    if mt == MSG_ANNOUNCE_CARD:
        return "announce_card", {"card_code": 89631139}  # Blue-Eyes as dummy

    if mt == MSG_ANNOUNCE_NUMBER:
        return "announce_number", {"index": 0}

    if mt == MSG_ROCK_PAPER_SCISSORS:
        return "rock_paper_scissors", {"hand": 1}

    raise HarnessError(f"no passive-opponent route for msg_type {mt} ({p.msg_name})")


def _pick_fallback_opponent_response(p: PendingDecision) -> tuple[str, dict]:
    """Fallback: accept/index-0 for whatever the passive policy got wrong."""
    mt = p.msg_type
    if mt == MSG_SELECT_EFFECTYN:
        return "select_effectyn", {"accept": True}
    if mt == MSG_SELECT_YESNO:
        return "select_yesno", {"accept": True}
    if mt == MSG_SELECT_CHAIN:
        # Pick first chainable if decline failed
        return "select_chain", {"index": 0}
    # Re-invoke passive as a last resort (caller may still fail)
    return _pick_passive_opponent_response(p)


# ---------------------------------------------------------------------------
# Batch evaluator — handles CardDB reuse + engine lifecycle
# ---------------------------------------------------------------------------
class SingleAttemptEvaluator:
    """Reusable evaluator — loads the card DB once, creates engines per puzzle."""

    def __init__(
        self,
        *,
        dylib_path: Path = Path(DYLIB_PATH),
        db_dir: Path = Path(DB_DIR),
        script_dir: Path = Path(SCRIPT_DIR),
        card_script_dir: Path = Path(CARD_SCRIPT_DIR),
        card_db: CardDB | None = None,
        verbose: bool = False,
    ):
        self.dylib_path = dylib_path
        self.script_dir = script_dir
        self.card_script_dir = card_script_dir
        self.verbose = verbose
        # Allow callers to pass a pre-built CardDB (e.g. when the
        # outer runner already loaded it once).  Falls back to loading
        # from db_dir if not provided.
        self.card_db = card_db if card_db is not None else CardDB(db_dir)

    def evaluate_one(
        self, lua_setup: str, solution: list[dict], *, perspective: int = 0
    ) -> EvalResult:
        engine = OCGEngine(
            dylib_path=self.dylib_path,
            card_db=self.card_db,
            script_dir=self.script_dir,
            card_script_dir=self.card_script_dir,
            verbose=self.verbose,
        )
        try:
            return replay_solution(engine, lua_setup, solution, perspective=perspective)
        finally:
            try:
                engine.destroy()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Dataset I/O
# ---------------------------------------------------------------------------
def load_dataset(path: Path) -> dict[str, dict]:
    out: dict[str, dict] = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            inst = json.loads(line)
            out[inst["instance_id"]] = inst
    return out


def load_solutions(sol_dir: Path, instance_ids: Iterable[str]) -> dict[str, list[dict] | str]:
    """Load per-instance JSON action lists. Returns error strings for bad files."""
    out: dict[str, list[dict] | str] = {}
    for iid in instance_ids:
        sol_path = sol_dir / f"{iid}.json"
        if not sol_path.exists():
            continue
        try:
            data = json.loads(sol_path.read_text())
        except Exception as e:  # noqa: BLE001
            out[iid] = f"bad solution file: {e}"
            continue
        if not isinstance(data, list):
            out[iid] = "solution is not a JSON array"
            continue
        out[iid] = data
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument(
        "--solutions",
        type=Path,
        required=True,
        help="Directory of <instance_id>.json action-list files",
    )
    ap.add_argument("--results", type=Path, default=None,
                    help="Output path (default: results/<solutions-dir-name>.json)")
    ap.add_argument("--only", action="append", default=None,
                    help="Restrict to the given instance_id (repeatable)")
    ap.add_argument("--perspective", type=int, default=0,
                    help="Scoring player (0 or 1, default 0)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    instances = load_dataset(args.dataset)
    if args.only:
        instances = {k: v for k, v in instances.items() if k in set(args.only)}
    if not instances:
        print("No instances to evaluate.", file=sys.stderr)
        return 1

    solutions = load_solutions(args.solutions, instances.keys())

    print(f"Loading card DB from {DB_DIR}...", file=sys.stderr)
    evaluator = SingleAttemptEvaluator(verbose=args.verbose)
    print(f"  {len(evaluator.card_db._cache)} cards loaded", file=sys.stderr)

    per_instance: dict[str, dict] = {}
    counts: dict[str, int] = {"win": 0, "loss": 0, "incomplete": 0, "error": 0, "missing": 0}

    for idx, (iid, inst) in enumerate(sorted(instances.items())):
        sol = solutions.get(iid)
        if sol is None:
            counts["missing"] += 1
            per_instance[iid] = {"status": "missing"}
            print(f"[{idx+1}/{len(instances)}] {iid}: MISSING", file=sys.stderr)
            continue
        if isinstance(sol, str):
            counts["error"] += 1
            per_instance[iid] = {"status": "error", "error": sol}
            print(f"[{idx+1}/{len(instances)}] {iid}: BAD FILE ({sol})", file=sys.stderr)
            continue

        t0 = time.time()
        result = evaluator.evaluate_one(
            inst["lua_setup"], sol, perspective=args.perspective,
        )
        elapsed = round(time.time() - t0, 2)
        counts[result.status] = counts.get(result.status, 0) + 1
        row = result.to_dict()
        row["elapsed"] = elapsed
        per_instance[iid] = row
        print(
            f"[{idx+1}/{len(instances)}] {iid}: {result.status.upper()} "
            f"({result.steps_applied}/{result.total_steps}, {elapsed}s)"
            + (f" | {result.error[:80]}" if result.error else ""),
            file=sys.stderr,
        )

    attempted = sum(counts[k] for k in ("win", "loss", "incomplete", "error"))
    summary = {
        "mode": "single-attempt",
        "dataset": str(args.dataset),
        "solutions": str(args.solutions),
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
    for key in ("win", "loss", "incomplete", "error", "missing"):
        print(f"  {key:<14s}: {counts.get(key, 0)}", file=sys.stderr)
    print(f"Win rate (all) : {summary['win_rate_all']:.1%}", file=sys.stderr)
    print(f"Win rate (atmp): {summary['win_rate_attempted']:.1%}", file=sys.stderr)
    print(f"Wrote {results_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
