#!/usr/bin/env python3
"""Aggregate per-puzzle results from a runner sweep into headline tables.

Walks one or more ``results/<run_name>/`` directories produced by
``runner.py``, reads each puzzle's terminal ``outcome`` row, joins
against ``data/yugioh_bench.jsonl`` for puzzle metadata (complexity
tier, source kind), and emits:

* Top-line counts (wins / losses / non-terminated)
* Breakdown by termination type
* Breakdown by complexity tier (1-10)
* Breakdown by source kind, with official-vs-community partition
* Optional per-puzzle table
* Optional cost rollup when usage totals are logged

Output formats: ``text`` (rich-like ASCII tables, default), ``json``,
``csv``, ``md``.

Examples::

    # Headline summary on stdout
    python api-eval/aggregate.py results/interactive-deepseek-v4-pro-effmax-forage0-sol0/

    # Multiple runs, JSON for downstream processing
    python api-eval/aggregate.py results/interactive-* --format json

    # Per-puzzle markdown table for a paper appendix
    python api-eval/aggregate.py results/run-x --format md --per-puzzle > supp/run-x.md

The aggregator is designed to be run by reviewers and supplementary-
material readers: drop a `runs/` tree into a freshly cloned yugi-bench
checkout and `python api-eval/aggregate.py runs/<name>` reproduces the
paper's headline numbers without API spend.
"""

from __future__ import annotations

import sys
from pathlib import Path as _Path

_REPO_ROOT = _Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))


import argparse
import csv
import io
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = REPO_ROOT / "data" / "yugioh_bench.jsonl"

# Source-kind partition. "Official" sources are puzzles drawn from
# Konami-shipped Yu-Gi-Oh! titles; "Community" sources are
# user-contributed compilations.
OFFICIAL_SOURCES = {
    "Duel Links",
    "GX Spirit Caller",
    "Nightmare Troubadour",
    "World Championship",
}
COMMUNITY_SOURCES = {"Miscellaneous"}


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------
@dataclass
class PuzzleOutcome:
    """One puzzle's distilled outcome, derived from the raw JSONL trace.

    The classification is computed by independently walking the trace's
    events (not by trusting a pre-recorded ``outcome`` row), so a sweep
    run by anyone — under any harness version, with any
    runner-side instrumentation — produces the same headline numbers
    when its traces land in this aggregator.

    Recorded vs derived fields are kept separate so that mismatches
    surface explicitly under ``--strict``.
    """

    instance_id: str
    run_name: str
    jsonl_path: Path
    # Derived fields — computed from the event stream, authoritative.
    derived_status: str = "no_outcome"
    derived_winner: int | None = None
    derived_win_reason: str | None = None
    derived_tool_calls: int = 0
    perspective: int = 0
    # Recorded fields — read from the trace's terminal ``outcome`` row,
    # if present. Used for cross-validation only (see ``--strict``).
    recorded_termination: str | None = None
    recorded_winner: int | None = None
    recorded_tool_calls: int | None = None
    recorded_status: str | None = None
    # Token + wallclock rollup, summed independently across model_turn rows.
    usage_totals: dict[str, Any] = field(default_factory=dict)
    # Joined metadata (filled from the dataset).
    source: str | None = None
    source_kind: str | None = None  # "official" | "community" | "other"
    complexity_tier: int | None = None  # 1..10 (parsed from "N/10")
    objective: str | None = None
    has_gold_solution: bool | None = None

    @property
    def status(self) -> str:
        return self.derived_status

    @property
    def termination(self) -> str | None:
        """Compatibility alias surfacing the derived termination."""
        if self.derived_status in ("win", "loss"):
            return "game_over"
        if self.derived_status == "stopped":
            return "model_stopped_without_tool_call"
        if self.derived_status == "exhausted":
            return "tool_budget_exhausted"
        if self.derived_status == "crashed":
            return "exception"
        if self.derived_status == "no_outcome":
            return None
        return self.derived_status

    @property
    def winner(self) -> int | None:
        return self.derived_winner

    @property
    def tool_calls_used(self) -> int | None:
        return self.derived_tool_calls

    @property
    def has_recorded_mismatch(self) -> bool:
        """True iff the recorded outcome row disagrees with the derived one."""
        if self.recorded_status is None:
            return False
        return self.recorded_status != self.derived_status


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------
def _parse_complexity(s: str | None) -> int | None:
    """Parse ``"7/10"`` -> 7. Tolerate missing or malformed values."""
    if not s:
        return None
    try:
        head = str(s).split("/", 1)[0]
        return int(head)
    except (TypeError, ValueError):
        return None


def _classify_source_kind(source: str | None) -> str:
    if source in OFFICIAL_SOURCES:
        return "official"
    if source in COMMUNITY_SOURCES:
        return "community"
    return "other"


def load_dataset_metadata(dataset_path: Path) -> dict[str, dict]:
    """Return ``{instance_id: metadata_dict}`` from the benchmark JSONL."""
    out: dict[str, dict] = {}
    with dataset_path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            iid = d.get("instance_id")
            if iid:
                out[iid] = d.get("metadata", {})
    return out


def _classify_recorded(termination: str | None, winner: int | None, perspective: int) -> str:
    """Re-classify a recorded outcome row's (termination, winner) into the
    aggregator's status taxonomy. Used only for ``has_recorded_mismatch``."""
    if termination is None:
        return "no_outcome"
    if termination == "game_over":
        if winner is None:
            return "no_outcome"
        return "win" if winner == perspective else "loss"
    if termination == "model_stopped_without_tool_call":
        return "stopped"
    if termination == "tool_budget_exhausted":
        return "exhausted"
    if termination == "exception":
        return "crashed"
    return "incomplete"


def derive_from_trace(jsonl_path: Path) -> dict[str, Any]:
    """Re-derive a puzzle's outcome from the trace's raw event stream.

    The aggregator does not trust any pre-recorded ``outcome`` row.
    Instead it walks the trace itself:

      - **win/loss** is decided by ``MSG_WIN`` events emitted from any
        ``tool_result`` row. ``MSG_WIN.winner`` against the
        ``config.perspective`` field tells us which side the puzzle
        was scored for.
      - **stopped** = no ``MSG_WIN``, and the trailing ``model_turn``
        had zero tool_calls (model gave up the floor).
      - **exhausted** = no ``MSG_WIN``, and the running tool_call
        count reached ``config.max_tool_calls``.
      - **crashed** = no ``MSG_WIN``, and there's a ``provider_error``
        or similarly-named fatal-error row in the trace.
      - **no_outcome** = the trace was cut short before a clean
        termination signal of any kind.

    Returns a dict with: ``status``, ``winner``, ``win_reason``,
    ``tool_calls`` (count), ``perspective``, ``usage_totals``, and
    ``recorded_*`` fields when a terminal ``outcome`` row exists (so
    callers can cross-validate).
    """
    perspective = 0
    max_tool_calls: int | None = None
    tool_calls_count = 0
    last_model_turn_tool_calls: int | None = None
    saw_msg_win = False
    win_winner: int | None = None
    win_reason: str | None = None
    saw_provider_error = False
    saw_exception_event = False
    usage_totals: Counter[str] = Counter()
    recorded: dict[str, Any] | None = None

    def _check_msg_win(ev: Any) -> None:
        nonlocal saw_msg_win, win_winner, win_reason
        if not isinstance(ev, dict):
            return
        if ev.get("msg_name") == "MSG_WIN":
            saw_msg_win = True
            if isinstance(ev.get("winner"), int):
                win_winner = ev["winner"]
            if ev.get("win_reason"):
                win_reason = ev["win_reason"]

    try:
        with jsonl_path.open() as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rtype = rec.get("type")
                if rtype == "config":
                    perspective = rec.get("perspective", 0) or 0
                    if isinstance(rec.get("max_tool_calls"), int):
                        max_tool_calls = rec["max_tool_calls"]
                elif rtype == "model_turn":
                    tcs = rec.get("tool_calls") or []
                    tool_calls_count += len(tcs)
                    last_model_turn_tool_calls = len(tcs)
                    cumu = rec.get("cumulative") or {}
                    if isinstance(cumu, dict):
                        for k, v in cumu.items():
                            if isinstance(v, (int, float)):
                                # cumulative is a running total; keep the
                                # max we observed (last writer wins on
                                # well-formed traces).
                                if v > usage_totals.get(k, 0):
                                    usage_totals[k] = v
                elif rtype == "tool_result":
                    content = rec.get("content")
                    if isinstance(content, str):
                        try:
                            content = json.loads(content)
                        except json.JSONDecodeError:
                            content = None
                    if isinstance(content, dict):
                        for ev in content.get("events", []) or []:
                            _check_msg_win(ev)
                elif rtype == "auto_chain_decline":
                    # The harness auto-declines an optional chain window
                    # between batched tool_calls. The events it captures
                    # can include the duel's terminal MSG_WIN.
                    for ev in rec.get("events", []) or []:
                        _check_msg_win(ev)
                elif rtype == "harness_recovery_advance":
                    for ev in rec.get("events", []) or []:
                        _check_msg_win(ev)
                elif rtype == "provider_error":
                    saw_provider_error = True
                elif rtype == "harness_recovery_advance_error":
                    saw_exception_event = True
                elif rtype == "outcome":
                    recorded = rec
                    # Outcome rows often include the final wave of events
                    # (the game-over flush) in `last_events`. Scan those
                    # too — for some traces this is the only place
                    # MSG_WIN is captured.
                    for ev in rec.get("last_events", []) or []:
                        _check_msg_win(ev)

        if saw_msg_win and win_winner is not None:
            status = "win" if win_winner == perspective else "loss"
        elif last_model_turn_tool_calls == 0:
            # Voluntary surrender takes precedence over earlier errors:
            # if the model reached a clean "no tool_calls" turn at the
            # end of the trace, the run terminated by stopping, not by
            # crashing — even if mid-run provider errors occurred and
            # the runner recovered.
            status = "stopped"
        elif max_tool_calls is not None and tool_calls_count >= max_tool_calls:
            status = "exhausted"
        elif saw_provider_error or saw_exception_event:
            # An error row appeared and the trace ended without a clean
            # surrender → the error itself was terminal.
            status = "crashed"
        else:
            status = "no_outcome"
    except OSError:
        status = "no_outcome"

    out: dict[str, Any] = {
        "status": status,
        "winner": win_winner,
        "win_reason": win_reason,
        "tool_calls": tool_calls_count,
        "perspective": perspective,
        "usage_totals": dict(usage_totals),
    }
    if recorded is not None:
        out["recorded_termination"] = recorded.get("termination")
        out["recorded_winner"] = recorded.get("winner")
        out["recorded_tool_calls"] = recorded.get("tool_calls_used")
        # Pull token usage from the outcome row if model_turn cumulative
        # records were absent (e.g. older trace formats).
        if not out["usage_totals"]:
            mut = recorded.get("model_usage_totals") or {}
            if isinstance(mut, dict):
                out["usage_totals"] = {k: v for k, v in mut.items() if isinstance(v, (int, float))}
        out["recorded_status"] = _classify_recorded(
            recorded.get("termination"),
            recorded.get("winner"),
            perspective,
        )
    return out


def collect_outcomes(
    runs_paths: list[Path],
    dataset_meta: dict[str, dict],
) -> list[PuzzleOutcome]:
    """Walk runs directories and produce one PuzzleOutcome per puzzle JSONL.

    Each outcome is independently derived from the raw event stream
    (see ``derive_from_trace``); the recorded ``outcome`` row, when
    present, is captured for cross-validation but never used as the
    source of truth.
    """
    outcomes: list[PuzzleOutcome] = []
    for run_path in runs_paths:
        if not run_path.is_dir():
            continue
        for jsonl_path in sorted(run_path.glob("*.jsonl")):
            if jsonl_path.name.startswith("_"):
                continue
            iid = jsonl_path.stem
            derived = derive_from_trace(jsonl_path)
            md = dataset_meta.get(iid, {})
            source = md.get("source")
            po = PuzzleOutcome(
                instance_id=iid,
                run_name=run_path.name,
                jsonl_path=jsonl_path,
                derived_status=derived["status"],
                derived_winner=derived["winner"],
                derived_win_reason=derived["win_reason"],
                derived_tool_calls=derived["tool_calls"],
                perspective=derived["perspective"],
                recorded_termination=derived.get("recorded_termination"),
                recorded_winner=derived.get("recorded_winner"),
                recorded_tool_calls=derived.get("recorded_tool_calls"),
                recorded_status=derived.get("recorded_status"),
                usage_totals=derived["usage_totals"],
                source=source,
                source_kind=_classify_source_kind(source),
                complexity_tier=_parse_complexity(md.get("complexity")),
                objective=md.get("objective"),
                has_gold_solution=md.get("has_gold_solution"),
            )
            outcomes.append(po)
    return outcomes


# ---------------------------------------------------------------------------
# Aggregations
# ---------------------------------------------------------------------------
def headline_counts(outcomes: list[PuzzleOutcome]) -> dict[str, Any]:
    n = len(outcomes)
    statuses = Counter(o.status for o in outcomes)
    wins = statuses.get("win", 0)
    return {
        "n_puzzles": n,
        "wins": wins,
        "win_rate": (wins / n) if n else 0.0,
        "by_status": dict(statuses),
    }


def by_termination(outcomes: list[PuzzleOutcome]) -> dict[str, int]:
    return dict(Counter(o.termination or "no_outcome" for o in outcomes))


def by_complexity(outcomes: list[PuzzleOutcome]) -> list[dict]:
    """Per-tier breakdown for tiers 1..10. Tiers with no puzzles are skipped."""
    bins: dict[int | None, list[PuzzleOutcome]] = defaultdict(list)
    for o in outcomes:
        bins[o.complexity_tier].append(o)
    rows = []
    for tier in sorted(bins.keys(), key=lambda t: (t is None, t)):
        bucket = bins[tier]
        wins = sum(1 for o in bucket if o.status == "win")
        rows.append(
            {
                "tier": tier if tier is not None else "unknown",
                "n": len(bucket),
                "wins": wins,
                "win_rate": wins / len(bucket) if bucket else 0.0,
            }
        )
    return rows


def by_source(outcomes: list[PuzzleOutcome]) -> dict[str, Any]:
    """Per-source breakdown plus an official-vs-community partition.

    Returns::

        {
            "by_source": [{"source": ..., "n": ..., "wins": ..., "win_rate": ...}, ...],
            "by_kind":   [{"kind": "official"|"community"|"other", ...}, ...],
        }
    """
    by_src: dict[str, list[PuzzleOutcome]] = defaultdict(list)
    by_kind: dict[str, list[PuzzleOutcome]] = defaultdict(list)
    for o in outcomes:
        by_src[o.source or "Unknown"].append(o)
        by_kind[o.source_kind or "other"].append(o)

    src_rows = []
    for src in sorted(by_src.keys(), key=lambda s: -len(by_src[s])):
        bucket = by_src[src]
        wins = sum(1 for o in bucket if o.status == "win")
        src_rows.append(
            {
                "source": src,
                "kind": _classify_source_kind(src),
                "n": len(bucket),
                "wins": wins,
                "win_rate": wins / len(bucket) if bucket else 0.0,
            }
        )

    kind_rows = []
    for kind in ("official", "community", "other"):
        bucket = by_kind.get(kind, [])
        if not bucket:
            continue
        wins = sum(1 for o in bucket if o.status == "win")
        kind_rows.append(
            {
                "kind": kind,
                "n": len(bucket),
                "wins": wins,
                "win_rate": wins / len(bucket) if bucket else 0.0,
            }
        )
    return {"by_source": src_rows, "by_kind": kind_rows}


def usage_rollup(outcomes: list[PuzzleOutcome]) -> dict[str, int]:
    """Sum token-usage fields across all outcomes that recorded them."""
    totals: Counter = Counter()
    for o in outcomes:
        for k, v in (o.usage_totals or {}).items():
            if isinstance(v, (int, float)):
                totals[k] += v
    return dict(totals)


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------
def _fmt_pct(x: float) -> str:
    return f"{x * 100:.1f}%"


def _ascii_table(headers: list[str], rows: list[list[str]], align: list[str] | None = None) -> str:
    """Pretty-print an ASCII table; align is per-column 'l' or 'r'."""
    if align is None:
        align = ["l"] * len(headers)
    cols = list(zip(headers, *rows, strict=False))
    widths = [max(len(str(c)) for c in col) for col in cols]
    sep = "  "

    def fmt_row(cells: list[str]) -> str:
        out = []
        for i, c in enumerate(cells):
            w = widths[i]
            out.append(str(c).rjust(w) if align[i] == "r" else str(c).ljust(w))
        return sep.join(out).rstrip()

    out = [fmt_row(headers)]
    out.append(sep.join("-" * w for w in widths))
    out.extend(fmt_row(r) for r in rows)
    return "\n".join(out)


def render_text(report: dict) -> str:
    buf = io.StringIO()
    h = report["headline"]
    buf.write("yugi-bench results aggregate\n")
    buf.write(f"  runs scanned : {', '.join(report['runs'])}\n")
    buf.write(f"  puzzles       : {h['n_puzzles']}\n")
    buf.write(f"  wins          : {h['wins']} ({_fmt_pct(h['win_rate'])})\n")
    buf.write("\n")

    buf.write("by status\n")
    rows = sorted(h["by_status"].items(), key=lambda kv: -kv[1])
    buf.write(
        _ascii_table(
            ["status", "n"],
            [[k, str(v)] for k, v in rows],
            align=["l", "r"],
        )
    )
    buf.write("\n\n")

    buf.write("by termination\n")
    rows = sorted(report["by_termination"].items(), key=lambda kv: -kv[1])
    buf.write(
        _ascii_table(
            ["termination", "n"],
            [[k, str(v)] for k, v in rows],
            align=["l", "r"],
        )
    )
    buf.write("\n\n")

    buf.write("by complexity tier\n")
    rows = [
        [str(r["tier"]), str(r["n"]), str(r["wins"]), _fmt_pct(r["win_rate"])]
        for r in report["by_complexity"]
    ]
    buf.write(
        _ascii_table(
            ["tier", "n", "wins", "rate"],
            rows,
            align=["l", "r", "r", "r"],
        )
    )
    buf.write("\n\n")

    by_kind = report["by_source"]["by_kind"]
    if by_kind:
        buf.write("by source kind\n")
        rows = [[r["kind"], str(r["n"]), str(r["wins"]), _fmt_pct(r["win_rate"])] for r in by_kind]
        buf.write(
            _ascii_table(
                ["kind", "n", "wins", "rate"],
                rows,
                align=["l", "r", "r", "r"],
            )
        )
        buf.write("\n\n")

    by_src = report["by_source"]["by_source"]
    if by_src:
        buf.write("by source\n")
        rows = [
            [r["source"], r["kind"], str(r["n"]), str(r["wins"]), _fmt_pct(r["win_rate"])]
            for r in by_src
        ]
        buf.write(
            _ascii_table(
                ["source", "kind", "n", "wins", "rate"],
                rows,
                align=["l", "l", "r", "r", "r"],
            )
        )
        buf.write("\n\n")

    usage = report.get("usage", {})
    if usage:
        buf.write("token usage (sum across all outcomes that recorded it)\n")
        rows = sorted(usage.items(), key=lambda kv: -kv[1])
        buf.write(
            _ascii_table(
                ["field", "tokens"],
                [[k, f"{v:,}"] for k, v in rows],
                align=["l", "r"],
            )
        )
        buf.write("\n\n")

    if report.get("per_puzzle"):
        buf.write("per puzzle\n")
        rows = []
        for o in report["per_puzzle"]:
            rows.append(
                [
                    o["instance_id"],
                    o["run_name"],
                    o["status"],
                    str(o.get("complexity_tier", "?")),
                    o.get("source", "?") or "?",
                    str(o.get("tool_calls_used", "-")),
                ]
            )
        buf.write(
            _ascii_table(
                ["puzzle", "run", "status", "tier", "source", "tool_calls"],
                rows,
                align=["l", "l", "l", "r", "l", "r"],
            )
        )
        buf.write("\n")

    return buf.getvalue()


def render_md(report: dict) -> str:
    buf = io.StringIO()
    h = report["headline"]
    buf.write("# yugi-bench results aggregate\n\n")
    buf.write(f"- Runs scanned: {', '.join(report['runs'])}\n")
    buf.write(f"- Puzzles: **{h['n_puzzles']}**\n")
    buf.write(f"- Wins: **{h['wins']} ({_fmt_pct(h['win_rate'])})**\n\n")

    buf.write("## By status\n\n")
    buf.write("| status | n |\n|---|---:|\n")
    for k, v in sorted(h["by_status"].items(), key=lambda kv: -kv[1]):
        buf.write(f"| {k} | {v} |\n")
    buf.write("\n")

    buf.write("## By termination\n\n")
    buf.write("| termination | n |\n|---|---:|\n")
    for k, v in sorted(report["by_termination"].items(), key=lambda kv: -kv[1]):
        buf.write(f"| {k} | {v} |\n")
    buf.write("\n")

    buf.write("## By complexity tier\n\n")
    buf.write("| tier | n | wins | rate |\n|---|---:|---:|---:|\n")
    for r in report["by_complexity"]:
        buf.write(f"| {r['tier']} | {r['n']} | {r['wins']} | {_fmt_pct(r['win_rate'])} |\n")
    buf.write("\n")

    if report["by_source"]["by_kind"]:
        buf.write("## By source kind\n\n")
        buf.write("| kind | n | wins | rate |\n|---|---:|---:|---:|\n")
        for r in report["by_source"]["by_kind"]:
            buf.write(f"| {r['kind']} | {r['n']} | {r['wins']} | {_fmt_pct(r['win_rate'])} |\n")
        buf.write("\n")

    if report["by_source"]["by_source"]:
        buf.write("## By source\n\n")
        buf.write("| source | kind | n | wins | rate |\n|---|---|---:|---:|---:|\n")
        for r in report["by_source"]["by_source"]:
            buf.write(
                f"| {r['source']} | {r['kind']} | {r['n']} | "
                f"{r['wins']} | {_fmt_pct(r['win_rate'])} |\n"
            )
        buf.write("\n")

    if report.get("usage"):
        buf.write("## Token usage (sum)\n\n")
        buf.write("| field | tokens |\n|---|---:|\n")
        for k, v in sorted(report["usage"].items(), key=lambda kv: -kv[1]):
            buf.write(f"| {k} | {v:,} |\n")
        buf.write("\n")

    if report.get("per_puzzle"):
        buf.write("## Per puzzle\n\n")
        buf.write("| puzzle | run | status | tier | source | tool_calls |\n")
        buf.write("|---|---|---|---:|---|---:|\n")
        for o in report["per_puzzle"]:
            buf.write(
                f"| `{o['instance_id']}` | {o['run_name']} | {o['status']} | "
                f"{o.get('complexity_tier', '?')} | "
                f"{o.get('source', '?') or '?'} | "
                f"{o.get('tool_calls_used', '-')} |\n"
            )

    return buf.getvalue()


def render_json(report: dict) -> str:
    return json.dumps(report, indent=2, default=str)


def render_csv(report: dict) -> str:
    """CSV export of the per-puzzle table (the most useful CSV view)."""
    if not report.get("per_puzzle"):
        return ""
    fields = [
        "instance_id",
        "run_name",
        "status",
        "termination",
        "winner",
        "complexity_tier",
        "source",
        "source_kind",
        "tool_calls_used",
        "objective",
        "has_gold_solution",
    ]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields, extrasaction="ignore")
    w.writeheader()
    for o in report["per_puzzle"]:
        w.writerow(o)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_report(
    outcomes: list[PuzzleOutcome],
    runs: list[str],
    *,
    per_puzzle: bool,
) -> dict:
    report = {
        "runs": runs,
        "headline": headline_counts(outcomes),
        "by_termination": by_termination(outcomes),
        "by_complexity": by_complexity(outcomes),
        "by_source": by_source(outcomes),
        "usage": usage_rollup(outcomes),
    }
    if per_puzzle:
        report["per_puzzle"] = [
            {
                "instance_id": o.instance_id,
                "run_name": o.run_name,
                "status": o.status,
                "termination": o.termination,
                "winner": o.winner,
                "complexity_tier": o.complexity_tier,
                "source": o.source,
                "source_kind": o.source_kind,
                "tool_calls_used": o.tool_calls_used,
                "objective": o.objective,
                "has_gold_solution": o.has_gold_solution,
            }
            for o in sorted(outcomes, key=lambda x: (x.run_name, x.instance_id))
        ]
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Aggregate yugi-bench runner results into headline tables.",
    )
    ap.add_argument(
        "runs",
        nargs="+",
        type=Path,
        help="One or more results/<run-name> directories. Globs are expanded by the shell.",
    )
    ap.add_argument(
        "--dataset",
        type=Path,
        default=DEFAULT_DATASET,
        help=f"Path to the benchmark dataset JSONL (default: {DEFAULT_DATASET}).",
    )
    ap.add_argument(
        "--format",
        choices=["text", "json", "csv", "md"],
        default="text",
        help="Output format (default: text).",
    )
    ap.add_argument(
        "--per-puzzle",
        action="store_true",
        help="Include the per-puzzle table in the output.",
    )
    ap.add_argument(
        "--strict",
        action="store_true",
        help="Cross-validate the derived classification against any "
        "recorded `outcome` row in each trace. Mismatches are "
        "printed to stderr and the exit code is 1 if any are found.",
    )
    ap.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Write to this file instead of stdout.",
    )
    args = ap.parse_args(argv)

    if not args.dataset.exists():
        print(f"ERROR: dataset not found at {args.dataset}", file=sys.stderr)
        return 2

    runs_dirs = []
    for r in args.runs:
        if not r.exists():
            print(f"WARNING: skipping missing path {r}", file=sys.stderr)
            continue
        if r.is_dir():
            runs_dirs.append(r)
        else:
            print(f"WARNING: not a directory: {r}", file=sys.stderr)
    if not runs_dirs:
        print("ERROR: no valid runs directories provided", file=sys.stderr)
        return 2

    dataset_meta = load_dataset_metadata(args.dataset)
    outcomes = collect_outcomes(runs_dirs, dataset_meta)
    if not outcomes:
        print(
            f"WARNING: no per-puzzle JSONL files found under {[str(r) for r in runs_dirs]}",
            file=sys.stderr,
        )

    # CSV format implies per-puzzle.
    per_puzzle = args.per_puzzle or args.format == "csv"
    report = build_report(outcomes, runs=[str(r) for r in runs_dirs], per_puzzle=per_puzzle)

    renderers = {
        "text": render_text,
        "json": render_json,
        "md": render_md,
        "csv": render_csv,
    }
    out = renderers[args.format](report)

    if args.output:
        args.output.write_text(out)
    else:
        sys.stdout.write(out)

    if args.strict:
        mismatches = [o for o in outcomes if o.has_recorded_mismatch]
        if mismatches:
            print(
                f"\n--strict: {len(mismatches)} recorded-vs-derived mismatch(es):", file=sys.stderr
            )
            for o in mismatches:
                print(
                    f"  {o.instance_id} ({o.run_name}): "
                    f"recorded={o.recorded_status} "
                    f"(winner={o.recorded_winner}) "
                    f"-> derived={o.derived_status} "
                    f"(winner={o.derived_winner})",
                    file=sys.stderr,
                )
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
