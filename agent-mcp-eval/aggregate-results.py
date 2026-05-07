#!/usr/bin/env python3
"""Aggregate per-session results from a host-install batch run.

Walks ``~/yugi-bench-runs/`` (or any directory passed via ``--runs-root``),
reads each session's ``metadata.json`` and ``results/<puzzle_id>.jsonl``,
extracts the outcome, and produces:

  - ``<runs-root>/_summary.json``    — full structured aggregate
  - ``<runs-root>/_summary.csv``     — one row per session, spreadsheet-friendly
  - ``<runs-root>/_summary.md``      — Markdown summary table for review
  - ``<runs-root>/_incomplete.txt``  — list:ID,ID,... of every puzzle that
                                        didn't reach a clean win/loss; paste
                                        into prep-batch.sh --strategy
                                        list:<...> to regenerate just those
  - stdout — top-line counts (W/L/incomplete/error) + the list:... line

Sessions are classified as:

  - ``win``                — outcome.winner == 0 (player won)
  - ``loss``               — outcome.winner == 1 (opponent won, engine
                             reached a clean game_over)
  - ``incomplete``         — outcome.termination is e.g.
                             ``agent_disconnected`` or
                             ``tool_budget_exhausted`` — engine did not
                             reach game_over before the agent stopped
  - ``no_jsonl``           — workspace exists but ``results/*.jsonl`` is
                             missing (container failed to start, or run
                             never produced output)
  - ``no_outcome``         — JSONL exists but never wrote an ``outcome``
                             event (process killed mid-run)

Anything not in ``{win, loss}`` lands in the regenerate-list — those are
the puzzles where you don't yet have a valid evaluation, regardless of
whether the engine, the agent, or the harness was the one that bailed.

Usage:
  ./agent-mcp-eval/aggregate-results.py
  ./agent-mcp-eval/aggregate-results.py --runs-root /custom/path
  ./agent-mcp-eval/aggregate-results.py --since 2026-05-04T00-00-00
"""

from __future__ import annotations

import sys
from pathlib import Path as _Path

_REPO_ROOT = _Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))


import argparse
import csv
import json
import os
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class SessionResult:
    workspace: str
    puzzle_id: str
    started_at: str
    agent: str | None = None  # 'claude' / 'codex' / None for legacy workspaces
    ended_at: str | None = None
    status: str = "unknown"
    winner: int | None = None
    termination: str | None = None
    tool_calls_used: int | None = None
    auto_decline_count: int | None = None
    wallclock_seconds: float | None = None
    final_lp: list[int] | None = None
    image: str | None = None
    auto_opponent: bool | None = None
    max_tool_calls: int | None = None
    jsonl_path: str | None = None
    jsonl_event_count: int | None = None
    error: str | None = None


def _classify(outcome: dict | None) -> str:
    if outcome is None:
        return "no_outcome"
    if outcome.get("termination") == "game_over":
        return "win" if outcome.get("winner") == 0 else "loss"
    return "incomplete"


def _scan_workspace(workspace: Path) -> SessionResult | None:
    meta_path = workspace / "metadata.json"
    if not meta_path.exists():
        # Not a yugi-bench session directory; skip silently.
        return None
    try:
        meta = json.loads(meta_path.read_text())
    except Exception as e:
        return SessionResult(
            workspace=str(workspace),
            puzzle_id=workspace.name,
            started_at="",
            status="error",
            error=f"metadata.json parse: {type(e).__name__}: {e}",
        )

    res = SessionResult(
        workspace=str(workspace),
        puzzle_id=meta.get("puzzle_id", workspace.name),
        started_at=meta.get("started_at", ""),
        agent=meta.get("agent"),
        image=meta.get("image"),
        auto_opponent=meta.get("auto_opponent"),
        max_tool_calls=meta.get("max_tool_calls"),
    )

    jsonl_path = workspace / "results" / f"{res.puzzle_id}.jsonl"
    if not jsonl_path.exists():
        res.status = "no_jsonl"
        return res
    res.jsonl_path = str(jsonl_path)

    outcome: dict | None = None
    event_count = 0
    last_event: dict | None = None
    try:
        with open(jsonl_path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                event_count += 1
                d = json.loads(line)
                last_event = d
                if d.get("type") == "outcome":
                    outcome = d
    except Exception as e:
        res.status = "error"
        res.error = f"jsonl parse: {type(e).__name__}: {e}"
        return res
    res.jsonl_event_count = event_count

    if outcome is None:
        res.status = "no_outcome"
        # Surface useful diagnostic info from the last event.
        if last_event is not None:
            res.error = f"last event type={last_event.get('type')!r}"
        return res

    res.status = _classify(outcome)
    res.winner = outcome.get("winner")
    res.termination = outcome.get("termination")
    res.tool_calls_used = outcome.get("tool_calls_used")
    res.auto_decline_count = outcome.get("auto_decline_count")
    res.wallclock_seconds = outcome.get("wallclock_seconds")
    res.final_lp = outcome.get("lp")
    # ended_at is approximate — use file mtime since we don't log it.
    try:
        res.ended_at = jsonl_path.stat().st_mtime.__str__()
    except Exception:
        pass
    return res


def _aggregate(runs_root: Path, since: str | None) -> list[SessionResult]:
    if not runs_root.is_dir():
        print(f"[aggregate] runs-root does not exist: {runs_root}", file=sys.stderr)
        sys.exit(2)
    results: list[SessionResult] = []
    for entry in sorted(runs_root.iterdir()):
        if not entry.is_dir():
            continue
        if since and entry.name < f"yugioh_puzzle_zzz-{since}":
            # Crude lexicographic filter: workspace names embed an
            # ISO timestamp suffix, so since="2026-05-04T00-00-00"
            # filters cleanly when the puzzle_id format is uniform.
            ts_suffix = entry.name.rsplit("-", 1)[-1] if "-" in entry.name else ""
            if ts_suffix < since:
                continue
        res = _scan_workspace(entry)
        if res is not None:
            results.append(res)
    return results


def _write_csv(out: Path, results: list[SessionResult]) -> None:
    fields = [
        "puzzle_id",
        "agent",
        "started_at",
        "status",
        "winner",
        "termination",
        "tool_calls_used",
        "auto_decline_count",
        "wallclock_seconds",
        "final_lp",
        "image",
        "auto_opponent",
        "max_tool_calls",
        "jsonl_event_count",
        "workspace",
        "error",
    ]
    with open(out, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for r in results:
            row = {f: getattr(r, f) for f in fields}
            if isinstance(row["final_lp"], list):
                row["final_lp"] = ",".join(str(x) for x in row["final_lp"])
            w.writerow(row)


def _write_md(out: Path, results: list[SessionResult], counts: Counter) -> None:
    lines = [
        "# yugi-bench host-install batch summary",
        "",
        f"Sessions scanned: **{len(results)}**",
        "",
        "| Status | Count |",
        "|---|---:|",
    ]
    for k in ("win", "loss", "incomplete", "no_outcome", "no_jsonl", "error"):
        if counts.get(k):
            lines.append(f"| {k} | {counts[k]} |")
    lines += ["", "## Per-session detail", ""]
    lines += [
        "| Puzzle | Status | Winner | Termination | Tool calls | Wallclock (s) | LP | Started |",
        "|---|---|---:|---|---:|---:|---|---|",
    ]
    for r in sorted(results, key=lambda x: (x.status, x.puzzle_id)):
        lp_str = "/".join(str(x) for x in r.final_lp) if r.final_lp else "-"
        lines.append(
            f"| `{r.puzzle_id}` | {r.status} | "
            f"{r.winner if r.winner is not None else '-'} | "
            f"{r.termination or '-'} | "
            f"{r.tool_calls_used or '-'} | "
            f"{r.wallclock_seconds or '-'} | "
            f"{lp_str} | {r.started_at} |"
        )
    out.write_text("\n".join(lines) + "\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument(
        "--runs-root",
        default=None,
        help="Root directory of session workspaces. "
        "Defaults to $YUGI_RUNS_ROOT or ~/yugi-bench-runs.",
    )
    ap.add_argument(
        "--since",
        default=None,
        help="Filter to workspaces with a started_at timestamp "
        ">= this value (lexicographic, ISO 'YYYY-MM-DDTHH-MM-SS').",
    )
    args = ap.parse_args(argv)

    runs_root = (
        Path(
            args.runs_root or os.environ.get("YUGI_RUNS_ROOT") or (Path.home() / "yugi-bench-runs")
        )
        .expanduser()
        .resolve()
    )

    results = _aggregate(runs_root, args.since)
    if not results:
        print(f"[aggregate] no sessions found under {runs_root}", file=sys.stderr)
        return 1

    counts = Counter(r.status for r in results)
    by_agent: dict[str, Counter] = {}
    for r in results:
        agent_key = r.agent or "unknown"
        by_agent.setdefault(agent_key, Counter())[r.status] += 1

    summary = {
        "runs_root": str(runs_root),
        "n_sessions": len(results),
        "counts": dict(counts),
        "counts_by_agent": {k: dict(v) for k, v in by_agent.items()},
        "results": [asdict(r) for r in results],
    }
    # Build the regenerate-list — every puzzle whose status isn't a
    # clean win/loss.  Deduplicated across multiple workspaces for the
    # same puzzle (keep the first seen, since results is sorted).
    NEEDS_RERUN = {"incomplete", "no_outcome", "no_jsonl", "error"}
    rerun_ids: list[str] = []
    rerun_seen: set[str] = set()
    rerun_by_status: dict[str, list[str]] = {k: [] for k in NEEDS_RERUN}
    rerun_workspace_count = 0
    for r in results:
        if r.status not in NEEDS_RERUN:
            continue
        rerun_workspace_count += 1
        if r.puzzle_id in rerun_seen:
            continue
        rerun_seen.add(r.puzzle_id)
        rerun_ids.append(r.puzzle_id)
        rerun_by_status[r.status].append(r.puzzle_id)

    summary["regenerate_list"] = rerun_ids
    summary["regenerate_by_status"] = rerun_by_status

    json_out = runs_root / "_summary.json"
    json_out.write_text(json.dumps(summary, indent=2, default=str))
    csv_out = runs_root / "_summary.csv"
    _write_csv(csv_out, results)
    md_out = runs_root / "_summary.md"
    _write_md(md_out, results, counts)

    incomplete_out = runs_root / "_incomplete.txt"
    if rerun_ids:
        incomplete_out.write_text(
            "# Puzzles that didn't reach a clean win/loss — paste the\n"
            "# list:... line below into prep-batch.sh to regenerate.\n"
            "#\n"
            f"# {len(rerun_ids)} unique puzzle id(s) across "
            f"{rerun_workspace_count} workspace(s).\n"
            "# Per-status breakdown (unique puzzle ids):\n"
            + "".join(
                f"#   {st:12s} {len(rerun_by_status[st]):3d}\n"
                for st in sorted(NEEDS_RERUN)
                if rerun_by_status[st]
            )
            + "\n"
            f"list:{','.join(rerun_ids)}\n"
        )
    elif incomplete_out.exists():
        incomplete_out.unlink()  # tidy up if everything is now win/loss

    # Stdout summary
    print(f"yugi-bench host-install batch summary  ({runs_root})")
    print(f"  sessions scanned: {len(results)}")
    for k in ("win", "loss", "incomplete", "no_outcome", "no_jsonl", "error"):
        if counts.get(k):
            print(f"  {k:14s} {counts[k]}")
    if len(by_agent) > 1:
        print()
        print("  by agent:")
        for agent_key in sorted(by_agent):
            ac = by_agent[agent_key]
            line = " ".join(f"{k}={v}" for k, v in ac.items())
            print(f"    {agent_key:10s} {line}")
    if rerun_ids:
        print()
        print(f"  needs-rerun: {len(rerun_ids)} unique puzzle id(s) (see {incomplete_out})")
        for st in sorted(NEEDS_RERUN):
            if rerun_by_status[st]:
                print(f"    {st:12s} {len(rerun_by_status[st])}")
        print()
        print("  paste into prep-batch.sh --strategy:")
        print(f"    list:{','.join(rerun_ids)}")
    print()
    print(f"  wrote {json_out}")
    print(f"  wrote {csv_out}")
    print(f"  wrote {md_out}")
    if rerun_ids:
        print(f"  wrote {incomplete_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
