"""Run a yugi-bench sweep against the containerised MCP environment.

Architecture
============

For each puzzle, this driver spawns one ``docker run -i --rm --network none
... yugi-bench-env --puzzle <id>`` instance and connects to it as an MCP
client over stdio. N puzzles run in parallel, gated by ``--concurrency``.
Per-puzzle JSONLs land under the bind-mounted results directory the same
way the API-driven sweep produces them.

The container itself has no network. The agent (this driver, or any other
MCP client) lives outside.

Modes
=====

``--mode replay``
    For each puzzle that has a ``solutions/<id>.json``, dispatch each
    recorded tool call into the container and verify the engine reaches
    ``game_over`` with ``winner=0``. This is the regression test: confirms
    the containerised pipeline produces the same wins as the existing
    89 verified solutions.

``--mode stub``
    For each puzzle, call ``get_briefing`` + ``get_state`` +
    ``pending_decision``, then disconnect. The container exits cleanly
    via the agent-disconnected outcome path. Fast smoke across many
    puzzles to catch image-build regressions.

Default mode is ``replay``. Both modes are deterministic — they don't
call any external LLM.

Output
======

Per-puzzle JSONLs land at ``<results_root>/<puzzle_id>.jsonl``. After all
runs complete, the driver writes ``<results_root>/_summary.json`` with
the aggregate counts, matching the existing API-sweep summary shape.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

# Allow `python api-eval/run_sweep_containerised.py` from repo root.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))


# ---------------------------------------------------------------------------
# Per-puzzle result record
# ---------------------------------------------------------------------------


@dataclass
class PuzzleResult:
    puzzle_id: str
    mode: str
    started_at: float
    finished_at: float = 0.0
    container_exit_code: int | None = None
    outcome: dict | None = None
    error: str | None = None

    @property
    def wallclock_seconds(self) -> float:
        return round((self.finished_at or time.time()) - self.started_at, 3)

    @property
    def winner(self) -> int | None:
        return (self.outcome or {}).get("winner")

    @property
    def termination(self) -> str | None:
        return (self.outcome or {}).get("termination")


# ---------------------------------------------------------------------------
# Puzzle list resolution
# ---------------------------------------------------------------------------


def _load_dataset(dataset_path: Path) -> list[dict]:
    out = []
    with open(dataset_path) as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            out.append(json.loads(line))
    return out


def _excluded_puzzles() -> set[str]:
    """The 6 puzzles disqualified by the libocgcore SELECT_SUM bug."""
    try:
        from dataset.build_benchmark import EXCLUDED_PUZZLES

        return set(EXCLUDED_PUZZLES)
    except Exception:
        return set()


def _pick_puzzles(args, dataset: list[dict]) -> list[str]:
    if args.puzzles:
        ids = [p.strip() for p in args.puzzles.split(",") if p.strip()]
        return ids
    excluded = _excluded_puzzles()
    pool: list[str] = []
    for inst in dataset:
        pid = inst.get("instance_id")
        if not pid or pid in excluded:
            continue
        if args.mode == "replay":
            sol = _REPO_ROOT / "solutions" / f"{pid}.json"
            if not sol.exists():
                continue
        pool.append(pid)
    if args.limit and args.limit > 0:
        pool = pool[: args.limit]
    return pool


# ---------------------------------------------------------------------------
# MCP client driving — one container, one puzzle
# ---------------------------------------------------------------------------


def _docker_args(args, puzzle_id: str) -> list[str]:
    """Build the docker run command for one puzzle.

    Bind-mounts ``args.results_root`` into ``/work/results`` so the
    container's per-puzzle JSONL lands on the host. Honours
    ``--docker-cmd`` (default: ``$DOCKER`` env var, falls back to
    ``docker``) so the driver works equally with podman.
    """
    docker_cmd = args.docker_cmd or os.environ.get("DOCKER") or "docker"
    cmd = [docker_cmd, "run", "-i", "--rm", "--network", "none"]
    # Rootless podman maps container uid 1000 to a host subuid by default,
    # which makes the bind-mounted results dir unwritable. --userns=keep-id
    # maps the host caller uid identically so the container's ygo user
    # (uid 1000) writes as host uid 1000. Docker's default user-namespace
    # behaviour does not have this issue, so the flag is opt-in.
    if "podman" in os.path.basename(docker_cmd):
        cmd += ["--userns=keep-id"]
    cmd += [
        "-v",
        f"{args.results_root}:/work/results",
        args.image_tag,
        "--puzzle",
        puzzle_id,
        "--max-tool-calls",
        str(args.max_tool_calls),
    ]
    # In replay mode, mirror engine.replay's auto_opponent=True default so
    # the containerised pipeline reproduces the same outcomes engine.replay
    # produces on solutions/<id>.json. In stub mode and for real-agent runs
    # the flag stays off (matches API-driven Episode behaviour).
    if args.mode == "replay":
        cmd.append("--auto-opponent")
    return cmd


async def _run_replay(session, puzzle_id: str) -> dict | None:
    """Drive the container from solutions/<puzzle_id>.json."""
    sol_path = _REPO_ROOT / "solutions" / f"{puzzle_id}.json"
    if not sol_path.exists():
        return {"error": f"no solution at {sol_path}"}
    actions = json.loads(sol_path.read_text())
    await session.call_tool("get_briefing", {})
    last_text = ""
    for step in actions:
        result = await session.call_tool(step["tool"], step.get("args") or {})
        last_text = result.content[0].text if result.content else ""
        # The tool result's text contains "_outcome" once the engine reaches
        # game_over. We don't need to parse here — the container's JSONL is
        # authoritative — but break early so we don't push extra calls into
        # a terminated server.
        if '"_outcome"' in last_text or '"is_error": true' in last_text:
            break
    return None


async def _run_stub(session, puzzle_id: str) -> dict | None:
    """Minimum smoke: briefing + a couple of inspection tools."""
    await session.call_tool("get_briefing", {})
    await session.call_tool("get_state", {})
    await session.call_tool("pending_decision", {})
    return None


async def _run_one_puzzle(args, puzzle_id: str, sem: asyncio.Semaphore) -> PuzzleResult:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    res = PuzzleResult(puzzle_id=puzzle_id, mode=args.mode, started_at=time.time())
    docker_cmd = _docker_args(args, puzzle_id)
    server_params = StdioServerParameters(
        command=docker_cmd[0],
        args=docker_cmd[1:],
    )
    async with sem:
        try:
            async with stdio_client(server_params) as (r, w):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    if args.mode == "replay":
                        err = await _run_replay(session, puzzle_id)
                    elif args.mode == "stub":
                        err = await _run_stub(session, puzzle_id)
                    else:
                        err = {"error": f"unknown mode: {args.mode!r}"}
                    if err and "error" in err:
                        res.error = err["error"]
        except Exception as e:  # noqa: BLE001
            res.error = f"{type(e).__name__}: {e}"
        finally:
            res.finished_at = time.time()
    # Read the container's outcome from the per-puzzle JSONL.
    try:
        out_path = Path(args.results_root) / f"{puzzle_id}.jsonl"
        if out_path.exists():
            for line in out_path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                d = json.loads(line)
                if d.get("type") == "outcome":
                    res.outcome = d
    except Exception as e:  # noqa: BLE001
        if not res.error:
            res.error = f"jsonl-read: {type(e).__name__}: {e}"
    return res


# ---------------------------------------------------------------------------
# Sweep entrypoint
# ---------------------------------------------------------------------------


async def _sweep(args) -> int:
    dataset = _load_dataset(_REPO_ROOT / "data" / "yugioh_bench.jsonl")
    puzzle_ids = _pick_puzzles(args, dataset)
    if not puzzle_ids:
        print(
            "no puzzles to run (check --puzzles / --limit / --mode replay "
            "requires solutions/<id>.json)",
            file=sys.stderr,
        )
        return 2

    Path(args.results_root).mkdir(parents=True, exist_ok=True)
    sem = asyncio.Semaphore(args.concurrency)

    print(
        f"[sweep] mode={args.mode} concurrency={args.concurrency} "
        f"puzzles={len(puzzle_ids)} image={args.image_tag}",
        file=sys.stderr,
    )
    started = time.time()
    tasks = [asyncio.create_task(_run_one_puzzle(args, pid, sem)) for pid in puzzle_ids]
    results: list[PuzzleResult] = []
    for i, fut in enumerate(asyncio.as_completed(tasks), start=1):
        res = await fut
        results.append(res)
        status = (
            "WIN"
            if res.winner == 0
            else f"LOSS(winner={res.winner})"
            if res.termination == "game_over"
            else res.termination or f"ERR({res.error})"
            if res.error
            else "?"
        )
        elapsed = res.wallclock_seconds
        print(f"[{i}/{len(puzzle_ids)}] {res.puzzle_id} {status} ({elapsed}s)", file=sys.stderr)

    summary = _summarise(results, started)
    out = Path(args.results_root) / "_summary.json"
    out.write_text(json.dumps(summary, indent=2, default=str))
    print(f"[sweep] wrote {out}", file=sys.stderr)
    print(f"[sweep] {summary['counts']}", file=sys.stderr)
    return 0 if summary["counts"].get("error", 0) == 0 else 1


def _summarise(results: list[PuzzleResult], started: float) -> dict:
    counts: Counter = Counter()
    for r in results:
        if r.error:
            counts["error"] += 1
        elif r.winner == 0:
            counts["win"] += 1
        elif r.termination == "game_over":
            counts["loss"] += 1
        else:
            counts[r.termination or "incomplete"] += 1
    return {
        "started_at": started,
        "finished_at": time.time(),
        "wallclock_seconds": round(time.time() - started, 3),
        "n_puzzles": len(results),
        "counts": dict(counts),
        "results": [asdict(r) for r in results],
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="run_sweep_containerised",
        description=__doc__.splitlines()[0],
    )
    p.add_argument(
        "--mode",
        choices=["replay", "stub"],
        default="replay",
        help="replay: drive from solutions/<id>.json (regression). "
        "stub: minimal MCP calls per puzzle (smoke).",
    )
    p.add_argument(
        "--puzzles",
        default=None,
        help="Comma-separated puzzle_id list. "
        "Default = the full 217-puzzle dataset "
        "(intersected with solutions/ in --mode replay).",
    )
    p.add_argument("--limit", type=int, default=0, help="Cap the puzzle count (0 = no cap).")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--image-tag", default="yugi-bench-env:latest")
    p.add_argument(
        "--results-root",
        required=True,
        help="Host directory bind-mounted to /work/results in "
        "each container. Per-puzzle JSONLs land here.",
    )
    p.add_argument("--max-tool-calls", type=int, default=500)
    p.add_argument(
        "--docker-cmd",
        default=None,
        help="Container runtime binary (default: $DOCKER env var, "
        "else 'docker'). Set to 'podman' for rootless.",
    )
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_sweep(args))


if __name__ == "__main__":
    raise SystemExit(main())
