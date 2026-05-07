# yugi-bench host install — benchmark a clean agent CLI

Cycle a clean Claude Code or OpenAI Codex CLI session through N puzzles,
one puzzle per session, with hermetic isolation between sessions and
fully automatic logging. Pick which agent you want.

## Layout

```
agent-mcp-eval/
├── README.md                          (this file)
├── build-image.sh                     (shared infra: build the engine image)
├── Dockerfile           (shared infra: clones + compiles libocgcore)
├── aggregate-results.py               (shared, agent-agnostic outcome reader)
├── _lib/                              (shared helpers, called by per-agent scripts)
│   ├── pick_puzzles.py                (puzzle selection by strategy)
│   └── run_batch_core.py              (idempotent workspace driver)
├── codex/                             (codex variant — see codex/README.md)
│   ├── README.md
│   ├── prep-session.sh                (prep one workspace)
│   ├── prep-batch.sh                  (prep N workspaces by strategy)
│   ├── exec-launcher.sh               (per-workspace template)
│   ├── run-batch.py                   (auto-driven loop, idempotent)
│   └── restrictions.toml.template
└── claude/                            (Claude Code variant — see claude/README.md)
    ├── README.md
    ├── prep-session.sh
    ├── prep-batch.sh
    ├── exec-launcher.sh
    └── run-batch.py
```

There are no agent-shared *entry-point* scripts. Each agent has its
own `prep-batch.sh` and `run-batch.py`. Common puzzle-selection +
workspace-driver logic lives under `_lib/` and is called as a library;
that's "shared code as needed" rather than a shared script.

## Prereqs

- **macOS** with **Docker Desktop** running. Linux + Docker also works
  without changes; Linux + Podman works if you `export DOCKER=podman`.
- **Agent CLI**: either Claude Code (`claude`) or OpenAI Codex
  (`codex`) — pick whichever you want to drive.
- **Python 3.10+** (used by the helper scripts only — the container
  has its own Python).
- A clone of this repo, with the top-level `./setup.sh` already run
  once. That populates `vendor/` (libocgcore + BabelCDB + CardScripts +
  ProjectIgnis/Puzzles) and builds `data/yugioh_bench.jsonl` locally
  from the upstream Puzzles clone — nothing Konami-derived ships from
  this repo, so the dataset has to be built before the image can copy
  it in.

No host-side `premake5` / `g++` build tooling required for the *image*
build itself — the Dockerfile re-compiles libocgcore inside. setup.sh
does need them on the host because it builds the lean dataset and
verifies the harness against the host-side libocgcore.

## One-time setup

```bash
git clone <yugi-bench>
cd yugi-bench
./setup.sh                       # vendor deps + build data/*.jsonl
./agent-mcp-eval/build-image.sh  # build the engine image
```

`build-image.sh` produces `yugi-bench-env:latest` (~260 MB layered,
~700 MB image size). First build is 3–5 minutes; re-builds are seconds
when only benchmark code changes.

## Workflow per agent

The two agent variants are operationally identical — only the entry-
point paths differ. Replace `<agent>` with `codex` or `claude`.

```bash
# 1. Prep a batch of N workspaces (default strategy = `easy`, full 217).
./agent-mcp-eval/<agent>/prep-batch.sh --count 10

# 2a. Manual flow.  Open one terminal per session.
#     codex:  cd <ws> && CODEX_HOME=$PWD/.codex codex
#     claude: cd <ws> && claude --strict-mcp-config --mcp-config ./.mcp.json
# Type "start" when the agent connects, let it play, close on _outcome.

# 2b. Auto-driven flow (single workspace).
bash <ws>/run-<agent>-exec.sh

# 2c. Auto-driven flow (every pending workspace, idempotent).
./agent-mcp-eval/<agent>/run-batch.py

# 3. Aggregate results across both agents (shared script, agent-agnostic).
./agent-mcp-eval/aggregate-results.py
```

Strategies for `prep-batch.sh` (operate on the full 217-puzzle dataset
by default; `--strategy verified-*` opts in to the 133-puzzle Konami-
gold subset):

- `easy` *(default)* — verified puzzles first (easiest → hardest),
  then non-verified (easiest → hardest); tie-break by puzzle id.
  Take the first N. Verified-first matches "do the puzzles with a
  Konami-shipped gold solution first" so capability is benchmarked
  on ground-truth-known items before the rest.
- `random` — random sample of the full 217.
- `all` — every puzzle in the full 217 in the same easy-order (ignores `--count`).
- `verified-easy` — sort the 133 verified-subset puzzles by complexity
  ascending, take the first N. Use when you want only puzzles with
  Konami-shipped gold solutions.
- `verified` — random sample of the 133 verified subset.
- `list:ID,ID,...` — explicit comma-separated puzzle list.

Workspaces land under `~/yugi-bench-runs/`. Codex workspaces are
suffixed `-codex`; claude workspaces have no suffix.

## Idempotency + mixed-mode safety

`run-batch.py` (per agent) walks every workspace under the runs root,
filters to its agent, and runs each pending one. **Pending vs done is
determined by whether `results/<id>.jsonl` contains an `outcome`
event** — that's identical regardless of how the puzzle finished
(manual paste, auto-driven exec, or any mix). Re-running is a no-op
for completed workspaces; partial runs get retried.

This means you can freely interleave manual and auto-driven sessions:
play a few by hand, then run the script for the rest — the final
state is identical to running the script alone.

## Run-batch flags

`run-batch.py` (per agent) is rate-limit-aware and supports
concurrency.  All flags are optional; defaults are tuned for
unattended overnight runs on a single OAuth window.

```
--concurrency N                max parallel sessions (default 1 = sequential)
--per-session-timeout-seconds  wallclock cap per session (default 1800s = 30min)
--rate-limit-pause-seconds     cap on pause duration (default 3600s = 1h)
--max-rate-limit-retries       bail after N consecutive 429s without a success
                               in between (default 5 = ~5h before giving up)
--five-hour-stop-pct PCT       pause when 5h utilization ≥ PCT (default 80)
--seven-day-stop-pct PCT       pause when 7d utilization ≥ PCT (default 95)
--no-preflight                 skip the usage probe; rely only on post-flight
                               429 detection
--limit N                      cap workspaces processed this invocation
                               (applied AFTER --offset / --only / --puzzle-ids)
--offset N                     skip first N pending workspaces
--only PUZZLE_ID               only process workspaces for this puzzle_id
--puzzle-ids ID1,ID2,...       only process workspaces for these puzzle_ids
--dry-run                      list workspaces + report current usage; don't run
```

### Filter-flag composition

`--only` / `--puzzle-ids` apply first (filter pending workspaces by id),
then `--offset` slices, then `--limit` caps.  All four compose with
`--concurrency` — concurrency dispatches across whatever the filter
chain produces.  Examples:

```bash
# Run only one puzzle (useful for debugging a specific failure):
./agent-mcp-eval/codex/run-batch.py --only yugioh_puzzle_42ffb7a8

# Run a hand-picked subset of three puzzles concurrently:
./agent-mcp-eval/codex/run-batch.py \
    --puzzle-ids yugioh_puzzle_42ffb7a8,yugioh_puzzle_044c693a,yugioh_puzzle_c55b6641 \
    --concurrency 3

# Process the first 20 pending workspaces, 4 in parallel — useful for
# a smoke run before scaling up to the full 217:
./agent-mcp-eval/codex/run-batch.py --limit 20 --concurrency 4

# Resume a long-running batch — skip what you already did:
./agent-mcp-eval/codex/run-batch.py --offset 50 --limit 30
# (note: re-running without --offset is also fine — completed workspaces
# are skipped via outcome-event detection regardless)
```

### Two layers of rate-limit protection

**Pre-flight probe** — before each session, hits the agent's own
usage endpoint:

| Agent | Endpoint | Auth source |
|---|---|---|
| codex  | `chatgpt.com/backend-api/codex/usage` | `~/.codex/auth.json:tokens.access_token` (+ `chatgpt-account-id` from `tokens.account_id`) |
| claude | `api.anthropic.com/api/oauth/usage`   | `~/.claude/.credentials.json:claudeAiOauth.accessToken` (+ `anthropic-beta: oauth-2025-04-20`) |

Both responses are normalised to `{five_hour: {utilization,
resets_at}, seven_day: {...}}`.  If either window's `utilization`
is ≥ its stop-pct, run-batch pauses until that window resets
(capped by `--rate-limit-pause-seconds`).  When auth isn't
available (no `codex login` / `claude login` done), the probe
silently falls back to "post-flight only" mode.

**Post-flight pattern match** — scans the launcher's combined
stdout/stderr for known rate-limit error phrases (`rate limit`,
`429`, `quota exceeded`, `5h window`, `7d window`, `too many
requests`, etc.).  On match: pauses + retries the SAME workspace.
After `--max-rate-limit-retries` consecutive matches across all
workers without a successful session in between, bails loud
(likely the 7-day cap is hit and human attention is warranted).

### Concurrency tradeoffs

- Each session spawns its own 1g/1cpu container — `--concurrency N`
  → ~Ng of memory needed.
- Quota burns N× faster.  If your 5h window is at 80% used and you
  set `--concurrency 4`, you'll trip the pre-flight pause after ~4
  sessions instead of ~16.
- Pre-flight pauses are GLOBAL — one worker observing a 429 sets
  a shared pause-until, all workers wake from their next pause-
  check and sleep together until the window resets.
- The MCP server's non-destructive log handling applies per
  workspace independently, so concurrent workers on different
  workspaces don't fight over JSONLs.

### Practical defaults

For an unattended overnight run on a single OAuth window:

```bash
# Sequential, conservative — won't burn quota faster than interactive use.
./agent-mcp-eval/codex/run-batch.py

# Faster: 4 parallel sessions.  Quota burns 4× faster too — if you have
# the 5h headroom, this finishes a 217-puzzle batch in ~25% of the time.
./agent-mcp-eval/codex/run-batch.py --concurrency 4

# Most aggressive — no headroom for interactive sessions while it runs.
./agent-mcp-eval/codex/run-batch.py --concurrency 8 \
    --five-hour-stop-pct 95 --seven-day-stop-pct 99
```

`--dry-run` is the safe smoke test: lists pending/done workspaces
+ prints current 5h/7d utilization so you can decide whether to
launch.



## Container behaviour: non-destructive log handling

The MCP container is one-shot per `docker run --rm` invocation. When a
new agent session connects (e.g. you reopen a completed workspace), a
fresh container starts. To prevent the previously-written
`results/<id>.jsonl` from being silently truncated, the container
classifies the existing log on startup:

- **complete** (has `outcome` event) → exits 0 immediately, leaves the
  JSONL untouched. Log line on stderr explains.
- **partial** (events but no `outcome`) → archives as
  `<id>.partial-<ts>.jsonl` and starts a fresh log.
- **corrupt** (un-parseable) → archives as
  `<id>.corrupt-<ts>.jsonl` and starts a fresh log.
- **fresh** (missing or empty) → normal path.

So manual + auto + reopened sessions can interleave freely. Move or
delete `results/<id>.jsonl` if you want to re-attempt a completed
puzzle.

## Isolation guarantees (shared across agents)

This is a benchmark, so cross-session contamination would invalidate
the results. The design enforces isolation at four layers:

1. **Per-puzzle MCP config.** Each workspace pins one `docker run`
   command targeting one puzzle id.
2. **Config-dir lockdown on the agent CLI.** Codex via
   `CODEX_HOME=$PWD/.codex`; Claude via `--strict-mcp-config
   --mcp-config ./.mcp.json`.
3. **Agent-side tool/memory disables.** Web search, image attach,
   memory subsystems all off in both agent configs. See per-agent
   READMEs for the full list.
4. **`--network none` on the engine container.** The puzzle file isn't
   reachable from inside the container; the agent can only see what
   the MCP tools return.

Multiple agent sessions in different terminals run in independent OS
processes. No state is shared.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `image yugi-bench-env:latest not found` | image not built or wrong tag | `./agent-mcp-eval/build-image.sh` |
| Container exits with permission error on `/work/results` | bind-mount uid mismatch (Linux+Podman) | the prep script auto-applies `--userns=keep-id` when it detects podman |
| Tool call returns is_error=true with "tool budget exhausted" | agent burned through `--max-tool-calls` | re-prep with `--max-tool-calls 1000` |
| Container build fails on Apple Silicon podman with Rosetta prompt | applehv default + Rosetta-in-VM enabled | `~/.config/containers/containers.conf` with `[machine] rosetta = false`, then `podman machine init` |
| Container OOM during ocgcore compile | VM memory cap too low | `podman machine set --memory 8192` |

## Podman notes (Linux)

Rootless podman works as a `docker` drop-in:

```bash
mkdir -p ~/.config/containers
cat > ~/.config/containers/containers.conf <<'EOF'
[engine]
cgroup_manager = "cgroupfs"
events_logger = "file"
EOF

DOCKER=podman ./agent-mcp-eval/build-image.sh
DOCKER=podman ./agent-mcp-eval/codex/prep-batch.sh --count 10
```

The cgroupfs override avoids the systemd-cgroup ↔ user-dbus race that
breaks podman under high concurrency.
