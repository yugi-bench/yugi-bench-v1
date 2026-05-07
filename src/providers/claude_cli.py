"""Claude CLI tool-calling provider — drives the local ``claude`` binary.

Self-contained implementation of ``ToolCallingProvider`` that spawns
the locally-installed ``claude`` CLI as a subprocess for each
``respond()`` call.  Authenticates via OAuth (Max-subscription
credentials at ``~/.claude/.credentials.json``) so calls bill against
the subscription rather than per-token API credits.

Provider-specific concerns encapsulated here:

- All Claude Code built-in tools (Read/Edit/Write/Bash/etc.) are
  disabled via ``--tools ""``.  Settings, MCP servers, slash commands,
  and session persistence are all suppressed so the spawned model
  sees nothing of the calling environment.
- The system prompt is fully replaced with the interactive-mode system prompt
  + a custom output-format instruction telling the model to emit
  tool calls as fenced ``json`` blocks (since native tool_use is
  mediated by Claude Code's harness and not available here).
- Tool calls are extracted from assistant text by parsing fenced
  ```json blocks containing ``{"tool": ..., "args": ...}``.
- Per-respond() watchdog timeout (default 240s) kills wedged calls.
- Optional per-call stream-json log file under ``log_dir``.

This provider doesn't need any external SDK — just the ``claude``
binary on PATH (or passed via ``claude_bin=``).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from pathlib import Path
from typing import Any

from .base import (
    Message,
    ModelTurn,
    ToolCall,
    ToolCallingProvider,
    ToolSchema,
)

_CLI_OUTPUT_FORMAT_INSTRUCTION = """\
You communicate with the puzzle harness ONLY by emitting tool calls as
fenced JSON blocks in your reply.  Each tool call must look exactly like:

```json
{"tool": "<tool_name>", "args": {<arguments>}}
```

You may emit zero, one, or several such blocks per reply.  The harness
will dispatch them in order and feed the results back on the next turn.
Free-form prose between the blocks is allowed and ignored by the
harness — keep it short.  Do NOT use any other tool-call syntax; the
harness parses ```json fenced blocks only.

The full list of tools you may call follows:
"""


_FENCED_JSON_RE = re.compile(r"```(?:json|JSON)?\s*\n(.*?)\n\s*```", re.DOTALL)


def _render_tools_for_cli(tools: list[ToolSchema]) -> str:
    """Render tool schemas as readable text for the CLI system prompt."""
    lines: list[str] = []
    for tool in tools:
        lines.append(f"### {tool['name']}")
        desc = (tool.get("description") or "").strip()
        if desc:
            lines.append(desc)
        schema = tool.get("input_schema") or {}
        lines.append("input schema: " + json.dumps(schema))
        lines.append("")
    return "\n".join(lines)


def _render_messages_for_cli(messages: list[Message]) -> str:
    """Roll up the LCD message history into a single text scroll.

    The CLI receives one user message via stdin (stream-json input).
    That message contains the entire conversation so far + the latest
    observation, framed so the model can read it.
    """
    chunks: list[str] = []
    for m in messages:
        role = m["role"]
        if role == "user":
            chunks.append("[OBSERVATION]\n" + str(m["content"]))
        elif role == "assistant":
            text = (m.get("text") or "").strip()
            tcs = m.get("tool_calls") or []
            block = ["[YOUR PREVIOUS REPLY]"]
            if text:
                block.append(text)
            for tc in tcs:
                block.append("```json")
                block.append(
                    json.dumps(
                        {
                            "tool": tc["name"],
                            "args": tc.get("arguments", {}),
                        }
                    )
                )
                block.append("```")
            chunks.append("\n".join(block))
        elif role == "tool":
            tag = "[TOOL ERROR]" if m.get("is_error") else "[TOOL RESULT]"
            chunks.append(f"{tag} call_id={m['tool_call_id']}\n{m['content']}")
    chunks.append(
        "Reply with your next move(s) as one or more ```json fenced blocks "
        "matching the tool schema.  Stop after submitting a response tool — "
        "the next turn will give you fresh observation."
    )
    return "\n\n".join(chunks)


def _extract_tool_calls_from_text(text: str) -> list[ToolCall]:
    """Parse fenced ```json {tool, args} blocks into ToolCall objects."""
    out: list[ToolCall] = []
    for match in _FENCED_JSON_RE.finditer(text):
        body = match.group(1).strip()
        try:
            obj = json.loads(body)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        name = obj.get("tool")
        if not isinstance(name, str) or not name:
            continue
        args = obj.get("args", {})
        if not isinstance(args, dict):
            args = {}
        out.append(
            ToolCall(
                id=f"call_{uuid.uuid4().hex[:12]}",
                name=name,
                arguments=args,
            )
        )
    return out


class ClaudeCLIToolProvider(ToolCallingProvider):
    """Drives the local ``claude`` CLI; bills against Max subscription.

    Constructor parameters:

    - ``model``: model alias (default ``"claude-opus-4-7"``).
    - ``max_tokens`` / ``temperature``: surfaced for parity with other
      providers; the CLI doesn't expose these directly.
    - ``claude_bin``: path to the ``claude`` binary (default: search PATH).
    - ``timeout_seconds``: per-respond() watchdog (default 240s).
    - ``log_dir``: optional directory for per-call stream-json logs.
    - ``extra_flags``: additional argv flags to pass to ``claude``.
    """

    name = "claude-cli"

    def __init__(
        self,
        model: str = "claude-opus-4-7",
        max_tokens: int = 4096,
        temperature: float = 0.0,
        claude_bin: str | None = None,
        timeout_seconds: int = 240,
        log_dir: Path | None = None,
        extra_flags: tuple[str, ...] = (),
    ):
        bin_path = claude_bin or shutil.which("claude")
        if not bin_path:
            raise RuntimeError(
                "`claude` binary not found on PATH.  Install Claude Code or pass claude_bin=<path>."
            )
        self.claude_bin = bin_path
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.timeout_seconds = timeout_seconds
        self.log_dir = log_dir
        self.extra_flags = tuple(extra_flags)
        if self.log_dir is not None:
            self.log_dir.mkdir(parents=True, exist_ok=True)

    def provider_config_for_log(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "claude_bin": self.claude_bin,
            "timeout_seconds": self.timeout_seconds,
        }

    def _build_system_prompt(self, base_system: str, tools: list[ToolSchema]) -> str:
        return (
            base_system.rstrip()
            + "\n\n"
            + _CLI_OUTPUT_FORMAT_INSTRUCTION
            + _render_tools_for_cli(tools)
        )

    def _build_argv(self, system_prompt: str) -> list[str]:
        argv = [
            self.claude_bin,
            "--print",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            self.model,
            "--system-prompt",
            system_prompt,
            "--tools",
            "",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--dangerously-skip-permissions",
        ]
        argv.extend(self.extra_flags)
        return argv

    def respond(
        self,
        system: str,
        messages: list[Message],
        tools: list[ToolSchema],
    ) -> ModelTurn:
        import time

        full_system = self._build_system_prompt(system, tools)
        user_text = _render_messages_for_cli(messages)
        argv = self._build_argv(full_system)
        run_id = uuid.uuid4().hex[:12]
        run_log_path: Path | None = None
        if self.log_dir is not None:
            run_log_path = self.log_dir / f"{run_id}.jsonl"
        cwd = "/tmp"
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUDE_CODE_")}
        env.pop("ANTHROPIC_API_KEY", None)
        user_turn = {"type": "user", "message": {"role": "user", "content": user_text}}

        t_start = time.time()
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise RuntimeError(f"failed to launch claude CLI: {exc}") from exc

        assistant_text_parts: list[str] = []
        stop_reason = ""
        rate_limited = False
        events_seen = 0

        try:
            assert proc.stdin is not None and proc.stdout is not None
            proc.stdin.write(json.dumps(user_turn) + "\n")
            proc.stdin.flush()
            proc.stdin.close()

            stdout_lines: list[str] = []
            stderr_chunks: list[str] = []
            stderr_done = threading.Event()

            def _drain_stderr() -> None:
                assert proc.stderr is not None
                for chunk in proc.stderr:
                    stderr_chunks.append(chunk)
                stderr_done.set()

            stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
            stderr_thread.start()

            killed = {"hit": False}

            def _kill_on_timeout() -> None:
                killed["hit"] = True
                try:
                    proc.kill()
                except Exception:
                    pass

            watchdog = threading.Timer(self.timeout_seconds, _kill_on_timeout)
            watchdog.daemon = True
            watchdog.start()

            try:
                for line in proc.stdout:
                    line = line.rstrip("\n")
                    if not line:
                        continue
                    stdout_lines.append(line)
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    events_seen += 1
                    rtype = record.get("type")
                    if rtype == "assistant":
                        msg = record.get("message", {})
                        for block in msg.get("content", []):
                            if block.get("type") == "text":
                                assistant_text_parts.append(block.get("text", ""))
                            elif block.get("type") == "tool_use":
                                assistant_text_parts.append(
                                    f"\n[unexpected tool_use: {block.get('name')}]\n"
                                )
                    elif rtype == "result":
                        stop_reason = record.get("stop_reason") or stop_reason
                        if record.get("is_error"):
                            err_str = str(record.get("error", "")).lower()
                            if any(t in err_str for t in ("rate", "429", "quota", "overload")):
                                rate_limited = True
                    elif rtype == "error":
                        err = str(record).lower()
                        if any(
                            t in err
                            for t in (
                                "rate_limit",
                                "rate limit",
                                "overloaded",
                                "429",
                                "too many requests",
                            )
                        ):
                            rate_limited = True
            finally:
                watchdog.cancel()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
                stderr_thread.join(timeout=5)

            stderr_text = "".join(stderr_chunks)
            if run_log_path is not None:
                try:
                    with run_log_path.open("w") as f:
                        f.write("\n".join(stdout_lines))
                        if stderr_text:
                            f.write("\n--- STDERR ---\n" + stderr_text)
                except Exception:
                    pass

            if killed["hit"]:
                raise RuntimeError(
                    f"claude CLI watchdog hit at {self.timeout_seconds}s "
                    f"(events={events_seen}); stderr tail: "
                    f"{stderr_text[-500:]!r}"
                )
            if proc.returncode != 0 and not assistant_text_parts:
                stderr_lower = stderr_text.lower()
                if any(t in stderr_lower for t in ("rate", "429", "quota", "overload")):
                    rate_limited = True
                raise RuntimeError(
                    f"claude CLI exited {proc.returncode} with no assistant "
                    f"output; stderr tail: {stderr_text[-1000:]!r}"
                )
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
            raise

        wallclock = time.time() - t_start
        text = "".join(assistant_text_parts)
        tool_calls = _extract_tool_calls_from_text(text)

        return ModelTurn(
            text=text,
            tool_calls=tool_calls,
            stop_reason=stop_reason,
            raw={
                "events": events_seen,
                "rate_limited": rate_limited,
                "log_path": str(run_log_path) if run_log_path else None,
            },
            wallclock_seconds=wallclock,
            provider_data={
                "events_seen": events_seen,
                "rate_limited": rate_limited,
                "log_path": str(run_log_path) if run_log_path else None,
            },
        )
