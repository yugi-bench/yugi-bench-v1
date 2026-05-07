# Claude Code variant

Use Claude Code as the agent driver, billed against your Claude
Pro/Max subscription. Same one-shot puzzle-play behaviour as the
codex variant.

## Files in this directory

- **`prep-session.sh`** — generates one isolated workspace under
  `~/yugi-bench-runs/<id>-<ts>/`. Invoked by the top-level
  `./agent-mcp-eval/claude/prep-batch.sh`.
- **`exec-launcher.sh`** — copied into each workspace as
  `run-claude-exec.sh`. Auto-driven equivalent of the manual flow:
  `claude --strict-mcp-config --mcp-config ./.mcp.json --print "start"`.
  **Best-effort** — see "Caveats" below.

## Workspace layout

```
~/yugi-bench-runs/<id>-<ts>/
├── CLAUDE.md                       (Claude Code auto-discovers from cwd)
├── .mcp.json                       (MCP server pinned to this puzzle)
├── .claude/
│   └── settings.json               (project-local lockdown overrides)
├── metadata.json                   (puzzle_id, image, flags, agent="claude")
├── results/                        (bind-mounted into container)
│   └── <puzzle_id>.jsonl
└── run-claude-exec.sh              (auto-driven launcher; see caveats)
```

## Two equivalent flows

**Manual paste flow:**

```bash
cd ~/yugi-bench-runs/<id>-<ts>/
claude --strict-mcp-config --mcp-config ./.mcp.json
# type: start
# (close session when _outcome lands)
```

**Auto-driven flow (single workspace):**

```bash
bash ~/yugi-bench-runs/<id>-<ts>/run-claude-exec.sh
```

**Auto-driven flow (whole queue):**

```bash
./agent-mcp-eval/claude/run-batch.py
```

## Authentication

For interactive `claude` sessions, the standard one-time login works:

```bash
claude              # if not yet authenticated, opens browser for OAuth
claude auth login   # explicit equivalent
```

For the **auto-driven `claude --print` flow** there's a wrinkle: the
spawned subprocess can't always reach the Mac Keychain in non-interactive
contexts (observed "Not logged in" despite an interactive `claude`
session working). The platform-uniform fix is to use a long-lived OAuth
token via env var, which sits at #5 in claude's auth-precedence chain
(above the Keychain/file at #6) and works in any subprocess context:

```bash
# One-time setup:
claude setup-token                              # prints a 1-year OAuth token
export CLAUDE_CODE_OAUTH_TOKEN=<token>          # add to ~/.zshrc / ~/.bashrc

# Re-run prep so the token is baked into the workspace settings.json:
./agent-mcp-eval/claude/prep-batch.sh --count N
```

`prep-session.sh` reads `$CLAUDE_CODE_OAUTH_TOKEN` at prep time and adds
it to the workspace's `.claude/settings.json` `env` block, so the
workspace is **self-contained** for batch runs — you don't need to keep
the env var exported in every shell that launches a session. This is
the Claude analogue of the codex side's symlinking
`~/.codex/auth.json` into per-workspace `.codex/auth.json` under
`CODEX_HOME`.

`prep-session.sh` prints an `auth:` line on its stdout that makes the
detected auth path explicit:
- `auth: CLAUDE_CODE_OAUTH_TOKEN baked into settings.json env (self-contained)`
- `auth: relying on ~/.claude/.credentials.json (Linux/Windows default)`
- `auth: WARNING no obvious auth path detected. ...`

Verify on the next run-claude-exec by checking that `claude-exec.log`
doesn't contain `"text":"Not logged in"` in the assistant message —
that's the failure signature.

## The lockdown profile

`prep-session.sh` writes `.claude/settings.json` per workspace —
the Claude-side parity profile to codex's `restrictions.toml.template`.
Verified against the docs at
[code.claude.com/docs/en/{settings,permissions,permission-modes,env-vars,cli-reference}](https://code.claude.com/docs/en/settings)
as of 2026-05-07.

```json
{
  "autoMemoryEnabled": false,
  "effortLevel": "xhigh",
  "disableAllHooks": true,
  "enableAllProjectMcpServers": false,
  "includeCoAuthoredBy": false,
  "permissions": {
    "defaultMode": "dontAsk",
    "allow": ["mcp__yugi-bench__*", "ToolSearch"],
    "deny": [
      "Bash", "BashOutput", "KillShell",
      "Read", "Write", "Edit", "MultiEdit", "NotebookEdit",
      "Glob", "Grep",
      "Task", "Agent",
      "WebFetch", "WebSearch",
      "TodoWrite",
      "PowerShell"
    ]
  },
  "env": {
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "CLAUDE_CODE_SKIP_PROMPT_HISTORY": "1",
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_ATTACHMENTS": "1",
    "CLAUDE_CODE_DISABLE_BACKGROUND_TASKS": "1",
    "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
    "CLAUDE_CODE_DISABLE_FAST_MODE": "1",
    "CLAUDE_CODE_DISABLE_FILE_CHECKPOINTING": "1",
    "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
    "DISABLE_COMPACT": "1",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "1000000"
  }
}
```

The launcher pairs this with CLI flags:
`claude --strict-mcp-config --mcp-config ./.mcp.json --print --output-format stream-json --verbose --permission-mode dontAsk "$PROMPT"`.

### Why `dontAsk` not `bypassPermissions`

Per the [permission-modes docs](https://code.claude.com/docs/en/permission-modes),
`bypassPermissions` "skips the permission layer entirely" — meaning
both the deny *and* allow rules are ignored. That's wrong for a
lockdown: it leaves the agent with the full builtin toolset (Bash,
Read, Edit, Task, …) running unprompted. `dontAsk` is the correct
"auto-DENY everything not pre-approved" mode; under it, only tools
matching `permissions.allow` execute, and explicit `ask` rules are
denied rather than prompting.  Read-only Bash commands and reads in
the working directory remain auto-allowed by a built-in carve-out;
this is unconfigurable but harmless for puzzle play (engine state
lives in the MCP container, not in any host-reachable file).

### Parity matrix vs codex

| Concern | Codex | Claude | Status |
|---|---|---|---|
| Skip approval prompts | `approval_policy = "never"` + 25 per-tool `approval_mode = "approve"` | `permissions.defaultMode = "dontAsk"` | ✓ full |
| Tool allowlist (MCP-only) | `[mcp_servers.yugi-bench].enabled_tools = [...25...]` | `permissions.allow = ["mcp__yugi-bench__*", "ToolSearch"]` + explicit `deny` of all dangerous builtins | ✓ full (ToolSearch carve-out is required for `--print` mode schema-recovery) |
| Web search blocked | `web_search = "disabled"` + `[tools].web_search = false` | implicit via `dontAsk` (`WebSearch` / `WebFetch` not in allow list) | ✓ full |
| Memory subsystem off | `[features].memories=false` + `[memories]` block | `autoMemoryEnabled: false` + `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` | ✓ full |
| Max reasoning effort | `model_reasoning_effort = "xhigh"` | `effortLevel: "xhigh"` | ✓ full |
| Hooks disabled | `[features].codex_hooks = false` | `disableAllHooks: true` | ✓ full |
| Image / view-image off | `[tools].view_image = false` | `CLAUDE_CODE_DISABLE_ATTACHMENTS=1` | ✓ full |
| Background / fast / sleep features | `[features]` toggles for `fast_mode`, `prevent_idle_sleep`, `shell_snapshot` | `CLAUDE_CODE_DISABLE_FAST_MODE/_BACKGROUND_TASKS/_FILE_CHECKPOINTING=1` | ✓ full |
| Analytics / feedback / update-check | `analytics.enabled = false` + `feedback.enabled = false` + `check_for_update_on_startup = false` | `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1` (sets `DISABLE_AUTOUPDATER` + `DISABLE_FEEDBACK_COMMAND` + `DISABLE_TELEMETRY` + `DISABLE_ERROR_REPORTING`) | ✓ full |
| Ephemeral / no transcript | `--ephemeral` + `history.persistence = "none"` | `CLAUDE_CODE_SKIP_PROMPT_HISTORY=1` | ✓ full |
| Strict MCP scope | per-workspace `.codex/config.toml` + `CODEX_HOME=$PWD/.codex` | `--strict-mcp-config --mcp-config ./.mcp.json` + `enableAllProjectMcpServers: false` | ✓ full |
| Auto-compact disabled | `model_auto_compact_token_limit = 999999999` | `DISABLE_COMPACT=1` | ✓ full |
| Context window override | `model_context_window = 256000` | `CLAUDE_CODE_MAX_CONTEXT_TOKENS = 256000` (per [env-vars docs](https://code.claude.com/docs/en/env-vars), only takes effect when `DISABLE_COMPACT=1` is also set) | ✓ full |
| MCP container isolation | `docker run --network none --memory 1g` | identical | ✓ full |
| **Agent-level FS sandbox** | `sandbox_mode = "workspace-write"` + `[permissions.cwd-only.filesystem]` | **launcher passes `--tools "ToolSearch"`** which strips every Claude builtin (Bash, Read, Edit, Glob, Grep, Task, WebFetch, WebSearch, …) from the agent's tool surface entirely. MCP tools always pass through. The agent literally cannot see Bash exists; no read-only-Bash carve-out can fire, no `cat solutions/...` path. `permissions.deny` block stays as belt-and-braces. ToolSearch stays because the agent uses it for MCP-schema recovery in `--print` mode | ✓ full (via `--tools` flag, not via OS-level sandbox; equivalently strong for benchmark integrity since Bash is invisible) |
| **Agent-level network egress** | `[sandbox_workspace_write].network_access = false` | **none** at the agent level — Claude Code's `sandbox` block restricts the *Bash subprocess*, not the agent process itself | ✗ partial — for hard egress isolation, run the launcher inside a VM/container with iptables drop rules |

The two ✗ entries are **structural gaps in Claude Code itself**, not
fixable from settings.json. For the puzzle benchmark these are
mitigated in practice:
1. Engine state lives in the MCP container (which already has
   `--network none`), not in any host-reachable file. Reading
   `solutions/<id>.json` from the host would be cheating but the
   agent has no reason to and the puzzle doctrine in `CLAUDE.md`
   doesn't surface that path.
2. The agent's reasoning-side network reach is its OAuth call back
   to api.anthropic.com — that's expected; further egress would
   need to go through a builtin tool that's already denied.

If you want hard isolation matching codex, run the per-puzzle
launcher inside a VM/container with iptables egress drop rules for
everything except `api.anthropic.com:443`.

## Relaxing the lockdown

The Claude variant doesn't have a separate restrictions template — the
lockdown is generated inline by `prep-session.sh`. To customize, edit
the `settings = {...}` dict in the python heredoc inside
`prep-session.sh`. Big-impact knobs:

| Toggle | Effect when relaxed | Benchmark risk |
|---|---|---|
| `permissions.allow` adds `WebSearch` / `WebFetch` | agent can google solutions / fetch arbitrary URLs | **HIGH** — dominant signal leakage |
| `permissions.defaultMode: "default"` instead of `"dontAsk"` | every tool prompts for approval; in `--print` mode that aborts the run | breaks auto-driven flow |
| `permissions.defaultMode: "bypassPermissions"` instead of `"dontAsk"` | agent has full builtin toolset (Bash, Read, Edit, Task) without prompting | medium-high — agent could read `solutions/<id>.json`, etc. |
| `autoMemoryEnabled: true` | memory carries across sessions | medium — earlier puzzles can leak |
| `DISABLE_COMPACT` removed from env | claude auto-compacts mid-puzzle | medium — turns get summarised, may degrade play |
| `effortLevel: "low"` | minimum reasoning | low — just makes the agent worse |

## Caveats — auto-driven flow

`claude --print` historically misbehaved with MCP servers per the
2026-05-04 finding. The failure was traced to MCP-tool approval
prompts being silently cancelled in non-interactive contexts.

The current lockdown profile closes that gap on three layers:
1. `permissions.defaultMode: "dontAsk"` + `permissions.allow:
   ["mcp__yugi-bench__*"]` pre-approves the MCP tools so no approval
   prompt fires; non-MCP tools auto-deny.
2. `--permission-mode dontAsk` on the CLI as belt-and-braces.
3. `--strict-mcp-config --mcp-config ./.mcp.json` locks the MCP
   server allowlist to one puzzle.

So the historical failure mode SHOULD be structurally impossible. But
this hasn't been re-verified against the current Claude Code release.
**Verify on a single workspace before scaling up to a batch run.** If
`run-claude-exec.sh` still misbehaves, fall back to the manual paste
flow — it's known-working.

## Reference

- [Claude Code settings reference](https://code.claude.com/docs/en/settings)
- [Permissions](https://code.claude.com/docs/en/permissions)
- [Permission modes](https://code.claude.com/docs/en/permission-modes)
- [Environment variables](https://code.claude.com/docs/en/env-vars)
- [CLI reference](https://code.claude.com/docs/en/cli-reference)
