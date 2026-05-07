# Codex CLI variant

Use the OpenAI Codex CLI as if it were the API — same one-shot puzzle-
play behaviour as the API-driven runner under api-eval/, but billed
against your ChatGPT Pro/Plus OAuth window instead of API tokens.

## Files in this directory

- **`prep-session.sh`** — generates one isolated workspace under
  `~/yugi-bench-runs/<id>-<ts>-codex/`. Invoked by the top-level
  `./agent-mcp-eval/codex/prep-batch.sh`.
- **`exec-launcher.sh`** — copied into each workspace as
  `run-codex-exec.sh`. Auto-driven equivalent of the manual flow:
  `codex exec --cd <ws> --skip-git-repo-check --ephemeral "start"`.
- **`restrictions.toml.template`** — the static lockdown profile. Read
  by `prep-session.sh`, merged with per-puzzle dynamic blocks (MCP
  server wiring, project trust, per-tool approval entries) into the
  workspace's `.codex/config.toml`. **User-editable** — flip toggles
  here to relax the lockdown across all subsequent workspaces.

## Workspace layout

```
~/yugi-bench-runs/<id>-<ts>-codex/
├── AGENTS.md                       (codex auto-discovers from cwd)
├── .codex/
│   ├── config.toml                 (template + dynamic per-puzzle block)
│   └── auth.json                   (symlink → ~/.codex/auth.json)
├── metadata.json                   (puzzle_id, image, flags, agent="codex")
├── results/                        (bind-mounted into container)
│   └── <puzzle_id>.jsonl           (canonical per-puzzle log)
└── run-codex-exec.sh               (auto-driven launcher)
```

## Two equivalent flows

Both produce an `outcome` event in `results/<id>.jsonl`. Use whichever
fits your situation; mix-and-match is safe (see top-level README on
idempotency).

**Manual paste flow:**

```bash
cd ~/yugi-bench-runs/<id>-<ts>-codex/
CODEX_HOME=$PWD/.codex codex          # opens TUI
# type: start
# (close session when _outcome lands)
```

**Auto-driven flow (single workspace):**

```bash
bash ~/yugi-bench-runs/<id>-<ts>-codex/run-codex-exec.sh
```

**Auto-driven flow (whole queue):**

```bash
./agent-mcp-eval/codex/run-batch.py
```

## Authentication

### ChatGPT OAuth (preferred — same as Claude Pro/Max billing model)

```bash
codex login                          # browser-based OAuth, one-time
```

Writes `~/.codex/auth.json`. Each workspace's `.codex/auth.json` is a
symlink to that global file, so all workspaces share one login. Token
refresh during a benchmark batch: codex refreshes via in-place writes
most of the time (follows the symlink and updates the global). If a
refresh happens via temp+rename instead, that one workspace ends up
with its own divergent auth.json — still works for the rest of the
token's lifetime.

### OPENAI_API_KEY fallback

```bash
export OPENAI_API_KEY="sk-..."
```

Skip `codex login` and codex picks up the env var as the auth source.
Caveat: if a stale `auth.json` got symlinked from a prior run, it
shadows the env var per
[openai/codex#3286](https://github.com/openai/codex/issues/3286).
Delete the workspace's `.codex/auth.json` if you switch from OAuth
to key-based mid-batch.

## The lockdown profile

`restrictions.toml.template` enforces "codex behaves like an API call,
not an agent" — disabling everything codex ships with except the
yugi-bench MCP tool surface. Specifically:

- **Top-level scalars**: `sandbox_mode = "workspace-write"`,
  `default_permissions = "cwd-only"`, `approval_policy = "never"`,
  `model_reasoning_effort = "xhigh"`, `web_search = "disabled"`,
  `model_auto_compact_token_limit = 999999999` (effectively disable
  auto-compaction so game-state turns aren't summarised mid-puzzle),
  `analytics.enabled = false`, `feedback.enabled = false`,
  `check_for_update_on_startup = false`, `disable_paste_burst = true`,
  `history.persistence = "none"`.
- **`[sandbox_workspace_write]`**: `network_access = false`, only the
  workspace's `results/` directory writable.
- **`[permissions.cwd-only]`**: filesystem read-only on cwd, network
  off (extra layer beyond the sandbox).
- **`[features]`**: 12 toggles all `false` (`apps`, `codex_hooks`,
  `fast_mode`, `memories`, `multi_agent`, `personality`,
  `prevent_idle_sleep`, `shell_snapshot`, `shell_tool`,
  `skill_mcp_dependency_install`, `undo`, `unified_exec`).
- **`[tools]`**: `view_image = false`, `web_search = false`.
- **`[apps._default]`**: all 3 toggles `false`.
- **`[memories]`**: `use_memories / generate_memories = false`.

Plus the dynamic per-puzzle block injected by `prep-session.sh`:

- **`[projects."<workspace>"] trust_level = "trusted"`** — pre-trusts
  the workspace folder so codex doesn't prompt on first launch.
- **`[mcp_servers.yugi-bench]`** — pinned `docker run` command,
  `enabled_tools = [...25 names...]` allowlist,
  `default_tools_approval_mode = "approve"`.
- **25 `[mcp_servers.yugi-bench.tools.<name>] approval_mode = "approve"`
  blocks** — explicit per-tool pre-approval (belt-and-braces with the
  default).

## Relaxing the lockdown

Edit `restrictions.toml.template`. Big-impact knobs:

| Toggle | Effect when relaxed | Benchmark risk |
|---|---|---|
| top-level `web_search = "disabled"` → `"cached"` / `"live"` | agent can search the web | **HIGH** — agent could google the puzzle's solution. Dominant signal-leakage path. |
| `[features] shell_tool = false` → `true` | agent gets host shell | **HIGH** — full read of your filesystem incl. `solutions/` and any local card-database notes |
| `[features] unified_exec = false` → `true` | same as above (PTY exec) | **HIGH** |
| `[tools] view_image = false` → `true` | agent can attach screenshots | low — minor unless you paste game-board images |
| `[memories] use_memories = false` → `true` | agent reuses memory across sessions | medium — earlier puzzles' state can leak into later puzzles' context |
| `approval_policy = "never"` → `"on-request"` | agent prompts for approvals | nuisance — breaks unattended runs |
| `sandbox_mode = "workspace-write"` → `"danger-full-access"` | sandbox off | high — agent can read/write anywhere |

For a different lockdown profile per batch, point the prep at an
alternate template:

```bash
./agent-mcp-eval/codex/prep-session.sh <id> \
    --restrictions-template /path/to/your-template.toml
```

## Auto-driven flow internals

`exec-launcher.sh` (which gets copied into each workspace as
`run-codex-exec.sh`) runs:

```bash
codex exec --cd "$PWD" --skip-git-repo-check --ephemeral "start"
```

- `--cd "$PWD"` anchors cwd so AGENTS.md auto-discovery and the
  in-config `[projects."<workspace>"]` trust block both resolve.
- `--skip-git-repo-check` — workspaces aren't git repos.
- `--ephemeral` — codex doesn't persist session rollout files (the
  canonical record is `results/<id>.jsonl` from the MCP container).
- `"start"` — matches AGENTS.md's "When the user says 'start', call
  `get_briefing` first..." trigger.

Combined stdout/stderr is teed to `<workspace>/codex-exec.log` for
post-mortem.

## Verified key references

All keys verified against the latest official docs as of May 2026:

- [Codex Configuration Reference](https://developers.openai.com/codex/config-reference)
- [Codex CLI command line options](https://developers.openai.com/codex/cli/reference)
- [Codex CLI: Non-interactive mode](https://developers.openai.com/codex/noninteractive)
- [Codex CLI: AGENTS.md guide](https://developers.openai.com/codex/guides/agents-md)
- [Codex CLI: Agent approvals & security](https://developers.openai.com/codex/agent-approvals-security)
- [Codex schema (main branch)](https://github.com/openai/codex/blob/main/codex-rs/core/config.schema.json)
