"""Shared run-batch implementation for codex/run-batch.py and claude/run-batch.py.

Each per-agent entry point is a thin import + ``main(agent_name)`` call.
Workspaces are filtered to the named agent by reading
``metadata.json``'s ``agent`` field.

**Idempotent + mixed-mode safe** — re-running after some manual
sessions produces the same final state as running the script alone.
The "done" detector is the presence of an ``outcome`` event in
``results/<puzzle_id>.jsonl``, agent-agnostic and identical across
manual paste and auto-driven exec sessions.

**Rate-limit aware**.  Two layers of protection so an unattended batch
doesn't burn through every pending workspace as failures while the
agent's OAuth window is capped:

1. **Pre-flight probe** before each session.  Hits the agent's own
   usage endpoint (``chatgpt.com/backend-api/codex/usage`` for codex,
   ``api.anthropic.com/api/oauth/usage`` for claude).  If either the
   5-hour or 7-day window utilization is above its stop-threshold,
   pauses until the window resets (capped by
   ``--rate-limit-pause-seconds``).
2. **Post-flight pattern detection**.  Scans the launcher's combined
   output for known rate-limit error strings (rate-limit, 429, quota
   exceeded, etc.).  On a match, pauses and retries the SAME workspace.
   After ``--max-rate-limit-retries`` consecutive matches without a
   successful session in between, exits loud (likely the 7-day cap is
   hit and human attention is warranted).

**Concurrency** (``--concurrency N``) runs N sessions in parallel via
a thread pool.  Each session still gets its own per-puzzle MCP
container (``--rm`` cleans up on exit).  Pre-flight pauses apply
globally — when one worker observes a rate-limit hit, all workers
sleep until the window resets.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from concurrent.futures import CancelledError, ThreadPoolExecutor, as_completed

DEFAULT_RUNS_ROOT = os.environ.get("YUGI_RUNS_ROOT") or str(
    Path.home() / "yugi-bench-runs"
)

ANTHROPIC_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
ANTHROPIC_OAUTH_BETA = "oauth-2025-04-20"
ANTHROPIC_CREDS_PATH = Path.home() / ".claude" / ".credentials.json"

# Codex CLI's undocumented usage endpoint (same one the codex TUI itself
# polls every ~60s for in-CLI display, and same one the
# wakamex/codex-cli-usage PyPI package wraps).  Returned shape:
# rate_limit.primary_window.used_percent (5h) +
# rate_limit.secondary_window.used_percent (7d), with reset_at as
# Unix epoch seconds.
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/codex/usage"
CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"

RATE_LIMIT_PATTERNS = [
    r"rate[\s_-]?limit",
    r"too many requests",
    r"quota[\s_-]?exceeded",
    r"\b429\b",
    r"5[\s-]?h(?:our)?\s*(?:window|cap|limit|quota)",
    r"7[\s-]?d(?:ay)?\s*(?:window|cap|limit|quota)",
    r"usage[\s_-]?limit",
    r"insufficient[\s_-]?quota",
    r"please try again later",
    r"you have been rate-limited",
]
RATE_LIMIT_RE = re.compile("|".join(RATE_LIMIT_PATTERNS), re.IGNORECASE)

# Stream-json events claude emits as INFORMATIONAL metadata that contain
# the literal substring "rate_limit" / "rateLimit" but do not indicate
# an actual rate-limit failure (status="allowed", overageStatus="ok",
# etc.).  These would falsely match the regex above; we parse them as
# JSON instead and only treat them as rate-limit hits when their fields
# show actual denial / overage.
_RATE_LIMIT_EVENT_TYPES = {"rate_limit_event"}
_RATE_LIMIT_DENY_STATUS = {"denied", "rejected", "exceeded", "blocked"}


def _to_text(x) -> str:
    """Coerce stdout/stderr to str regardless of source.  Handles the
    Python-stdlib quirk where ``subprocess.TimeoutExpired.stdout`` is
    the raw byte buffer even when the parent ``subprocess.run`` was
    called with ``text=True`` (the text-mode decode only happens on
    successful completion, not on the timeout-error exception path).
    """
    if x is None:
        return ""
    if isinstance(x, bytes):
        return x.decode("utf-8", errors="replace")
    return x


@dataclass
class WorkspaceState:
    workspace: Path
    metadata: dict
    puzzle_id: str
    agent: str
    jsonl: Path
    launcher: Path

    @property
    def has_outcome(self) -> bool:
        if not self.jsonl.exists():
            return False
        try:
            with open(self.jsonl) as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        if json.loads(line).get("type") == "outcome":
                            return True
                    except Exception:
                        continue
        except Exception:
            return False
        return False


def _load_puzzle_priority_map(repo_root: Path) -> dict[str, tuple[int, int, str]]:
    """Build a map: puzzle_id -> (verified_tier, complexity, puzzle_id)
    for use as a sort key.  verified_tier = 0 if the puzzle is in the
    Konami-gold verified subset (data/yugioh_bench_verified.jsonl),
    else 1.  complexity is the leading int of the "N/10" metadata
    field, defaulting to 99 when missing.  Tie-break on the puzzle
    id (which is the content hash, so stable across rebuilds).

    Used by discover_workspaces to order processing: verified easy
    first, then non-verified easy, hardest last in each tier.  Falls
    back gracefully (empty map) if the dataset isn't reachable —
    discover_workspaces then sorts by puzzle_id alone.
    """
    out: dict[str, tuple[int, int, str]] = {}
    dataset = repo_root / "data" / "yugioh_bench.jsonl"
    verified = repo_root / "data" / "yugioh_bench_verified.jsonl"
    if not dataset.exists():
        return out

    verified_ids: set[str] = set()
    if verified.exists():
        try:
            for line in open(verified):
                line = line.strip()
                if not line:
                    continue
                verified_ids.add(json.loads(line)["instance_id"])
        except Exception:
            pass

    try:
        for line in open(dataset):
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            pid = d.get("instance_id")
            if not pid:
                continue
            raw = (d.get("metadata") or {}).get("complexity", "")
            try:
                cx = int(str(raw).split("/")[0])
            except Exception:
                cx = 99
            tier = 0 if pid in verified_ids else 1
            out[pid] = (tier, cx, pid)
    except Exception:
        return {}
    return out


def discover_workspaces(runs_root: Path, agent: str) -> list[WorkspaceState]:
    out: list[WorkspaceState] = []
    if not runs_root.is_dir():
        return out
    # Repo root is two levels up from agent-mcp-eval/_lib/run_batch_core.py.
    repo_root = Path(__file__).resolve().parent.parent.parent
    prio = _load_puzzle_priority_map(repo_root)

    for ws in runs_root.iterdir():
        if not ws.is_dir():
            continue
        meta_path = ws / "metadata.json"
        if not meta_path.exists():
            continue
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        ws_agent = meta.get("agent")
        if ws_agent != agent:
            continue
        pid = meta.get("puzzle_id")
        if not pid:
            continue
        jsonl = ws / "results" / f"{pid}.jsonl"
        launcher = ws / f"run-{agent}-exec.sh"
        out.append(
            WorkspaceState(
                workspace=ws, metadata=meta, puzzle_id=pid,
                agent=agent, jsonl=jsonl, launcher=launcher,
            )
        )

    # Sort: verified-tier (0=verified first, 1=rest), complexity ascending,
    # puzzle_id (= content hash, stable tie-break), then workspace dir name
    # so multiple workspaces for the same puzzle keep deterministic order.
    out.sort(key=lambda w: (
        prio.get(w.puzzle_id, (2, 99, w.puzzle_id)),  # unknown puzzles last
        w.workspace.name,
    ))
    return out


# ---------------------------------------------------------------------------
# Usage-probe helpers — agent-specific endpoints, normalised output shape
# ---------------------------------------------------------------------------

def _parse_iso8601_to_dt(s: str) -> dt.datetime | None:
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        return dt.datetime.fromisoformat(s).astimezone(dt.timezone.utc)
    except Exception:
        return None


def _read_anthropic_oauth_token() -> str | None:
    if not ANTHROPIC_CREDS_PATH.exists():
        return None
    try:
        data = json.loads(ANTHROPIC_CREDS_PATH.read_text())
    except Exception:
        return None
    for path in [
        ("claudeAiOauth", "accessToken"),
        ("claude_ai_oauth", "access_token"),
        ("oauth", "access_token"),
    ]:
        cur: object = data
        ok = True
        for k in path:
            if isinstance(cur, dict) and k in cur:
                cur = cur[k]
            else:
                ok = False
                break
        if ok and isinstance(cur, str) and cur:
            return cur
    return None


def fetch_anthropic_usage() -> dict | None:
    """Probe Anthropic's /api/oauth/usage. Returns parsed dict normalised
    to {'five_hour': {utilization, resets_at}, 'seven_day': {...}} or None."""
    token = _read_anthropic_oauth_token()
    if not token:
        return None
    req = urllib.request.Request(
        ANTHROPIC_USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": ANTHROPIC_OAUTH_BETA,
            "User-Agent": "yugi-bench-run-batch/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception:
        return None


def fetch_codex_usage() -> dict | None:
    """Probe codex CLI's usage endpoint.  Returns the same normalised
    shape as fetch_anthropic_usage so usage_preflight_pause_seconds()
    can treat both agents identically."""
    if not CODEX_AUTH_PATH.exists():
        return None
    try:
        auth = json.loads(CODEX_AUTH_PATH.read_text())
    except Exception:
        return None
    tokens = auth.get("tokens") or {}
    access_token = tokens.get("access_token")
    if not access_token:
        return None
    account_id = tokens.get("account_id") or ""

    req = urllib.request.Request(
        CODEX_USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "chatgpt-account-id": account_id,
            "Accept": "application/json",
            "User-Agent": "yugi-bench-run-batch/0.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception:
        return None

    rl = data.get("rate_limit") or {}
    primary = rl.get("primary_window") or {}
    secondary = rl.get("secondary_window") or {}

    def _epoch_to_iso(epoch: object) -> str:
        try:
            ep = float(epoch)
            return dt.datetime.fromtimestamp(ep, dt.timezone.utc).isoformat()
        except Exception:
            return ""

    out: dict = {}
    if primary:
        out["five_hour"] = {
            "utilization": primary.get("used_percent"),
            "resets_at": _epoch_to_iso(primary.get("reset_at", 0)),
        }
    if secondary:
        out["seven_day"] = {
            "utilization": secondary.get("used_percent"),
            "resets_at": _epoch_to_iso(secondary.get("reset_at", 0)),
        }
    return out or None


def fetch_usage(agent: str) -> dict | None:
    if agent == "claude":
        return fetch_anthropic_usage()
    if agent == "codex":
        return fetch_codex_usage()
    return None


def usage_preflight_pause_seconds(
    usage: dict,
    five_hour_stop_used_pct: float,
    seven_day_stop_used_pct: float,
    cap_pause_seconds: int,
) -> tuple[float, str] | None:
    """If either window has crossed its stop threshold, return
    (pause_seconds, reason).  Otherwise return None."""
    now_utc = dt.datetime.now(dt.timezone.utc)
    for key, stop_pct in (
        ("five_hour", five_hour_stop_used_pct),
        ("seven_day", seven_day_stop_used_pct),
    ):
        win = usage.get(key) or {}
        util = win.get("utilization")
        if util is None:
            continue
        try:
            util = float(util)
        except (TypeError, ValueError):
            continue
        if util < stop_pct:
            continue
        resets_at = _parse_iso8601_to_dt(win.get("resets_at", "") or "")
        if resets_at is None:
            return (
                float(cap_pause_seconds),
                f"{key} util={util:.1f}% (>={stop_pct}%); "
                f"no resets_at, sleeping {cap_pause_seconds}s",
            )
        until_reset = (resets_at - now_utc).total_seconds()
        until_reset = max(60.0, until_reset)
        pause = min(until_reset, cap_pause_seconds)
        return (
            pause,
            f"{key} util={util:.1f}% (>={stop_pct}%); "
            f"resets_at={resets_at:%H:%M:%S UTC}, sleeping {pause:.0f}s",
        )
    return None


def detect_rate_limit(stdout: str, stderr: str) -> bool:
    """Detect actual rate-limit / quota indicators in launcher output.

    Two-stage check, designed to avoid false positives on claude's
    stream-json informational events:

    1. **Structural pass over stdout**: parse each JSON line.  When we
       see a ``rate_limit_event``, check its ``status`` /
       ``overageStatus`` fields explicitly — only flag when the values
       indicate actual denial.  When we see the final ``result`` event
       with ``is_error=true``, check whether the ``result`` text
       mentions rate-limiting.  Other JSON-event lines are skipped (we
       won't fall through to regex on them).

    2. **Regex fallback over non-JSON text + stderr**: catches
       human-readable rate-limit messages that don't go through a
       structured event (e.g. CLI exit-text "you've hit your limit").
       JSON event lines are stripped first so their informational
       fields can't false-match.

    Empirically: claude emits
    ``{"type":"rate_limit_event","rate_limit_info":{"status":"allowed",
    "rateLimitType":"five_hour",...}}`` on every session as metadata —
    a naive regex over the raw stream matches the field names
    ``rate_limit`` / ``rateLimit`` and triggers global pauses + retries
    on every successful run, hence the structured-pass-first design.
    """
    stdout = _to_text(stdout)
    stderr = _to_text(stderr)
    # Stage 1: structured pass on stdout JSON lines
    json_lines: set[int] = set()  # indices of lines that parsed as JSON
    for i, line in enumerate(stdout.splitlines()):
        s = line.strip()
        if not s.startswith("{"):
            continue
        try:
            ev = json.loads(s)
        except Exception:
            continue
        json_lines.add(i)
        t = ev.get("type")
        if t in _RATE_LIMIT_EVENT_TYPES:
            info = ev.get("rate_limit_info") or {}
            status = str(info.get("status", "")).lower()
            if status in _RATE_LIMIT_DENY_STATUS:
                return True
            # Note: do NOT trigger on `overageStatus` — that field describes
            # the user's overage policy ("rejected" = overage disabled by
            # org), not whether THIS request was denied.  When the org has
            # overage disabled, every rate_limit_event carries
            # overageStatus="rejected" even when status="allowed".  Only
            # the top-level `status` reflects the request outcome.
        elif t == "result" and ev.get("is_error"):
            result_text = (ev.get("result") or "").lower()
            if RATE_LIMIT_RE.search(result_text):
                return True
    # Stage 2: regex fallback on the non-JSON portion + stderr
    cleaned_lines = [
        line for i, line in enumerate(stdout.splitlines())
        if i not in json_lines
    ]
    cleaned = "\n".join(cleaned_lines) + "\n" + stderr
    return bool(RATE_LIMIT_RE.search(cleaned))


# ---------------------------------------------------------------------------
# Token-usage extraction from launcher output
# ---------------------------------------------------------------------------
# The exec launchers invoke the agent CLIs with structured output:
#   - claude: --print --output-format stream-json --verbose
#   - codex:  --json
# Both produce newline-delimited JSON events on stdout.  The final
# "result"-shaped event carries cumulative input/output token counts.
# Best-effort parser: walks all JSON lines, tracks the latest seen
# input/output-token values across known field paths/aliases.  Returns
# None when no parseable usage is found (e.g. agent CLI didn't emit
# JSON, the run errored before any usage event, or the schema changed
# beyond what this parser knows about).  Non-regressive: callers print
# the run line without tokens when this returns None.

def _find_usage_dict(obj) -> dict | None:
    """Locate a dict that looks like an LLM usage record inside an event."""
    if not isinstance(obj, dict):
        return None
    direct = obj.get("usage")
    if isinstance(direct, dict):
        return direct
    for key in ("message", "response", "result", "data"):
        sub = obj.get(key)
        if isinstance(sub, dict):
            nested = sub.get("usage")
            if isinstance(nested, dict):
                return nested
    return None


def _coalesce_input_tokens(u: dict) -> int | None:
    """Sum the input-side fields of a usage dict, accounting for the
    Anthropic-style cache_{creation,read}_input_tokens splits."""
    base_keys = ("input_tokens", "prompt_tokens", "in_tokens", "input")
    cache_keys = ("cache_creation_input_tokens", "cache_read_input_tokens")
    base: int | None = None
    for k in base_keys:
        v = u.get(k)
        if isinstance(v, (int, float)):
            base = int(v)
            break
    if base is None:
        return None
    extra = sum(
        int(u[k]) for k in cache_keys
        if isinstance(u.get(k), (int, float))
    )
    return base + extra


def _coalesce_output_tokens(u: dict) -> int | None:
    for k in ("output_tokens", "completion_tokens", "out_tokens", "output"):
        v = u.get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return None


def extract_usage_from_log(stdout: str) -> dict | None:
    """Best-effort token-usage extraction from a launcher's JSONL stdout.

    Walks every line that parses as a JSON object, finds usage-shaped
    sub-dicts, and tracks the latest seen input/output counts.  Both
    agent CLIs emit a final "result"-shaped event with cumulative
    totals, so latest-wins gives the right answer.  Returns
    ``{"in", "out", "total"}`` or ``None`` if nothing parseable found.
    """
    stdout = _to_text(stdout)
    last_in: int | None = None
    last_out: int | None = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        usage = _find_usage_dict(event)
        if usage is None:
            continue
        i = _coalesce_input_tokens(usage)
        if i is not None:
            last_in = i
        o = _coalesce_output_tokens(usage)
        if o is not None:
            last_out = o
    if last_in is None and last_out is None:
        return None
    i = last_in if last_in is not None else 0
    o = last_out if last_out is not None else 0
    return {"in": i, "out": o, "total": i + o}


# ---------------------------------------------------------------------------
# Run-batch: shared state + worker
# ---------------------------------------------------------------------------

@dataclass
class BatchState:
    """Shared-across-workers coordination for concurrent run-batch."""
    agent: str
    args: argparse.Namespace
    pause_until: float = 0.0  # epoch seconds; workers sleep past this
    rate_limit_streak: int = 0
    success: int = 0
    fail: int = 0
    lock: threading.Lock | None = None

    def __post_init__(self):
        self.lock = threading.Lock()

    def maybe_global_pause(self) -> None:
        """If pause_until is in the future, sleep until then.  Re-checks
        usage on wake-up to confirm reset."""
        while True:
            now = time.time()
            with self.lock:
                wait = self.pause_until - now
            if wait <= 0:
                return
            time.sleep(min(wait, 30))  # 30s ticks for Ctrl-C responsiveness

    def request_global_pause(self, seconds: float, reason: str) -> None:
        with self.lock:
            new_until = time.time() + seconds
            if new_until > self.pause_until:
                self.pause_until = new_until
                print(
                    f"[{self.agent}/run-batch] global pause "
                    f"({seconds:.0f}s): {reason}",
                    flush=True,
                )

    def preflight(self) -> None:
        """Probe usage; if over threshold, request a global pause."""
        if self.args.no_preflight:
            return
        usage = fetch_usage(self.agent)
        if usage is None:
            return
        result = usage_preflight_pause_seconds(
            usage,
            self.args.five_hour_stop_pct,
            self.args.seven_day_stop_pct,
            self.args.rate_limit_pause_seconds,
        )
        if result is not None:
            pause_s, reason = result
            self.request_global_pause(pause_s, reason)


# Drain flag — set by the signal handler, polled by the dispatch loops
# to stop submitting new work while letting in-flight work finish.
_drain_requested: bool = False


def is_drain_requested() -> bool:
    """Return True after the signal handler has been triggered."""
    return _drain_requested


def install_signal_handler() -> None:
    """Install SIGINT/SIGTERM handlers that flip the drain flag.

    First signal: graceful drain — in-flight workers finish, queued
    futures get cancelled (via Future.cancel(), which only affects
    futures that haven't started running), no new sequential work
    starts.  Re-invoke to resume; completed workspaces are skipped
    automatically via the outcome-event check.

    Second signal: immediate exit (sys.exit(130)) — the user really
    wants out, take no more care of in-flight launchers.
    """
    seen = {"count": 0}

    def handler(signum, frame):  # noqa: ARG001
        global _drain_requested
        seen["count"] += 1
        if seen["count"] == 1:
            _drain_requested = True
            print(
                "\n[run-batch] interrupted — draining: in-flight "
                "workspaces will finish, queued workspaces will be "
                "cancelled.  Re-invoke to resume; completed workspaces "
                "are skipped automatically.  Send signal again to "
                "force-exit immediately.",
                file=sys.stderr,
            )
        else:
            print(
                "\n[run-batch] second signal — force-exit.",
                file=sys.stderr,
            )
            sys.exit(130)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


def run_one(state: WorkspaceState, timeout_s: int) -> tuple[bool, bool, str, str, str, dict | None]:
    """Run one workspace's launcher.  Returns:
    (succeeded, rate_limited, summary, stdout, stderr, usage).

    Never raises — any exception (timeout, missing launcher, OS-level
    subprocess failure, etc.) is folded into a (False, False, ...)
    result so the surrounding worker can record the failure and move
    on to the next workspace instead of aborting the batch.
    """
    try:
        proc = subprocess.run(
            [str(state.launcher)],
            cwd=str(state.workspace),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired as e:
        # Python stdlib quirk: TimeoutExpired's `stdout` / `stderr`
        # are the raw byte buffers captured before the timeout fired,
        # NOT the decoded text — the text-mode decode only happens on
        # successful completion of subprocess.run.  Coerce to str so
        # downstream parsers (detect_rate_limit, extract_usage_from_log)
        # don't blow up with `<bytes>.startswith(<str>)`.
        so = _to_text(e.stdout)
        se = _to_text(e.stderr)
        # The runner inside the launcher may have written a valid
        # outcome event (e.g. game_over) BEFORE the launcher itself got
        # SIGKILL'd at the wallclock deadline.  Re-check the workspace
        # jsonl after the timeout — if outcome present, count it as
        # success despite the timeout (the puzzle did finish; the agent
        # CLI just hadn't tidied up by the deadline).
        usage = extract_usage_from_log(so)
        if state.has_outcome:
            return (
                True, False,
                f"timeout after {timeout_s}s, outcome event present",
                so, se, usage,
            )
        return (
            False, False,
            f"timeout after {timeout_s}s",
            so, se, usage,
        )
    except (FileNotFoundError, PermissionError, NotADirectoryError) as e:
        return (
            False, False,
            f"launcher unrunnable: {type(e).__name__}: {e}",
            "", "", None,
        )
    except Exception as e:
        # Defensive catch-all so a single broken workspace can't kill
        # the whole batch.  We log type+message; full traceback would
        # be noise across N concurrent workers and is reproducible by
        # running the launcher manually.
        return (
            False, False,
            f"launcher error: {type(e).__name__}: {e}",
            "", "", None,
        )
    rate_limited = detect_rate_limit(proc.stdout or "", proc.stderr or "")
    usage = extract_usage_from_log(proc.stdout or "")
    if rate_limited:
        return False, True, f"rate-limited (exit={proc.returncode})", proc.stdout, proc.stderr, usage
    if state.has_outcome:
        return True, False, f"exit={proc.returncode}, outcome event present", proc.stdout, proc.stderr, usage
    return False, False, f"exit={proc.returncode}, no outcome event", proc.stdout, proc.stderr, usage


def process_workspace(idx: int, total: int, w: WorkspaceState, batch: BatchState) -> None:
    """Worker entry point — runs ONE workspace with rate-limit-aware
    retry semantics.  Updates batch.success / batch.fail.

    Never raises — top-level try/except wraps the loop so a malformed
    workspace, an unexpected exception in run_one, or a transient
    error during preflight/pause coordination only fails THIS worker,
    not the whole batch.  Other workers continue uninterrupted.
    """
    try:
        _process_workspace_inner(idx, total, w, batch)
    except Exception as e:
        with batch.lock:
            batch.fail += 1
        print(
            f"  [{idx}/{total}] {w.puzzle_id} FAILED "
            f"(worker exception: {type(e).__name__}: {e}); "
            f"see {w.workspace}/{batch.agent}-exec.log if it exists",
            flush=True,
        )


def _process_workspace_inner(idx: int, total: int, w: WorkspaceState, batch: BatchState) -> None:
    attempt = 0
    while True:
        attempt += 1
        # Honour any global pause set by other workers.
        batch.maybe_global_pause()
        # Probe before launching (cheap; 1 HTTP call).
        batch.preflight()
        batch.maybe_global_pause()

        print(
            f"[{idx}/{total}] {w.puzzle_id} attempt={attempt} ...",
            flush=True,
        )
        t0 = time.time()
        ok, rate_limited, summary, _stdout, _stderr, usage = run_one(
            w, batch.args.per_session_timeout_seconds
        )
        elapsed = time.time() - t0
        usage_suffix = (
            f", in={usage['in']} out={usage['out']} tot={usage['total']}"
            if usage else ""
        )

        if rate_limited:
            with batch.lock:
                batch.rate_limit_streak += 1
                streak = batch.rate_limit_streak
            if streak >= batch.args.max_rate_limit_retries:
                print(
                    f"[{batch.agent}/run-batch] {streak} consecutive rate-limit "
                    f"hits across workers — likely the 7-day cap.  Bailing; "
                    f"re-invoke later to resume.",
                    flush=True,
                )
                # Set a long pause so other workers don't hammer.
                batch.request_global_pause(
                    batch.args.rate_limit_pause_seconds,
                    "max-rate-limit-retries reached",
                )
                with batch.lock:
                    batch.fail += 1
                return
            batch.request_global_pause(
                batch.args.rate_limit_pause_seconds,
                f"workspace {w.puzzle_id} rate-limited (streak={streak}/"
                f"{batch.args.max_rate_limit_retries})",
            )
            print(
                f"  [{idx}/{total}] {w.puzzle_id} rate-limited "
                f"({elapsed:.0f}s); retrying after pause",
                flush=True,
            )
            continue  # retry SAME workspace

        # Either succeeded or failed cleanly — reset the streak.
        with batch.lock:
            batch.rate_limit_streak = 0
            if ok:
                batch.success += 1
            else:
                batch.fail += 1
        if ok:
            print(
                f"  [{idx}/{total}] {w.puzzle_id} DONE "
                f"({elapsed:.0f}s, {summary}{usage_suffix})",
                flush=True,
            )
        else:
            print(
                f"  [{idx}/{total}] {w.puzzle_id} FAILED "
                f"({elapsed:.0f}s, {summary}{usage_suffix}); see "
                f"{w.workspace}/{batch.agent}-exec.log",
                flush=True,
            )
        return


def main(agent: str, argv: list[str] | None = None) -> int:
    """Entry point.  ``agent`` is one of {"codex", "claude"}."""
    ap = argparse.ArgumentParser(
        prog=f"{agent}/run-batch",
        description=(
            f"Sequentially or concurrently run every pending {agent} "
            f"workspace under ~/yugi-bench-runs/.  Idempotent + "
            f"mixed-mode safe + rate-limit aware."
        ),
    )
    ap.add_argument(
        "--runs-root",
        default=DEFAULT_RUNS_ROOT,
        help=f"Workspace root (default: {DEFAULT_RUNS_ROOT})",
    )
    ap.add_argument(
        "--concurrency",
        type=int,
        default=1,
        help=("Max concurrent sessions (default 1 = sequential).  Each "
              "session spawns its own MCP container; memory + CPU "
              "scale linearly.  Quota burns N× faster too."),
    )
    ap.add_argument(
        "--per-session-timeout-seconds",
        type=int,
        default=3600,
        help="Wallclock cap per session before SIGKILL (default 3600s = 1h).",
    )
    ap.add_argument(
        "--rate-limit-pause-seconds",
        type=int,
        default=3600,
        help=("Cap on pause duration (default 3600s = 1h).  Used both "
              "for the pre-flight 'sleep until reset' (capped) and for "
              "the post-flight 429-detected pause."),
    )
    ap.add_argument(
        "--max-rate-limit-retries",
        type=int,
        default=5,
        help=("Bail if rate-limit fires this many times in a row across "
              "all workers without a successful session in between.  "
              "Default 5 = ~5h of waiting before giving up."),
    )
    ap.add_argument(
        "--five-hour-stop-pct",
        type=float,
        default=80.0,
        help=("Pause-and-wait when 5-hour utilization crosses this "
              "(default 80%% = 20%% headroom)."),
    )
    ap.add_argument(
        "--seven-day-stop-pct",
        type=float,
        default=95.0,
        help=("Pause-and-wait when 7-day utilization crosses this "
              "(default 95%% = 5%% headroom)."),
    )
    ap.add_argument(
        "--no-preflight",
        action="store_true",
        help=("Skip the pre-flight usage probe; rely only on post-"
              "flight 429 detection."),
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help=("Discover and print workspaces without running anything.  "
              "Also probes + reports current usage if available."),
    )
    ap.add_argument(
        "--limit",
        type=int,
        default=None,
        help=("Cap the number of workspaces to process this invocation "
              "(applied AFTER --offset / --only / --puzzle-ids).  "
              "Idempotent skipping of already-done workspaces still "
              "applies, so re-invoking continues from where the previous "
              "limit stopped."),
    )
    ap.add_argument(
        "--offset",
        type=int,
        default=0,
        help=("Skip the first N pending workspaces before processing "
              "(applied AFTER --only / --puzzle-ids, BEFORE --limit)."),
    )
    ap.add_argument(
        "--only",
        type=str,
        default=None,
        help=("Only process workspaces for this single puzzle_id.  "
              "Matches via metadata.json's puzzle_id field, so "
              "timestamped workspace dir suffixes don't interfere."),
    )
    ap.add_argument(
        "--puzzle-ids",
        type=str,
        default=None,
        help=("Comma-separated list of puzzle_ids to process.  Like "
              "--only but for multiple ids."),
    )
    args = ap.parse_args(argv)

    install_signal_handler()
    runs_root = Path(args.runs_root).expanduser().resolve()
    workspaces = discover_workspaces(runs_root, agent)
    if not workspaces:
        print(
            f"[{agent}/run-batch] no {agent} workspaces found under {runs_root}",
            file=sys.stderr,
        )
        return 1

    pending = [w for w in workspaces if not w.has_outcome]
    done = [w for w in workspaces if w.has_outcome]
    print(
        f"[{agent}/run-batch] {len(workspaces)} {agent} workspaces total; "
        f"{len(done)} done, {len(pending)} pending."
    )

    # Probe + report usage at start (informational; honored only when
    # not --no-preflight).
    usage = fetch_usage(agent)
    if usage is not None:
        h5 = (usage.get("five_hour") or {}).get("utilization")
        h7 = (usage.get("seven_day") or {}).get("utilization")
        h5_str = f"{h5:.1f}%" if isinstance(h5, (int, float)) else "?"
        h7_str = f"{h7:.1f}%" if isinstance(h7, (int, float)) else "?"
        print(
            f"[{agent}/run-batch] usage: 5h={h5_str} "
            f"(stop>={args.five_hour_stop_pct}%) 7d={h7_str} "
            f"(stop>={args.seven_day_stop_pct}%)"
        )
    elif not args.no_preflight:
        print(
            f"[{agent}/run-batch] usage probe unavailable (no auth token "
            f"or endpoint unreachable); falling back to post-flight 429 "
            f"detection only."
        )

    if args.dry_run:
        for w in pending:
            tag = "ready" if w.launcher.exists() else "no-launcher"
            print(f"  pending [{tag}]: {w.workspace.name}")
        for w in done:
            print(f"  done             : {w.workspace.name}")
        return 0

    if not pending:
        print(f"[{agent}/run-batch] nothing to do.")
        return 0

    # Apply --only / --puzzle-ids / --offset / --limit BEFORE concurrency
    # dispatch.  Done workspaces stay reported in the totals regardless.
    if args.only or args.puzzle_ids:
        targets = set()
        if args.only:
            targets.add(args.only)
        if args.puzzle_ids:
            targets.update(s.strip() for s in args.puzzle_ids.split(",") if s.strip())
        pending = [w for w in pending if w.puzzle_id in targets]
    if args.offset > 0:
        pending = pending[args.offset:]
    if args.limit is not None and args.limit >= 0:
        pending = pending[: args.limit]

    runnable = [w for w in pending if w.launcher.exists()]
    no_launcher = [w for w in pending if not w.launcher.exists()]
    if no_launcher:
        print(
            f"[{agent}/run-batch] {len(no_launcher)} pending workspaces "
            f"have no exec launcher; require manual sessions."
        )
        for w in no_launcher[:5]:
            print(f"  manual-only: {w.workspace.name}")

    batch = BatchState(agent=agent, args=args)
    t_start = time.time()
    concurrency = max(1, args.concurrency)

    if concurrency == 1:
        for i, w in enumerate(runnable, 1):
            if is_drain_requested():
                print(
                    f"[{agent}/run-batch] drain: stopping after "
                    f"{i - 1} of {len(runnable)} workspaces "
                    f"(Ctrl-C requested cancellation of remaining work)",
                    flush=True,
                )
                break
            # process_workspace has its own try/except wrapper, but
            # belt-and-braces here so a single bad workspace can't
            # kill the whole sequential loop either.
            try:
                process_workspace(i, len(runnable), w, batch)
            except Exception as e:
                with batch.lock:
                    batch.fail += 1
                print(
                    f"  [{i}/{len(runnable)}] {w.puzzle_id} FAILED "
                    f"(worker exception escaped: {type(e).__name__}: {e})",
                    flush=True,
                )
    else:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [
                pool.submit(process_workspace, i, len(runnable), w, batch)
                for i, w in enumerate(runnable, 1)
            ]
            already_cancelled = False
            cancelled_count = 0
            for f in as_completed(futures):
                # On first drain detection, cancel every queued future.
                # Future.cancel() only succeeds for futures that haven't
                # started running — in-flight ones complete naturally.
                # Futures that get cancelled resolve with CancelledError
                # which we catch + count silently.
                if is_drain_requested() and not already_cancelled:
                    cancelled_count = sum(1 for fut in futures if fut.cancel())
                    if cancelled_count > 0:
                        print(
                            f"[{agent}/run-batch] drain: cancelled "
                            f"{cancelled_count} queued workspace(s); "
                            f"in-flight will finish",
                            flush=True,
                        )
                    already_cancelled = True
                # process_workspace's own try/except should make this
                # never raise.  Belt-and-braces: if some unexpected
                # path inside an executor task DOES escape, log it
                # and keep iterating over the other futures instead
                # of letting the exception kill in-flight workers.
                try:
                    f.result()
                except CancelledError:
                    pass  # drained future, already counted
                except Exception as e:
                    with batch.lock:
                        batch.fail += 1
                    print(
                        f"[{agent}/run-batch] worker exception escaped "
                        f"({type(e).__name__}: {e}); other workers continue",
                        flush=True,
                    )

    total = time.time() - t_start
    print(
        f"\n[{agent}/run-batch] DONE. success={batch.success} "
        f"fail={batch.fail} manual-only={len(no_launcher)} "
        f"concurrency={concurrency} total={total:.0f}s ({total / 60:.1f}min)"
    )
    print(
        "[{0}/run-batch] aggregate with: {1}".format(
            agent,
            Path(__file__).resolve().parent.parent / "aggregate-results.py",
        )
    )
    return 0 if batch.fail == 0 else 1
