# yugi-bench

[![tests](https://github.com/yugi-bench/yugi-bench-v1/actions/workflows/tests.yml/badge.svg)](https://github.com/yugi-bench/yugi-bench-v1/actions/workflows/tests.yml)
[![engine-tests](https://github.com/yugi-bench/yugi-bench-v1/actions/workflows/engine-tests.yml/badge.svg)](https://github.com/yugi-bench/yugi-bench-v1/actions/workflows/engine-tests.yml)
[![replay-verify](https://github.com/yugi-bench/yugi-bench-v1/actions/workflows/replay-verify.yml/badge.svg)](https://github.com/yugi-bench/yugi-bench-v1/actions/workflows/replay-verify.yml)
[![container](https://github.com/yugi-bench/yugi-bench-v1/actions/workflows/container.yml/badge.svg)](https://github.com/yugi-bench/yugi-bench-v1/actions/workflows/container.yml)
[![lint](https://github.com/yugi-bench/yugi-bench-v1/actions/workflows/lint.yml/badge.svg)](https://github.com/yugi-bench/yugi-bench-v1/actions/workflows/lint.yml)
[![codeql](https://github.com/yugi-bench/yugi-bench-v1/actions/workflows/codeql.yml/badge.svg)](https://github.com/yugi-bench/yugi-bench-v1/actions/workflows/codeql.yml)

[![License](https://img.shields.io/badge/license-Apache--2.0-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/)
[![Paper](https://img.shields.io/badge/paper-NeurIPS%202026%20ED-red.svg)](#citation)
[![MCP](https://img.shields.io/badge/MCP-server-purple.svg)](agent-mcp-eval/README.md)
[![Dataset](https://img.shields.io/badge/data-CC--BY--4.0-blueviolet.svg)](#license)

A long-horizon tool-use benchmark for LLM agents on a deterministic
Yu-Gi-Oh! rule engine.

**217 single-turn-win puzzles.** The agent acts in an interactive,
deterministic, fully-observable environment through a structured
24-tool interface. Every "win" is verified by replaying the agent's
tool calls through the real EDOPro / `ocgcore` engine, the same
ruleset the game uses.

**Two evaluation modes** that share one engine, one prompt scaffold,
and one verifier:

- **N-attempts (bulk).** Up to N full-solution submissions per
  puzzle. Tests multi-step planning and (when N>1) error recovery
  from engine feedback.
- **Fully interactive.** Per-turn tool-use loop, one response verb
  per engine decision. Tests long-horizon agentic tool use, state
  tracking, and information gathering.

The same engine + verifier is exposed through a containerised MCP
tool server, so the benchmark is directly usable for reinforcement
learning with verifiable rewards and for safe external-agent
evaluation.

## Table of contents

- [Quickstart](#quickstart--interactive-benchmark-via-vllm)
- [Setup options](#setup-options)
- [Release modes](#release-modes)
- [Repo layout](#repo-layout)
- [Providers](#providers)
- [Path overrides](#path-overrides)
- [Solution format (n-attempts mode)](#solution-format-n-attempts-mode)
- [Workflow](#workflow)
- [Container (MCP environment)](#container--engine-as-mcp-environment)
- [Host install (Mac benchmark workflow)](#host-install--mac-benchmark-workflow)
- [Aggregating runs](#aggregating-runs)
- [Troubleshooting](#troubleshooting)
- [Regenerating the benchmark](#regenerating-the-benchmark)
- [Citation](#citation)
- [License](#license)

## Quickstart — interactive benchmark via vLLM

One-time install (clones pinned upstream sources, builds `libocgcore`,
installs Python deps, runs an offline replay of one puzzle to verify):

```bash
git clone <yugi-bench>
cd yugi-bench
git checkout dev
./setup.sh
```

Serve your model with [vLLM](https://docs.vllm.ai/) in a separate
terminal — pick any OpenAI-tool-calling-compatible model:

```bash
vllm serve meta-llama/Llama-3.1-8B-Instruct
```

Run the interactive benchmark:

```bash
python api-eval/runner.py --provider vllm \
    --model meta-llama/Llama-3.1-8B-Instruct \
    --limit 5
```

Per-puzzle JSONL logs land in `results/interactive-vllm-<model>/`. The
provider defaults `--base-url` to `http://localhost:8000/v1` and
`api_key` to `"vllm"`; override `--base-url` if your server is elsewhere.

After a run, replay-verify any winning trace as a sanity check (no API
spend; replays through the engine):

```bash
python api-eval/extract_actions.py results/interactive-vllm-<model>/<puzzle_id>.jsonl
python src/engine/replay.py --solutions solutions --only <puzzle_id>
```

## Setup options

```
./setup.sh                 # default: install + verify
./setup.sh --skip-pip      # skip Python dep install
./setup.sh --no-verify     # skip the post-install replay
./setup.sh --force         # re-clone + rebuild even if present
./setup.sh --help          # full flag reference
```

System tools required: `g++` (C++17), `make`, `premake5`, `git`,
`python3` (3.11+), `pip` (or `uv`). Verified end-to-end on Linux arm64;
Linux x86_64 + macOS expected to work but less tested. Build takes
~60 s on a 4-core machine; total `vendor/` footprint is ~475 MB.

Pin overrides (rarely needed; defaults are committed in `setup.sh`):

```bash
OCGCORE_REF=<sha> BABEL_REF=<sha> SCRIPTS_REF=<sha> ./setup.sh
```

## Release modes

Both modes share the same engine core. The model responds to the
engine's real pending-decision stream via 20 response verbs (one per
`field::process(Processors::X&)` hook in `ocgcore`).

| Mode | How the model plays | Typical use |
|---|---|---|
| **N-attempts mode (`--attempts 1`)** | Single pass: model returns a complete list of response-verb calls. | Reasoning benchmark — can the model plan the full solution from the initial state? |
| **N-attempts mode (`--attempts N>1`)** | Up to N attempts: on failure, the model gets the error and engine pending-decision and may resubmit a fresh full solution. | Error-recovery benchmark — does self-correction from feedback help? |
| **Fully interactive mode (`--interactive`)** | Fully interactive tool-use episode: one response verb per engine decision, observations between turns. | Agentic benchmark — multi-turn tool-use loop. The Quickstart above runs this mode. |

An n-attempts solution is a JSON list of `{"tool", "args"}` calls
(see *Solution format* below). A fully-interactive run is a streaming
tool-use conversation logged as JSONL.

## Repo layout

```
data/yugioh_bench.jsonl          Full benchmark — 217 playable puzzles,
                                 curated from upstream ProjectIgnis/Puzzles.
                                 GITIGNORED — produced locally by setup.sh
                                 from vendor/puzzles/ so nothing Konami-
                                 derived ships from this repo.
data/yugioh_bench_verified.jsonl Verified subset — 133 puzzles with a
                                 Konami-shipped gold solution.
                                 GITIGNORED — produced locally by setup.sh.
solutions/<id>.json              Per-puzzle replay-verified action lists.

src/                             Internal Python packages (added to sys.path
                                 by entry-point scripts and pytest).
  engine/                          Engine core + replay machinery.
    core.py                          libocgcore FFI, CardDB, OCGEngine
    harness.py                       20 respond_* methods, StepResult, PendingDecision
    state.py                         Perspective-filtered observation builder
    tools.py                         25 JSON-Schema tools (20 response + 4 inspection + 1 restart)
    episode.py                       Fully interactive Episode loop, ResumeError, chain auto-decline
    replay.py                        N-attempts evaluator core + tolerant_chains + auto_advance_opponent
    multi_attempt.py                 Retry loop wrapper (N>1 attempts)
  providers/                       Pluggable LLM backends + LCD ABC.
    base.py                          ToolCallingProvider abstract class
    anthropic.py                     AnthropicToolProvider
    openai.py                        OpenAIToolProvider, VLLMToolProvider
    deepseek.py                      DeepSeekToolProvider
    claude_cli.py                    ClaudeCLIToolProvider (OAuth Max-sub)
  lib/                             Shared prompt-building library.
    prompt_builder.py                build_bulk_prompt + build_interactive_system_prompt
    grammar.py                       render_action_grammar(mode)
    glossary.py                      render_full_glossary / render_seen_glossary
    state_render.py                  render_omniscient_state / render_visible_state
    puzzle_preamble.py               "this is a puzzle, win this turn" framing
  dataset/                         Dataset-author utilities.
    build_benchmark.py               Generate yugioh_bench.jsonl from upstream Lua
    build_verified_subset.py         Regenerate data/yugioh_bench_verified.jsonl
    enrich.py                        Add metadata to dataset rows
    dump_prompt.py                   Inspect any puzzle's built prompt

api-eval/                        API-driven evaluation entry points.
  runner.py                        Single CLI entry point for both eval modes:
                                     --attempts N    n-attempts bulk mode
                                                     (N=1 single-shot, N>1 retry-with-feedback)
                                     --interactive   fully interactive tool-use loop
                                   Default: --interactive when --attempts unset.
  aggregate.py                     Result aggregation across runner sweeps
  extract_actions.py               Winning interactive JSONL → solutions/<id>.json
  run_sweep_containerised.py       Parallel docker-driven replay sweeps

agent-mcp-eval/                  Engine-as-MCP-environment + per-puzzle workspace
                                 prep for benchmarking interactive agent CLIs
                                 (Claude Code, Codex CLI).  See "Container —
                                 engine-as-MCP-environment" below.
  server.py                        MCP-over-stdio server, one puzzle per container
  Dockerfile + build-image.sh      Self-contained image build (no host deps)
  aggregate-results.py             CSV/JSON/MD summary across sessions
  _lib/                            Shared puzzle-picker + run-batch core
  codex/                           Codex variant (prep-session, prep-batch, exec
                                   launcher, run-batch, restrictions template)
  claude/                          Claude Code variant (same shape as codex/)
  README.md + per-agent README     Workflow docs

tests/                           Unit + integration tests (parsers, encoders,
                                 harness, auto-opponent, container dispatcher).

setup.sh                         One-step setup: vendors deps in vendor/,
                                 builds libocgcore, installs Python deps,
                                 builds data/yugioh_bench{,_verified}.jsonl
                                 from upstream ProjectIgnis/Puzzles, and
                                 verifies via offline replay.

vendor/                          Populated by setup.sh (gitignored).
sample/                          Standalone worked-example puzzle.
results/                         Per-run evaluation output (gitignored).
```

## Providers

`api-eval/runner.py --provider <name>` selects a backend. Each implements
`providers.ToolCallingProvider` (defined in `src/providers/base.py`). Built-in:

| `--provider` | Backend | Notes |
|---|---|---|
| `vllm` (alias `vllm-server`) | vLLM OpenAI-compatible server | Default `http://localhost:8000/v1`, `api_key="vllm"`. **Quickstart path.** |
| `anthropic` (alias `claude`) | Anthropic Messages API | `ANTHROPIC_API_KEY`, supports adaptive thinking + effort levels |
| `openai` (alias `chatgpt`, `gpt`) | OpenAI Responses / Chat | `OPENAI_API_KEY`, `--base-url` for compatible endpoints |
| `deepseek` | DeepSeek (native + reasoning_effort) | `DEEPSEEK_API_KEY` |
| `lmstudio` (alias `lm-studio`, `local`) | LM Studio local server | Default `http://localhost:1234/v1` |
| `claude-cli` | Claude Code CLI in `--print` mode | OAuth via `claude login`; uses Pro/Max subscription instead of API spend |

Adding a new provider is one file under `src/providers/` plus one line in
`src/providers/__init__.py::get_provider()`. See the `ToolCallingProvider`
docstring in `src/providers/base.py` for the full contract.

## Path overrides

Defaults look in `vendor/` first; override individually if your binaries
live elsewhere:

| Env var | Default search order | Purpose |
|---|---|---|
| `YGO_DYLIB` | `vendor/ygopro-core/bin/release/libocgcore.{so,dylib}` then `../edopro/ocgcore/bin/release/...` | Compiled engine |
| `YGO_SCRIPT_DIR` | `vendor/distribution/script` then `../distribution/script` | Lua script root |
| `YGO_CARD_SCRIPT_DIR` | `$YGO_SCRIPT_DIR/official` | Per-card scripts |
| `YGO_DB_DIR` | `vendor/distribution/expansions` then `../distribution/expansions` | `.cdb` files |

## Solution format (n-attempts mode)

Every pending decision the engine emits names one of 20 response verbs.
A solution for n-attempts mode is a JSON array of those calls, in order:

```json
[
  {"tool": "select_idlecmd", "args": {"command": "summon", "index": 0}},
  {"tool": "select_place",   "args": {"places": [{"player": 0, "location": 4, "sequence": 2}]}},
  {"tool": "select_chain",   "args": {"index": null}},
  {"tool": "select_battlecmd", "args": {"command": "attack", "index": 0}},
  {"tool": "select_card",    "args": {"indices": [0]}}
]
```

The full JSON-Schema for every verb lives in `src/engine/tools.py::TOOLS`. For
a terse reference:

```bash
python src/dataset/dump_prompt.py yugioh_puzzle_42ffb7a8 --section grammar
```

## Workflow

Both modes share a unified prompt builder and a unified flag set:

| Flag | N=1 | N>1 | Interactive | What it does |
|---|---|---|---|---|
| `--attempts N` | `--attempts 1` (single-shot) | `--attempts N` (retry-with-feedback) | n/a (interactive) | Maximum submissions per puzzle in n-attempts mode. |
| `--show-solution` | ✓ | ✓ | ✓ | **Ceiling-test mode** — injects the gold-solution walkthrough. Mark results as oracle-runs. |
| `--forage` | n/a | n/a | ✓ (default off) | **Forage mode** — system prompt is LEAN (no state dump, no glossary), model gets `inspect_card` + the other inspection tools to forage. Tests agentic information-gathering. Without `--forage`: prompt is RICH (full omniscient state + full glossary up front). |

The `restart` tool is **always available** in fully interactive mode regardless of `--forage`.

### N-attempts bulk mode

```bash
# Single-shot (one attempt only)
python api-eval/runner.py --provider anthropic --model claude-opus-4-7 --attempts 1

# Three attempts with retry context (engine feedback between attempts)
python api-eval/runner.py --provider anthropic --model claude-opus-4-7 --attempts 3

# Optional ceiling test (gold-solution hint)
python api-eval/runner.py --provider anthropic --model claude-opus-4-7 --attempts 1 --show-solution
```

Bulk-mode statuses: `game_over` (with `winner`), `incomplete`,
`parse_error`, `exception`. Summary JSON lands in
`results/<run-name>/_summary.json`. Each failed attempt feeds the engine
error + pending-decision-at-failure into the next prompt; the engine
resets between attempts.

### Replay-evaluate a directory of action-list solutions (no LLM call)

```bash
python src/engine/replay.py --solutions solutions/
```

### Fully interactive mode flags

```bash
# Default: rich prompt — full omniscient state + full card glossary.
python api-eval/runner.py --provider anthropic --model claude-opus-4-7

# --forage: lean prompt, model uses inspection tools.
python api-eval/runner.py --provider anthropic --model claude-opus-4-7 --forage
```

**Chain auto-decline (interactive only):** when the model batches
multiple actions in one turn, the harness auto-declines any optional
`select_chain` window between them unless the model included an
explicit `select_chain` in the batch. Forced chains are never
auto-decline'd.

## Container — engine-as-MCP-environment

For benchmarking *interactive* agent CLIs (Claude Code, Codex CLI,
OpenHands, custom MCP clients) the `agent-mcp-eval/` tree packages the
engine as an MCP-over-stdio server: one puzzle per container,
`--network none` air-gapped engine, agent connects from outside.

```bash
./agent-mcp-eval/build-image.sh                          # builds yugi-bench-env:latest
docker run -i --rm --network none \
    -v $PWD/results:/work/results \
    yugi-bench-env:latest --puzzle yugioh_puzzle_42ffb7a8
```

The agent (Claude Code / Codex / etc.) connects via stdio MCP. See
`agent-mcp-eval/README.md` for the full mcp.json snippet and isolation
guarantees. JSONL output schema is identical to the API-driven Episode
loop, so all the existing analysis tooling (`api-eval/extract_actions.py`,
`engine.replay`) works unchanged.

For parallel sweeps: `api-eval/run_sweep_containerised.py` fans out N
concurrent `docker run` invocations and collects results.

## Host install — Mac benchmark workflow

For benchmarking interactive agent CLIs on a Mac with hermetic
per-puzzle isolation and full automatic logging, `agent-mcp-eval/`
is a turnkey workflow.  Each agent has its own per-agent entry-point
scripts under `agent-mcp-eval/{codex,claude}/`.

```bash
# Build the engine image once.  setup.sh must have been run first
# (see "One-step install" above) — it produces data/yugioh_bench.jsonl
# locally from vendor/puzzles, which the image then copies in.
./agent-mcp-eval/build-image.sh

# Prep N hermetic per-puzzle workspaces under ~/yugi-bench-runs/.
# Default strategy is `easy` (full 217-puzzle dataset, sorted by
# complexity); pass `--strategy verified-easy` to limit to the
# 133-puzzle Konami-gold subset, or `--strategy all` to take all 217
# regardless of --count.  Pick the per-agent prep-batch.sh; no --agent
# flag.
./agent-mcp-eval/codex/prep-batch.sh  --count 10
./agent-mcp-eval/claude/prep-batch.sh --count 10

# Manual flow per workspace:
#   codex:  cd <ws> && CODEX_HOME=$PWD/.codex codex   # type "start"
#   claude: cd <ws> && claude --strict-mcp-config --mcp-config ./.mcp.json

# Auto-driven flow per workspace:
bash <ws>/run-codex-exec.sh
bash <ws>/run-claude-exec.sh

# Auto-driven flow over every pending workspace (idempotent + mixed-mode safe):
./agent-mcp-eval/codex/run-batch.py
./agent-mcp-eval/claude/run-batch.py

# Aggregate across both agents when done (agent-agnostic)
./agent-mcp-eval/aggregate-results.py
```

Each workspace's `.mcp.json` (Claude) or `.codex/config.toml` (Codex) is
locked to one puzzle; launchers use `claude --strict-mcp-config` /
`CODEX_HOME=$PWD/.codex` to isolate from globals. See
`agent-mcp-eval/README.md` for full docs including the cgroup gotcha
under high-concurrency podman + the codex OAuth symlink mechanism.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `setup.sh` says `missing tool: premake5` | Distro `premake5` not installed | Install per https://premake.github.io/; `setup.sh` is idempotent. |
| `apt install premake5` says `Unable to locate package premake5` | Ubuntu 24.04+ dropped the package, and upstream binaries ship x86-64 only | Build from source: `sudo apt install -y build-essential uuid-dev && git clone --depth 1 --branch v5.0.0-beta7 https://github.com/premake/premake-core.git && cd premake-core && make -f Bootstrap.mak linux && sudo cp bin/release/premake5 /usr/local/bin/`. ~1 min on a 4-core arm64 box. |
| `OSError: cannot open shared object file` | `libocgcore.{so,dylib}` not where `engine.core` expects | `./setup.sh --force`, or set `YGO_DYLIB`. |
| `sqlite3.OperationalError: no such table: datas` | 0-byte `.cdb` placeholder in BabelCDB upstream | Benign — `CardDB` warns and skips. |
| `FileNotFoundError: .../script/official/c<code>.lua` | Script tree missing or layout wrong | `./setup.sh --force` re-fetches CardScripts. |
| Verification step fails after a fresh build | Engine wiring / upstream drift | `python src/engine/replay.py --solutions solutions --only yugioh_puzzle_42ffb7a8 -v` |
| `ImportError: No module named anthropic` | SDK not installed | `pip install anthropic>=0.40.0`. |
| Container build fails on Apple Silicon podman with Rosetta prompt | applehv default + Rosetta-in-VM enabled | `~/.config/containers/containers.conf` with `[machine] rosetta = false`, then `podman machine init`. |
| Container OOM during ocgcore compile | VM memory cap too low | `podman machine set --memory 8192` or `--build-arg JOBS=2` on the self-contained Dockerfile. |

## Aggregating runs

Once a sweep finishes, `aggregate` walks the per-puzzle JSONL
trees, joins each puzzle's terminal `outcome` row with the dataset
metadata in `data/yugioh_bench.jsonl`, and emits the headline
tables. Output formats: `text` (default), `json`, `csv`, `md`.

```bash
# Headline summary on stdout
python api-eval/aggregate.py results/interactive-deepseek-v4-pro-effmax/

# Multiple runs at once (shell-glob expansion)
python api-eval/aggregate.py results/interactive-* --format json

# Per-puzzle markdown table for a paper appendix
python api-eval/aggregate.py results/run-x --format md --per-puzzle > run-x.md
```

The aggregator emits: top-line counts, breakdown by status,
breakdown by termination type, win rate by complexity tier (1–10),
win rate by source kind (with the official-vs-community partition
the paper relies on), per-source win rate, and a token-usage
rollup. It is the canonical entry point for reproducing reported
benchmark numbers from a recorded sweep — drop traces into
`results/<run-name>/` of a fresh checkout, run the aggregator,
match the headline table.

## Regenerating the benchmark

`data/yugioh_bench.jsonl` is gitignored and produced locally by
`setup.sh` from `vendor/puzzles/` (the pinned ProjectIgnis/Puzzles
clone).  Re-run `./setup.sh --force` to rebuild after a vendor refresh,
or call `build_benchmark.py` directly for a custom puzzle root:

```bash
python src/dataset/build_benchmark.py \
    --puzzle-root vendor/puzzles/'Canon collection' \
    --lean --overwrite
```

`--lean` strips Konami card text (joined back locally by
`src/dataset/enrich.py` from BabelCDB); `--overwrite` is required to
replace an existing dataset file.

## Citation

If you use yugi-bench in your work, please cite the paper:

```bibtex
@inproceedings{yugibench2026,
  title  = {yugi-bench: A Long-Horizon Tool-Use Benchmark and Frontier-Model Capability Analysis},
  author = {The yugi-bench authors},
  booktitle = {Advances in Neural Information Processing Systems 39 (NeurIPS 2026) Evaluations and Datasets Track},
  year   = {2026},
}
```

`CITATION.cff` in the repository root is also recognised by GitHub
and citation-management tools.

## Roadmap

The benchmark is released as a starting point. Planned future editions:

- Broader domain coverage (formats beyond single-turn-win,
  community-contributed puzzle decks).
- Refined puzzle complexity tiers, with calibration against
  human-solver effort.
- New evaluation modes (e.g. model-vs-model self-play, mixed bulk +
  interactive, long-horizon multi-turn duels).
- Containerised RL harness for reinforcement learning with
  engine-verifiable rewards.

We invite the community to use the benchmark, fork it, file issues,
and propose puzzle additions. See `CONTRIBUTING.md` for the contribution
flow.

## Acknowledgements

The benchmark builds on the open-source work of the Yu-Gi-Oh! engine
maintainers and the community puzzle archives:

- [edo9300/ygopro-core](https://github.com/edo9300/ygopro-core) — the
  OCG rule engine (libocgcore).
- [ProjectIgnis/BabelCDB](https://github.com/ProjectIgnis/BabelCDB) +
  [CardScripts](https://github.com/ProjectIgnis/CardScripts) — the
  card database and Lua effect scripts.
- [ProjectIgnis/Puzzles](https://github.com/ProjectIgnis/Puzzles) —
  the upstream puzzle archives that the curated 217-puzzle benchmark
  draws from.

Yu-Gi-Oh! is a trademark of Konami Digital Entertainment Co., Ltd.
This project is not affiliated with, endorsed by, or sponsored by
Konami. See `NOTICE` for the full third-party attribution.

## License

Code: Apache License 2.0 (see `LICENSE`).
Dataset (puzzle manifest + replay-verified machine solutions): CC BY 4.0.

The vendored upstream sources retain their respective licenses; see
`NOTICE` for details.
