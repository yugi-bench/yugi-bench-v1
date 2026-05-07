# Contributing to yugi-bench

Thanks for your interest in contributing. This benchmark exists to give
the community a deterministic, engine-verified, long-horizon tool-use
testbed for LLM agents, and we expect it to grow over time.

## Ways to contribute

- **Bug reports** — open an issue with reproducer commands, the
  benchmark version (`git rev-parse HEAD`), and the expected vs. actual
  behaviour. Include the `_summary.json` and the relevant
  per-puzzle JSONL trace if applicable.
- **Puzzle additions** — propose new puzzles via pull request to
  `data/yugioh_bench.jsonl` along with a reproducer Lua setup and
  ideally a gold solution under `solutions/`. Each new puzzle should
  pass `python src/engine/replay.py --solutions solutions --only <id>`.
- **Engine + harness improvements** — pull requests that fix bugs in
  the response-verb dispatch, prompt builder, or replay verifier are
  welcome. Please attach a regression test under `tests/`.
- **New providers** — add a file under `src/providers/` that subclasses
  `ToolCallingProvider` (see `src/providers/base.py`'s docstring for the
  full contract) and register it in `src/providers/__init__.py::get_provider`.
- **New evaluation modes** — propose a new mode (alongside the existing
  n-attempts and fully interactive modes) by opening a discussion issue
  first; the mode should compose with the existing prompt builder and
  scoring layer.

## Development setup

```bash
git clone https://github.com/yugi-bench/yugi-bench-v1.git
cd yugi-bench-v1
./setup.sh
pip install -e ".[dev]"
pytest
```

## Testing

- `pytest` runs the unit + integration tests. Engine-backed tests
  auto-skip if `libocgcore.{so,dylib}` is not built.
- The dry-run smoke test (no API spend, no network) exercises the
  full episode loop against canned tool-call traces:
  ```bash
  python api-eval/runner.py --provider fixture --limit 3
  ```
- The CI workflow (`.github/workflows/tests.yml`) runs both the unit
  job (Python 3.11 + 3.12) and the engine job (Ubuntu + the full
  `setup.sh` bootstrap).

## Style

- Format: `ruff format .` (line length 100).
- Lint: `ruff check .` (config in `pyproject.toml`).
- Imports: `ruff check --select I --fix .` to auto-sort.
- Tests must be deterministic; mock provider calls.

## Pull requests

- One focused change per PR; commit messages explain *why*, not just
  *what*.
- Update `README.md` and any affected docs alongside the code change.
- Note any new provider keys, env vars, or external services in the
  PR description.
- The CI must be green before merge.

## Issues

- Use the issue templates for bug reports and puzzle proposals when
  available.
- Engine-bug exclusions (puzzles unsolvable due to a known
  libocgcore quirk) belong in `build_benchmark.py::EXCLUDED_PUZZLES`
  with a comment linking to the upstream issue.

## Code of conduct

This project adheres to the contributor covenant; see
[`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md). By participating you agree
to abide by its terms.
