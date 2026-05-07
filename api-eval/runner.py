"""Unified driver — run benchmark episodes per puzzle in either release mode.

The runner supports two release modes:
  * Fully interactive mode (``--interactive``): per-turn tool-use loop
    with one response verb per engine decision and observations between
    turns.  Constructs a fresh ``OCGEngine`` + ``Harness`` and runs a
    single ``Episode`` with the chosen ``ToolCallingProvider``.
  * N-attempts bulk mode (``--attempts N``): the model receives the full
    puzzle state once and returns a JSON list of ``{"tool", "args"}``
    calls; on failure (when N>1) it gets engine feedback and may
    resubmit.  ``N=1`` is single-shot; ``N>1`` is multi-attempt.

Writes one JSONL log per instance under
``results/<run_name>/<instance_id>.jsonl`` capturing every observation,
model turn, tool call, and outcome.  A summary JSON of
win/loss/timeout/crash counts is written at the end.

Only the Anthropic tool-use provider ships in-repo today; OpenAI and
vLLM tool-use providers can be added alongside ``AnthropicToolProvider``
in ``runner.py`` without changes elsewhere.
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
import time
from pathlib import Path
from typing import Any

from engine.core import (
    CARD_SCRIPT_DIR,
    DB_DIR,
    DYLIB_PATH,
    SCRIPT_DIR,
    CardDB,
    OCGEngine,
)
from engine.episode import Episode, ResumeError
from providers import (
    AnthropicToolProvider,
    ClaudeCLIToolProvider,
    DeepSeekToolProvider,
    OpenAIToolProvider,
    ToolCallingProvider,
    VLLMToolProvider,
)

REPO_ROOT = Path(__file__).resolve().parent
# Prefer the enriched dataset (lean release + locally-reconstituted
# card_details + prompt; produced by src/dataset/enrich.py at install
# time).  Fall back to the lean release if the enriched copy is
# absent: the runner still works but card_details has to be
# rebuilt on each puzzle observation.
_ENRICHED = REPO_ROOT / "data" / "yugioh_bench.enriched.jsonl"
_LEAN = REPO_ROOT / "data" / "yugioh_bench.jsonl"
DEFAULT_DATASET = _ENRICHED if _ENRICHED.exists() else _LEAN
DEFAULT_RESULTS_ROOT = REPO_ROOT / "results"


def _load_dataset(dataset: Path) -> list[dict]:
    """Read all instances from the dataset, sorted by instance_id.

    Sorting by id gives a stable canonical ordering so that
    --offset/--limit windows ("first 50", "next 50") refer to the
    same puzzles across invocations regardless of file insertion
    order in the underlying JSONL.
    """
    insts: list[dict] = []
    with open(dataset) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            insts.append(json.loads(line))
    insts.sort(key=lambda x: x["instance_id"])
    return insts


def _select_candidates(
    all_instances: list[dict],
    *,
    only: set[str] | None,
    puzzle_ids: set[str] | None,
    offset: int,
    limit: int | None,
) -> list[dict]:
    """Filter the dataset to the candidate set the user asked for.

    Explicit id selectors (``--only`` and ``--puzzle-ids``) take
    precedence over windowing — when any explicit id is given,
    ``--offset``/``--limit`` are ignored.  Otherwise the offset and
    limit window the sorted ordering produced by ``_load_dataset``.
    """
    explicit: set[str] = set()
    if only:
        explicit.update(only)
    if puzzle_ids:
        explicit.update(puzzle_ids)
    if explicit:
        out = [i for i in all_instances if i["instance_id"] in explicit]
        present = {i["instance_id"] for i in out}
        missing = sorted(explicit - present)
        if missing:
            print(f"WARNING: requested puzzle_ids not in dataset: {missing}", file=sys.stderr)
        return out
    selected = all_instances[offset:]
    if limit is not None:
        selected = selected[:limit]
    return selected


def _jsonl_has_outcome(p: Path) -> bool:
    """True iff the JSONL contains a terminal ``{"type": "outcome", ...}`` record.

    The Episode loop writes exactly one such record at the end of a
    controlled termination (game_over, tool_budget_exhausted,
    model_stopped_without_tool_call, etc).  A run interrupted before
    that point — provider exception, OOM, kill -9 — leaves a JSONL
    without an outcome line, which the pre-flight then re-runs
    (clean resume after API exhaustion etc).

    Parses each JSON line so the check is robust to formatting
    differences between writers.
    """
    if not p.exists():
        return False
    try:
        with p.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:  # noqa: BLE001
                    continue
                if rec.get("type") == "outcome":
                    return True
    except Exception:  # noqa: BLE001
        pass
    return False


def _parse_bulk_partial(log_path: Path) -> dict | None:
    """Read a partial bulk-mode JSONL and recover the resume state.

    Returns ``None`` if the file is missing, malformed, contains a
    terminal ``outcome`` row (already finished — should be SKIPped),
    or has no successful ``model_turn`` rows yet (no work to preserve;
    a fresh run is fine).

    Otherwise returns a dict with:
      - ``attempts_burned``: count of attempts whose API call succeeded
        (i.e. produced a ``model_turn`` row).
      - ``historical_actions``: ``[actions_a1, actions_a2, ...]`` where
        each entry is the action list extracted from that attempt's
        ``actions`` row, or ``[]`` if that attempt suffered a
        ``parse_error`` after the ``model_turn``.
      - ``usage_totals``: re-accumulated usage across the recorded
        ``model_turn`` rows so the re-run continues the running total.

    A run that aborted with a ``provider_error`` (network exhausted)
    after attempt N still has N successful ``model_turn`` rows; the
    failed call leaves no row, so attempts_burned correctly reflects
    "API spend already paid for".
    """
    if not log_path.exists():
        return None
    rows: list[dict] = []
    try:
        with log_path.open() as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    continue
    except Exception:  # noqa: BLE001
        return None
    if any(r.get("type") == "outcome" for r in rows):
        return None
    by_attempt: dict[int, dict] = {}
    for r in rows:
        t = r.get("type")
        if t == "model_turn":
            n = r.get("attempt")
            if isinstance(n, int):
                by_attempt.setdefault(n, {})["usage"] = r.get("usage", {})
                by_attempt[n]["had_model_turn"] = True
        elif t == "actions":
            n = r.get("attempt")
            if isinstance(n, int):
                by_attempt.setdefault(n, {})["actions"] = r.get("actions", [])
    burned_attempts = sorted(n for n, info in by_attempt.items() if info.get("had_model_turn"))
    if not burned_attempts:
        return None
    attempts_burned = burned_attempts[-1]
    # Historical actions for attempts 1..attempts_burned. Missing
    # actions row (parse_error) → [] which the inner evaluator will
    # treat as a no-op failure, consistent with the original run.
    historical_actions: list[list[dict]] = [
        by_attempt.get(i, {}).get("actions", []) for i in range(1, attempts_burned + 1)
    ]
    usage_totals = {
        "model_calls": 0,
        "model_wallclock_seconds": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "prompt_cache_hit_tokens": 0,
        "prompt_cache_miss_tokens": 0,
        "cache_read_input_tokens": 0,
    }
    for i in range(1, attempts_burned + 1):
        usage = by_attempt.get(i, {}).get("usage", {}) or {}
        if not by_attempt.get(i, {}).get("had_model_turn"):
            continue
        usage_totals["model_calls"] += 1
        for k, v in usage.items():
            if isinstance(v, (int, float)):
                usage_totals[k] = usage_totals.get(k, 0) + v
    return {
        "attempts_burned": attempts_burned,
        "historical_actions": historical_actions,
        "usage_totals": usage_totals,
    }


def _default_run_name(args) -> str:
    """Deterministic per-config sweep dir name.

    Different (mode, provider, model, effort, forage, show_solution)
    tuples land in different sweep directories so they don't clobber
    each other and idempotent re-runs find their previous outputs.

    Fully interactive mode lands under ``interactive-{provider}-...``;
    n-attempts bulk mode lands under ``n-attempts-{N}-{provider}-...``
    so the two modes coexist without overlap and N=1 vs N>1 sweeps stay
    visibly distinct.
    """
    model_safe = args.model.replace("/", "_")
    fg = "1" if args.forage else "0"
    sol = "1" if args.show_solution else "0"
    suffix = f"{args.provider}-{model_safe}-eff{args.effort}-forage{fg}-sol{sol}"
    if getattr(args, "interactive", False):
        return f"interactive-{suffix}"
    return f"n-attempts-{args.attempts}-{suffix}"


def build_provider(name: str, model: str, **kwargs) -> ToolCallingProvider:
    """Instantiate a tool-calling provider by short name.

    Providers:
      - ``anthropic``  Anthropic Messages API with native tool_use blocks.
      - ``openai``     Official OpenAI function-calling. Accepts ``base_url``
                       to point at any compatible endpoint (TGI, SGLang, etc).
      - ``vllm``       Same as openai, but defaults base_url to
                       ``http://localhost:8000/v1`` and api_key to ``"vllm"``.
                       Requires the vLLM server to be launched with
                       ``--enable-auto-tool-choice --tool-call-parser <...>``.
      - ``deepseek``   DeepSeek V4 Pro / V4 Flash via the OpenAI-compatible
                       endpoint at https://api.deepseek.com.  Reads
                       DEEPSEEK_API_KEY.  Defaults to ``deepseek-v4-pro``
                       with thinking mode (reasoning_effort=high) on.
                       Round-trips reasoning_content via provider_data
                       to satisfy DeepSeek's tool-call requirement.
      - ``claude-cli`` Shells out to the local ``claude`` CLI binary;
                       authenticates via the Max-subscription OAuth credential.
                       Built-in Claude Code tools are disabled and the system
                       prompt is fully replaced — the spawned model only sees
                       the interactive-mode schema and the rolled-up conversation.
    """
    if name == "anthropic":
        return AnthropicToolProvider(model=model, **kwargs)
    if name == "openai":
        return OpenAIToolProvider(model=model, **kwargs)
    if name in ("vllm", "vllm-openai", "vllm-server"):
        return VLLMToolProvider(model=model, **kwargs)
    if name == "deepseek":
        return DeepSeekToolProvider(model=model, **kwargs)
    if name in ("claude-cli", "claude_cli"):
        # claude-cli does not accept api_key / base_url — it picks up
        # OAuth from the local credential file.  Strip those if passed.
        kwargs = {k: v for k, v in kwargs.items() if k not in ("api_key", "base_url")}
        return ClaudeCLIToolProvider(model=model, **kwargs)
    raise ValueError(
        f"unknown interactive-mode tool provider {name!r}. "
        f"Known: anthropic, openai, vllm, deepseek, claude-cli."
    )


def run_one(
    inst: dict,
    provider: ToolCallingProvider,
    *,
    card_db: CardDB,
    dylib_path: Path,
    script_dir: Path,
    card_script_dir: Path,
    log_path: Path | None,
    max_tool_calls: int,
    perspective: int,
    verbose: bool = False,
    forage: bool = False,
    show_solution: bool = False,
    resume_from: Path | None = None,
) -> dict:
    """Fully interactive mode: run one episode through Episode.run()."""
    engine = OCGEngine(
        dylib_path=dylib_path,
        card_db=card_db,
        script_dir=script_dir,
        card_script_dir=card_script_dir,
        verbose=verbose,
    )
    try:
        # System prompt is built dynamically by Episode in _start() via
        # lib.prompt_builder.build_interactive_system_prompt — no
        # pre-construction needed here.
        episode = Episode(
            engine=engine,
            provider=provider,
            lua_setup=inst["lua_setup"],
            instance=inst,
            card_db=card_db,
            perspective=perspective,
            max_tool_calls=max_tool_calls,
            forage=forage,
            show_solution=show_solution,
            log_path=log_path,
            resume_from=resume_from,
        )
        outcome = episode.run()
    finally:
        try:
            engine.destroy()
        except Exception:  # noqa: BLE001
            pass
    return outcome


def run_one_bulk(
    inst: dict,
    provider: ToolCallingProvider,
    *,
    card_db: CardDB,
    dylib_path: Path,
    script_dir: Path,
    card_script_dir: Path,
    log_path: Path | None,
    max_attempts: int,
    perspective: int,
    show_solution: bool = False,
    resume_from: Path | None = None,
) -> dict:
    """N-attempts bulk mode (max_attempts=1 = single-shot, max_attempts>=2 = multi-attempt):

    The model receives the full puzzle state in one prompt, returns a
    JSON action list, and we replay it against a fresh harness.  On
    failure we feed the error context into a retry prompt and try
    again, up to ``max_attempts``.

    Resilience:
      * Provider calls are wrapped with exponential-backoff retry so a
        single transient ``Connection error`` doesn't burn the entire
        attempt budget.
      * If ``resume_from`` points at a partial JSONL (no terminal
        ``outcome`` row), historical attempts are replayed deterministically
        through the engine and only attempts beyond ``attempts_burned``
        burn fresh API calls.
      * If a fresh attempt's API call exhausts all retries, the run
        exits WITHOUT writing a terminal outcome — re-running the
        runner picks up cleanly from the same JSONL.

    Returns an outcome dict with the same shape as ``run_one()`` so
    the runner's sweep loop can record both modes uniformly:
        termination, game_over, winner, win_reason, win_reason_raw,
        turn_count, lp, tool_calls_used (= attempts_used here),
        last_events (= last EvalResult dump), model_usage_totals.
    """
    # Lazy imports — keeps interactive-mode startup fast.
    from engine.core import OCGEngine
    from engine.harness import Harness
    from engine.multi_attempt import MultiAttemptEvaluator
    from engine.replay import SingleAttemptEvaluator, classify_parse_failure, extract_actions
    from lib.prompt_builder import build_bulk_prompt

    log_f = None
    debug_f = None
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_f = log_path.open("a", buffering=1)
        # Side-channel for raw API responses (one JSON per call,
        # appended). Forensic only — never read by the runner.
        iid = inst.get("instance_id", "unknown")
        debug_dir = log_path.parent / "debug"
        debug_dir.mkdir(parents=True, exist_ok=True)
        debug_f = (debug_dir / f"{iid}.responses.jsonl").open("a", buffering=1)

    def _log(rec: dict) -> None:
        if log_f is not None:
            log_f.write(json.dumps(rec, default=str) + "\n")

    def _dump_response(turn, attempt_num: int) -> None:
        """Append the raw API response (provider's resp.model_dump())
        to the debug side-log. No-op if debug logging is off or the
        provider didn't populate raw_dict."""
        if debug_f is None or not getattr(turn, "raw_dict", None):
            return
        try:
            debug_f.write(
                json.dumps(
                    {
                        "attempt": attempt_num,
                        "wallclock_seconds": round(turn.wallclock_seconds, 3),
                        "raw": turn.raw_dict,
                    },
                    default=str,
                )
                + "\n"
            )
        except Exception:  # noqa: BLE001
            pass  # forensic only — never block the run

    def _close_logs() -> None:
        if log_f is not None:
            try:
                log_f.close()
            except Exception:
                pass
        if debug_f is not None:
            try:
                debug_f.close()
            except Exception:
                pass

    # Resume detection. If the partial JSONL has past attempts, we
    # replay their actions through the engine without re-calling the
    # LLM — preserving paid-for tokens.
    partial = _parse_bulk_partial(resume_from) if resume_from else None
    if partial:
        attempts_burned: int = partial["attempts_burned"]
        historical_actions: list[list[dict]] = partial["historical_actions"]
        usage_totals: dict = partial["usage_totals"]
        _log(
            {
                "type": "resume_from_partial",
                "prior_attempts": attempts_burned,
                "prior_usage": dict(usage_totals),
            }
        )
    else:
        attempts_burned = 0
        historical_actions = []
        usage_totals = {
            "model_calls": 0,
            "model_wallclock_seconds": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "prompt_cache_hit_tokens": 0,
            "prompt_cache_miss_tokens": 0,
            "cache_read_input_tokens": 0,
        }
        # Capture the system prompt + provider config once for the log
        # (only on a fresh start; resume preserves the original config row).
        provider_cfg = provider.provider_config_for_log()
        _log(
            {
                "type": "config",
                "mode": "bulk",
                "max_attempts": max_attempts,
                "perspective": perspective,
                "show_solution": show_solution,
                "system_prompt": inst.get("system_prompt", ""),
                "provider": provider_cfg,
            }
        )

    evaluator = SingleAttemptEvaluator(
        card_db=card_db,
        dylib_path=dylib_path,
        script_dir=script_dir,
        card_script_dir=card_script_dir,
    )
    pt2 = MultiAttemptEvaluator(max_attempts=max_attempts, inner=evaluator)

    # Sentinel — flipped True if a fresh attempt's provider.complete()
    # call exhausts all retries. We then skip writing the terminal
    # outcome row so the next runner invocation can resume.
    aborted_for_retry: dict[str, bool] = {"flag": False}

    # Sentinel — flipped True if a parse_error classified as
    # 'no_json_attempted' (model emitted no <solution> content and no
    # fenced JSON block). MultiAttemptEvaluator surrenders, and the
    # outcome is written as termination=model_surrender so the
    # summary distinguishes "model gave up" from "model exhausted
    # attempts".
    surrendered: dict[str, bool] = {"flag": False}

    # Carries parse-error context forward so the NEXT prompt's retry
    # context surfaces "your previous response didn't parse" instead
    # of an engine-error message that's nonsensical when there were
    # no actions to fail. Cleared once consumed by _build_prompt.
    parse_error_pending: dict[str, Any] = {}

    def _accumulate(turn) -> None:
        if turn is None:
            return
        usage_totals["model_calls"] += 1
        usage_totals["model_wallclock_seconds"] = round(
            usage_totals["model_wallclock_seconds"] + (turn.wallclock_seconds or 0.0), 3
        )
        for k, v in (turn.usage or {}).items():
            if isinstance(v, (int, float)):
                usage_totals[k] = usage_totals.get(k, 0) + v

    def _build_prompt(attempt_idx: int, prev_result=None):
        """Spin up a fresh harness so build_bulk_prompt has the
        live-engine state to render from."""
        eng = OCGEngine(
            dylib_path=dylib_path,
            card_db=card_db,
            script_dir=script_dir,
            card_script_dir=card_script_dir,
        )
        try:
            h = Harness(eng)
            h.start(inst["lua_setup"])
            kwargs = dict(
                instance=inst,
                card_db=card_db,
                harness=h,
                attempts=max_attempts,
                show_solution=show_solution,
                attempt_index=attempt_idx,
            )
            if parse_error_pending:
                # Override engine-error feedback with parse-error
                # feedback for THIS prompt only. Prev_result (if any)
                # is the engine's verdict on `[]` actions, which is
                # not what we want the model to react to.
                kwargs["last_failure"] = {
                    "parse_error": True,
                    "previous_text_preview": parse_error_pending.get("text", "")[:500],
                }
                parse_error_pending.clear()
            elif prev_result is not None:
                kwargs["last_failure"] = {
                    "error": prev_result.error or f"status={prev_result.status}",
                    "pending": prev_result.pending_after,
                    "failed_at_index": getattr(prev_result, "failure_step", None),
                }
            return build_bulk_prompt(**kwargs)
        finally:
            try:
                eng.destroy()
            except Exception:
                pass

    def _complete_with_retry(prompt: str, system: str, attempt_num: int, max_retries: int = 4):
        """Wrap provider.complete() with exponential backoff. Logs each
        retry. After max_retries+1 total tries, raises the last
        exception. Backoff: 5s → 10s → 20s → 40s → 80s (≈155s worst
        case before re-raise). Belt-and-braces above the SDK's own
        retry-on-transient-error layer."""
        delay = 5.0
        for attempt_no in range(max_retries + 1):
            try:
                return provider.complete(prompt, system=system)
            except Exception as e:  # noqa: BLE001
                if attempt_no == max_retries:
                    _log(
                        {
                            "type": "provider_error",
                            "attempt": attempt_num,
                            "retries_exhausted": max_retries,
                            "error": str(e),
                        }
                    )
                    raise
                _log(
                    {
                        "type": "provider_retry",
                        "attempt": attempt_num,
                        "retry": attempt_no + 1,
                        "of": max_retries,
                        "error": str(e),
                        "delay_seconds": delay,
                    }
                )
                time.sleep(delay)
                delay *= 2

    # First attempt — historical replay if resumed, else live LLM.
    if attempts_burned >= 1:
        first_actions = historical_actions[0]
    else:
        first_prompt = _build_prompt(0)
        try:
            first_turn = _complete_with_retry(
                first_prompt, inst.get("system_prompt", ""), attempt_num=1
            )
        except Exception as e:  # noqa: BLE001
            # Provider exhausted all retries on the very first call.
            # Don't write a terminal outcome — leave the JSONL partial
            # so a re-run can pick up where we left off.
            _close_logs()
            return {
                "termination": "aborted_retryable",
                "error": f"{type(e).__name__}: {e}",
                "game_over": False,
                "winner": None,
                "win_reason": None,
                "win_reason_raw": None,
                "turn_count": 0,
                "lp": [None, None],
                "tool_calls_used": 0,
                "last_events": [],
                "model_usage_totals": usage_totals,
                "_no_outcome_row": True,
            }
        first_text = first_turn.text
        _accumulate(first_turn)
        _dump_response(first_turn, attempt_num=1)
        _log(
            {
                "type": "model_turn",
                "attempt": 1,
                "text": first_text,
                "stop_reason": first_turn.stop_reason,
                "provider_data": dict(first_turn.provider_data),
                "usage": first_turn.usage,
                "elapsed_seconds": round(first_turn.wallclock_seconds, 3),
            }
        )
        try:
            first_actions = extract_actions(first_text)
        except Exception as e:  # noqa: BLE001
            kind = classify_parse_failure(first_text)
            _log(
                {
                    "type": "parse_error",
                    "attempt": 1,
                    "kind": kind,
                    "error": str(e),
                    "raw_text_preview": first_text[:500],
                }
            )
            if kind == "no_json_attempted":
                # Model emitted no recognizable solution wrapper —
                # treat as genuine surrender. Write outcome and exit.
                outcome = {
                    "termination": "model_surrender",
                    "game_over": False,
                    "winner": None,
                    "win_reason": None,
                    "win_reason_raw": None,
                    "turn_count": 0,
                    "lp": [None, None],
                    "tool_calls_used": 1,
                    "last_events": [],
                    "model_usage_totals": usage_totals,
                    "surrender_text_preview": first_text[:500],
                }
                _log({"type": "outcome", **outcome})
                _close_logs()
                return outcome
            # attempted_invalid_json — set parse-error context for the
            # next prompt and let MultiAttemptEvaluator iterate.  An empty
            # action list will fail in the engine; _resubmit then
            # builds the next prompt with parse-error feedback.
            parse_error_pending["text"] = first_text
            first_actions = []
        _log({"type": "actions", "attempt": 1, "actions": first_actions})

    def _resubmit(idx: int, prev_result, instance: dict):
        attempt_num = idx + 2  # idx=0 is first retry, so this is attempt 2
        if attempt_num > max_attempts:
            return None
        # Resume: replay historical actions for already-paid attempts.
        if attempt_num <= attempts_burned:
            return historical_actions[attempt_num - 1]
        # Fresh attempt — live LLM call with retry helper.
        retry_prompt = _build_prompt(idx + 1, prev_result=prev_result)
        try:
            turn = _complete_with_retry(
                retry_prompt, instance.get("system_prompt", ""), attempt_num=attempt_num
            )
        except Exception:  # noqa: BLE001
            # Retries exhausted — flag for outcome-write skip.
            aborted_for_retry["flag"] = True
            return None
        text = turn.text
        _accumulate(turn)
        _dump_response(turn, attempt_num=attempt_num)
        _log(
            {
                "type": "model_turn",
                "attempt": attempt_num,
                "text": text,
                "stop_reason": turn.stop_reason,
                "provider_data": dict(turn.provider_data),
                "usage": turn.usage,
                "elapsed_seconds": round(turn.wallclock_seconds, 3),
            }
        )
        try:
            actions = extract_actions(text)
        except Exception as e:  # noqa: BLE001
            kind = classify_parse_failure(text)
            _log(
                {
                    "type": "parse_error",
                    "attempt": attempt_num,
                    "kind": kind,
                    "error": str(e),
                    "raw_text_preview": text[:500],
                }
            )
            if kind == "no_json_attempted":
                # Model surrendered — terminate the puzzle. Setting
                # surrendered.flag here causes the post-loop block to
                # write outcome=model_surrender and skip the gave_up
                # default.
                surrendered["flag"] = True
                surrendered["text"] = text  # type: ignore[assignment]
                return None
            # attempted_invalid_json — feed parse-error context to the
            # NEXT prompt; empty actions burn the current slot in
            # MultiAttemptEvaluator.
            parse_error_pending["text"] = text
            return []
        _log({"type": "actions", "attempt": attempt_num, "actions": actions})
        return actions

    pt2_result = pt2.evaluate_one(inst, first_actions, _resubmit, perspective=perspective)

    # Provider retries exhausted on a fresh attempt → don't write a
    # terminal outcome row. Re-running the runner will see no outcome
    # in the JSONL and resume from the last successful model_turn.
    if aborted_for_retry["flag"]:
        _close_logs()
        return {
            "termination": "aborted_retryable",
            "game_over": False,
            "winner": None,
            "win_reason": None,
            "win_reason_raw": None,
            "turn_count": 0,
            "lp": [None, None],
            "tool_calls_used": pt2_result.attempts_used,
            "last_events": [],
            "model_usage_totals": usage_totals,
            "bulk_attempts_per_attempt": pt2_result.per_attempt,
            "_no_outcome_row": True,
        }

    # Translate MultiAttemptResult into the runner's outcome dict shape.
    final = pt2_result.final or {}
    if surrendered["flag"]:
        # A parse_error classified as 'no_json_attempted' triggered
        # _resubmit to return None mid-loop. Distinguish this from
        # ordinary attempt-exhaustion gave_up.
        termination = "model_surrender"
        winner = None
    elif pt2_result.status == "win":
        termination = "game_over"
        winner = perspective
    elif pt2_result.status == "loss":
        termination = "game_over"
        winner = 1 - perspective
    elif pt2_result.status == "incomplete":
        termination = "incomplete"
        winner = None
    elif pt2_result.status == "error":
        termination = "exception"
        winner = None
    else:
        termination = pt2_result.status  # "gave_up" etc
        winner = None

    outcome = {
        "termination": termination,
        "game_over": pt2_result.status in ("win", "loss"),
        "winner": winner,
        "win_reason": None,
        "win_reason_raw": None,
        "turn_count": final.get("turn_count", 0),
        "lp": final.get("lp", [None, None]),
        "tool_calls_used": pt2_result.attempts_used,
        "last_events": [],
        "model_usage_totals": usage_totals,
        "bulk_attempts_per_attempt": pt2_result.per_attempt,
    }
    if surrendered["flag"]:
        outcome["surrender_text_preview"] = surrendered.get("text", "")[:500]
    _log({"type": "outcome", **outcome})
    _close_logs()
    return outcome


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    ap.add_argument(
        "--provider",
        default="anthropic",
        choices=["anthropic", "openai", "vllm", "deepseek", "claude-cli"],
        help="Tool-calling provider",
    )
    ap.add_argument(
        "--model",
        required=True,
        help="Model id passed to the provider "
        "(e.g. claude-opus-4-7, gpt-4o, "
        "meta-llama/Llama-3.1-8B-Instruct)",
    )
    ap.add_argument(
        "--base-url",
        default=None,
        help="OpenAI-compatible endpoint URL (for openai/vllm providers; ignored for anthropic)",
    )
    ap.add_argument("--run-name", default=None)
    ap.add_argument("--results-root", type=Path, default=DEFAULT_RESULTS_ROOT)
    ap.add_argument("--api-key", default=None)
    ap.add_argument(
        "--max-tokens",
        type=int,
        default=128000,
        help="Per-turn output cap.  For Anthropic with adaptive "
        "thinking on (Opus 4.7+), this includes BOTH thinking "
        "and visible-content tokens.  Default 128000 (the "
        "Opus 4.7 standard ceiling).  Note: this is the "
        "per-turn OUTPUT cap, distinct from the model's "
        "input context window.",
    )
    ap.add_argument(
        "--temperature",
        type=float,
        default=1.0,
        help="Sampling temperature.  Anthropic forces 1.0 when "
        "adaptive thinking is on; default matches.",
    )
    ap.add_argument(
        "--effort",
        default="max",
        choices=["off", "low", "medium", "high", "xhigh", "max"],
        help="Anthropic effort level (output_config.effort).  "
        "Controls thinking depth AND overall token spend "
        "(text + tool calls).  'off' omits the parameter "
        "(model default = high).  'xhigh' is Anthropic's "
        "recommended starting point for coding/agentic work; "
        "'max' is reserved for genuinely frontier problems.  "
        "Empirical note from 2026-05-02 yugi-bench tests: "
        "for puzzles where most cost is conversation re-"
        "processing (cache_read), effort barely shifts "
        "total $ — xhigh saved ~5%% vs max on a 44-turn "
        "puzzle.  Honoured only by the anthropic provider.",
    )
    ap.add_argument(
        "--no-adaptive-thinking",
        action="store_true",
        help="Disable adaptive thinking on the anthropic "
        "provider (Opus 4.7 will then run without any "
        "thinking blocks).  Default off — adaptive "
        "thinking is on.",
    )
    ap.add_argument(
        "--stream-event-log",
        default=None,
        help="Path to write per-streaming-event JSONL "
        "(one event per line, content_block_delta + "
        "input_json events skipped to keep volume sane).  "
        "Default: <out_dir>/stream-events.jsonl when the "
        "anthropic provider is in use.  Pass an explicit "
        "path to override, or use --no-stream-event-log to "
        "disable.  Honoured only by anthropic provider.",
    )
    ap.add_argument(
        "--no-stream-event-log",
        action="store_true",
        help="Disable the default stream-event side log.  "
        "Use this for very large sweeps where the extra "
        "I/O matters; otherwise leave it on for full "
        "forensic visibility.",
    )
    ap.add_argument(
        "--max-tool-calls", type=int, default=500, help="Per-episode tool-call budget (default 500)"
    )
    ap.add_argument("--perspective", type=int, default=0)
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Take at most N puzzles after sorting and "
        "applying --offset.  Ignored when an explicit "
        "id selector (--only / --puzzle-ids) is used.",
    )
    ap.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Skip the first N puzzles after sorting.  "
        "Combined with --limit lets you split the "
        "dataset into stable batches: --offset 0 "
        "--limit 50 (first 50), --offset 50 --limit "
        "50 (next 50), etc.  Ignored when an explicit "
        "id selector is used.",
    )
    ap.add_argument(
        "--only",
        action="append",
        default=None,
        help="Run only this puzzle id (repeatable).  Equivalent to --puzzle-ids for a single id.",
    )
    ap.add_argument(
        "--puzzle-ids",
        default=None,
        help="Comma-separated list of puzzle ids to run.  "
        "Bypasses --offset/--limit.  Combines with "
        "--only if both are given.",
    )
    ap.add_argument(
        "--overwrite",
        action="store_true",
        help="Re-run puzzles even if a finished JSONL "
        "exists in the sweep dir.  Default: skip "
        "puzzles whose JSONL already contains a "
        "terminal outcome record (idempotent re-run).",
    )
    ap.add_argument(
        "--provider-timeout-seconds",
        type=int,
        default=None,
        help="Per-respond() watchdog timeout for the provider "
        "(currently only honoured by claude-cli).  Default "
        "240 s on claude-cli; set lower to fail wedged "
        "first-response hangs faster, higher for puzzles "
        "where the model genuinely thinks longer.",
    )
    ap.add_argument(
        "--forage",
        action="store_true",
        help="Enable inspection tools (get_state, "
        "pending_decision, inspect_card, get_glossary) "
        "AND give the model the full omniscient state + "
        "full card glossary in the system prompt.  "
        "Without --forage, fully interactive mode plays "
        "'like a real duel': model sees only what the engine reveals "
        "to the player normally (no deck contents, no "
        "opp set spell/trap identities), and the only "
        "non-response tool available is `restart`.",
    )
    ap.add_argument(
        "--show-solution",
        action="store_true",
        help="Inject the puzzle's natural-language gold "
        "solution walkthrough into the system prompt.  "
        "This is a CEILING-TEST mode — the model gets "
        "the answer.  Useful for measuring 'can the "
        "model execute a known plan' separated from "
        "'can it find the plan from scratch'.  Mark "
        "any results from this mode as oracle-runs "
        "in your reporting.",
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help="Run this many puzzles in parallel (thread pool — "
        "each puzzle owns its own engine instance, the "
        "provider is shared).  Default 1 (sequential).",
    )

    # --- Mode selection: bulk (--attempts N) vs interactive (default).
    ap.add_argument(
        "--interactive",
        action="store_true",
        help="Fully interactive mode: per-turn tool-use "
        "loop with the model calling response verbs + "
        "inspection tools (get_state, pending_decision, "
        "get_glossary, restart).  This is the default "
        "when --attempts is not set.  Mutually exclusive "
        "with --attempts.",
    )
    ap.add_argument(
        "--attempts",
        type=int,
        default=None,
        help="N-attempts bulk mode: the model receives the "
        "full puzzle in one prompt and returns a JSON "
        "action list, which is replayed against a fresh "
        "harness.  Failed attempts feed the engine's "
        "error back into the next prompt up to N times.  "
        "--attempts 1 = single-shot (no retry).  "
        "--attempts 3 = up to three attempts with "
        "engine feedback between them.  "
        "Mutually exclusive with --interactive.",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    # Mode reconciliation.
    if args.interactive and args.attempts is not None:
        ap.error("--interactive and --attempts are mutually exclusive")
    if args.attempts is None and not args.interactive:
        # Default = interactive mode (preserves the historical
        # behaviour of `python -m engine.run_inference`).
        args.interactive = True
    if args.attempts is not None and args.attempts < 1:
        ap.error("--attempts must be >= 1")

    # Resolve out_dir up front so the stream-event log default can land
    # inside it.  Default run_name encodes the full config tuple so that
    # different (effort, forage, show_solution) combinations land in
    # different sweep dirs and don't clobber each other on idempotent
    # re-runs.
    run_name = args.run_name or _default_run_name(args)
    out_dir = args.results_root / run_name
    out_dir.mkdir(parents=True, exist_ok=True)

    kwargs: dict = {"max_tokens": args.max_tokens, "temperature": args.temperature}
    if args.api_key:
        kwargs["api_key"] = args.api_key
    if args.base_url and args.provider in ("openai", "vllm"):
        kwargs["base_url"] = args.base_url
    if args.provider_timeout_seconds is not None and args.provider == "claude-cli":
        kwargs["timeout_seconds"] = args.provider_timeout_seconds
    if args.provider == "deepseek":
        # DeepSeek accepts only "high" or "max" for reasoning_effort
        # (low/medium are auto-mapped to high; xhigh is Anthropic-only).
        # Map any sub-high value to "high" for parity.
        eff = args.effort
        if eff in ("low", "medium"):
            eff = "high"
        elif eff == "xhigh":
            eff = "max"
        kwargs["reasoning_effort"] = eff if eff != "off" else None
    if args.provider == "anthropic":
        kwargs["effort"] = args.effort if args.effort != "off" else None
        kwargs["adaptive_thinking"] = not args.no_adaptive_thinking
        if not args.no_stream_event_log:
            # Default the stream-event log into the run's out_dir so every
            # anthropic run gets full forensic visibility without an opt-in.
            kwargs["stream_event_log_path"] = args.stream_event_log or str(
                out_dir / "stream-events.jsonl"
            )
    provider = build_provider(args.provider, args.model, **kwargs)

    print(f"Loading card DB from {DB_DIR}...", file=sys.stderr)
    card_db = CardDB(Path(DB_DIR))
    print(f"  {len(card_db._cache)} cards loaded", file=sys.stderr)

    per_instance: dict[str, dict] = {}
    counts: dict[str, int] = {}

    # Build the candidate set from the dataset, then classify each
    # candidate against the sweep dir so the operator sees what will
    # actually run before any model calls fire.
    only = set(args.only) if args.only else None
    puzzle_ids = set(s.strip() for s in args.puzzle_ids.split(",")) if args.puzzle_ids else None
    all_insts = _load_dataset(args.dataset)
    candidates = _select_candidates(
        all_insts,
        only=only,
        puzzle_ids=puzzle_ids,
        offset=args.offset,
        limit=args.limit,
    )

    pending: list[dict] = []
    skip_finished: list[str] = []
    resume_partial: list[str] = []
    for inst in candidates:
        iid = inst["instance_id"]
        log_path = out_dir / f"{iid}.jsonl"
        if args.overwrite:
            pending.append(inst)
            continue
        if _jsonl_has_outcome(log_path):
            skip_finished.append(iid)
        else:
            # Either no JSONL at all, or one without a terminal outcome.
            # The latter case means the previous attempt died mid-run
            # (API exhausted, kill -9, OOM, etc.); Episode will rebuild
            # state from the trace and continue the loop from the
            # crash point — no API spend on the already-completed turns.
            if log_path.exists():
                resume_partial.append(iid)
            pending.append(inst)

    def _show_ids(ids: list[str], cap: int = 10) -> None:
        for iid in ids[:cap]:
            print(f"    - {iid}", file=sys.stderr)
        if len(ids) > cap:
            print(f"    ... +{len(ids) - cap} more", file=sys.stderr)

    print(f"\n=== Pre-flight scan (results dir: {out_dir}) ===", file=sys.stderr)
    print(f"  Candidates       : {len(candidates)}", file=sys.stderr)
    print(f"  SKIP (finished)  : {len(skip_finished)}", file=sys.stderr)
    if skip_finished:
        _show_ids(skip_finished)
    print(
        f"  RUN  (pending)   : {len(pending)}  ({len(resume_partial)} resuming from partial JSONL)",
        file=sys.stderr,
    )
    if pending:
        _show_ids([i["instance_id"] for i in pending])
    if resume_partial:
        print("  ↳ resuming mid-puzzle (zero API cost for replayed turns):", file=sys.stderr)
        _show_ids(resume_partial, cap=5)
    print("", file=sys.stderr)

    if not pending:
        print("Nothing to run.  (Pass --overwrite to re-run finished puzzles.)", file=sys.stderr)
        return 0

    print(f"Running {len(pending)} puzzles (concurrency={args.concurrency})", file=sys.stderr)

    def _run_and_record(inst: dict) -> tuple[str, dict, float]:
        iid = inst["instance_id"]
        log_path = out_dir / f"{iid}.jsonl"
        t0 = time.time()
        # Bulk mode: pass an existing partial JSONL as resume_from so
        # paid attempts aren't re-burned. run_one_bulk parses it and
        # only fires fresh API calls beyond the recorded attempts.
        if not args.interactive:
            resume_from = log_path if log_path.exists() else None
            try:
                outcome = run_one_bulk(
                    inst,
                    provider,
                    card_db=card_db,
                    dylib_path=Path(DYLIB_PATH),
                    script_dir=Path(SCRIPT_DIR),
                    card_script_dir=Path(CARD_SCRIPT_DIR),
                    log_path=log_path,
                    max_attempts=args.attempts,
                    perspective=args.perspective,
                    show_solution=args.show_solution,
                    resume_from=resume_from,
                )
            except Exception as e:  # noqa: BLE001
                outcome = {
                    "termination": "exception",
                    "error": f"{type(e).__name__}: {e}",
                    "game_over": False,
                    "winner": None,
                    "tool_calls_used": 0,
                }
            dt = time.time() - t0
            return iid, outcome, dt

        # Interactive mode: existing Episode + resume path.
        # If a partial JSONL exists, pass it as resume_from — Episode
        # rebuilds engine + conversation from the trace and continues
        # the loop from the recorded crash point (no API calls for the
        # already-completed turns).  If the rebuild fails (corrupt
        # JSONL, replay divergence), Episode raises ResumeError and we
        # fall back to a fresh run by unlinking the stale trace.
        resume_from = log_path if log_path.exists() else None
        try:
            outcome = run_one(
                inst,
                provider,
                card_db=card_db,
                dylib_path=Path(DYLIB_PATH),
                script_dir=Path(SCRIPT_DIR),
                card_script_dir=Path(CARD_SCRIPT_DIR),
                log_path=log_path,
                max_tool_calls=args.max_tool_calls,
                perspective=args.perspective,
                verbose=args.verbose,
                forage=args.forage,
                show_solution=args.show_solution,
                resume_from=resume_from,
            )
        except ResumeError as e:
            # Rebuild from the stale JSONL failed.  Drop it and try
            # again from scratch.
            print(f"[{iid}] resume failed ({e}), retrying fresh", file=sys.stderr)
            try:
                log_path.unlink()
            except FileNotFoundError:
                pass
            try:
                outcome = run_one(
                    inst,
                    provider,
                    card_db=card_db,
                    dylib_path=Path(DYLIB_PATH),
                    script_dir=Path(SCRIPT_DIR),
                    card_script_dir=Path(CARD_SCRIPT_DIR),
                    log_path=log_path,
                    max_tool_calls=args.max_tool_calls,
                    perspective=args.perspective,
                    verbose=args.verbose,
                    forage=args.forage,
                    show_solution=args.show_solution,
                )
            except Exception as e2:  # noqa: BLE001
                outcome = {
                    "termination": "exception",
                    "error": f"{type(e2).__name__}: {e2}",
                    "game_over": False,
                    "winner": None,
                    "tool_calls_used": 0,
                }
        except Exception as e:  # noqa: BLE001
            outcome = {
                "termination": "exception",
                "error": f"{type(e).__name__}: {e}",
                "game_over": False,
                "winner": None,
                "tool_calls_used": 0,
            }
        dt = time.time() - t0
        return iid, outcome, dt

    def _record(iid: str, outcome: dict, dt: float) -> None:
        term = outcome.get("termination", "unknown")
        counts[term] = counts.get(term, 0) + 1
        won = outcome.get("winner") == args.perspective
        counts["_wins"] = counts.get("_wins", 0) + (1 if won else 0)
        row = dict(outcome)
        row["elapsed"] = round(dt, 2)
        per_instance[iid] = row
        # Atomic-ish progress write so an external monitor (eg. a tail
        # process or a watchdog) can read aggregate progress without
        # waiting for the whole sweep to finish.
        try:
            with (out_dir / "_progress.json").open("w") as f:
                json.dump(
                    {
                        "counts": counts,
                        "completed": len(per_instance),
                        "pending": len(pending) - len(per_instance),
                    },
                    f,
                    indent=2,
                )
        except Exception:
            pass
        print(
            f"[{iid}] {term.upper()} "
            f"winner={outcome.get('winner')} "
            f"tool_calls={outcome.get('tool_calls_used')} ({dt:.1f}s)",
            flush=True,
        )

    if args.concurrency <= 1:
        for inst in pending:
            iid, outcome, dt = _run_and_record(inst)
            _record(iid, outcome, dt)
    else:
        # ThreadPool — `respond()` blocks on `claude -p` subprocess
        # IO so threads release the GIL while the LLM thinks.  Each
        # OCGEngine is created inside the worker thread (per-puzzle),
        # so engine instances aren't shared across threads even though
        # the dylib itself is.  This has worked fine in practice; if
        # we hit a libocgcore re-entrancy crash the fallback is a
        # ProcessPoolExecutor (each worker owns its own card_db).
        from concurrent.futures import ThreadPoolExecutor, as_completed

        with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
            futs = {ex.submit(_run_and_record, inst): inst["instance_id"] for inst in pending}
            for fut in as_completed(futs):
                iid = futs[fut]
                try:
                    iid2, outcome, dt = fut.result()
                except Exception as e:  # noqa: BLE001
                    outcome = {
                        "termination": "future_exception",
                        "error": f"{type(e).__name__}: {e}",
                        "game_over": False,
                        "winner": None,
                        "tool_calls_used": 0,
                    }
                    dt = 0.0
                _record(iid, outcome, dt)

    # Merge with any pre-existing summary in this sweep dir so that
    # incremental runs (e.g. --offset 0/--limit 50 then --offset 50/
    # --limit 50) accumulate correctly instead of clobbering the prior
    # batch.  Newly-run puzzles overwrite their previous entries.
    summary_path = out_dir / "_summary.json"
    merged_per_instance: dict[str, dict] = {}
    if summary_path.exists():
        try:
            existing = json.loads(summary_path.read_text())
            merged_per_instance = dict(existing.get("per_instance", {}))
        except Exception:  # noqa: BLE001
            pass
    merged_per_instance.update(per_instance)
    merged_counts: dict[str, int] = {}
    for row in merged_per_instance.values():
        term = row.get("termination", "unknown")
        merged_counts[term] = merged_counts.get(term, 0) + 1
        if row.get("winner") == args.perspective:
            merged_counts["_wins"] = merged_counts.get("_wins", 0) + 1
    summary_path.write_text(
        json.dumps(
            {"counts": merged_counts, "per_instance": merged_per_instance},
            indent=2,
            default=str,
        )
    )

    counts = merged_counts
    wins = counts.pop("_wins", 0)
    total = sum(counts.values())
    print(file=sys.stderr)
    print(f"Total episodes : {total}", file=sys.stderr)
    for k, v in sorted(counts.items()):
        print(f"  {k:<28s}: {v}", file=sys.stderr)
    print(f"Wins (perspective={args.perspective}): {wins}/{total}", file=sys.stderr)
    print(f"Wrote {summary_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
