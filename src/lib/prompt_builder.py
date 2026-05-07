"""Universal prompt builder — single source of truth for both release modes.

Two entry points:

* ``build_bulk_prompt(instance, *, attempts, ...)``: n-attempts bulk
  mode prompt (one-shot when N=1, retry-loop variant when N>1).
  Returns a single user-message string.
* ``build_interactive_system_prompt(instance, *, forage, ...)``:
  fully interactive mode system prompt.  Returns a single string.

Both consume the same snippet library (``lib.puzzle_preamble``,
``lib.grammar``, ``lib.glossary``, ``lib.state_render``) and select
which sections to include based on the mode + flags.

Section order:
1. Puzzle preamble (preamble.PUZZLE_PREAMBLE)
2. Rules reference (carved from instance['prompt'] prefix)
3. Game state (omniscient for bulk + forage; visible-only for
   interactive no-forage)
4. Card glossary (full for bulk + forage; seen-only for interactive
   no-forage — but for the initial system prompt, "seen" = whatever's
   visible at puzzle start, so a small starter set)
5. Action grammar (mode-specific: bulk vs interactive)
6. Solution walkthrough (only if show_solution=True; this is a
   ceiling-test mode that gives the model the answer)
7. Mode tail (attempts count, restart tool, forage tools)
8. Retry context (only for bulk attempt_index > 0)
9. Task line ("win this puzzle THIS turn")
"""

from __future__ import annotations

import json

from engine.core import CardDB
from engine.harness import Harness

from . import grammar, puzzle_preamble
from .glossary import (
    render_full_glossary,
)
from .state_render import render_omniscient_state

# ---------------------------------------------------------------------------
# Carving the rules reference out of instance['prompt']
# ---------------------------------------------------------------------------

_GAME_STATE_HEADING = "## Game State"
_LEGACY_ACTION_HEADING = "## Action Schema"
_TASK_HEADING = "## Task"


def carve_rules_reference(raw_prompt: str) -> str:
    """Return the dataset prompt's rules-reference prefix only.

    Drops the `## Game State`, `## Card Details`, `## Action Schema`,
    `## Task`, and worked-example sections — those are replaced by
    lib's own (richer) versions.
    """
    for marker in (_GAME_STATE_HEADING, _LEGACY_ACTION_HEADING, _TASK_HEADING):
        idx = raw_prompt.find(marker)
        if idx != -1:
            return raw_prompt[:idx].rstrip()
    return raw_prompt.rstrip()


# ---------------------------------------------------------------------------
# Optional sections
# ---------------------------------------------------------------------------


def _solution_walkthrough_section(instance: dict) -> str:
    steps = instance.get("gold_solution_steps") or []
    if not steps:
        return ""
    body = "\n".join(f"{i + 1}. {step}" for i, step in enumerate(steps))
    return (
        "## Solution Walkthrough  *(--show-solution mode)*\n\n"
        "**You have been given the puzzle's gold solution as a "
        "natural-language walkthrough.**  Use it as guidance for "
        "translating the high-level intent into the engine's "
        "pending-decision sequence — but the *low-level execution* "
        "(correct indices, chain windows, place/position selection, "
        "etc.) is still up to you.\n\n"
        f"{body}\n"
    )


def _retry_context(attempt_index: int, last_failure: dict | None) -> str:
    if attempt_index <= 0 or not last_failure:
        return ""
    # Parse-error variant: the previous response could not be parsed
    # into an action list (the model emitted JSON-like content but it
    # was malformed, or the wrapper format wasn't followed). Tell the
    # model what went wrong format-wise; engine state is irrelevant
    # here because no actions were applied.
    if last_failure.get("parse_error"):
        last_failure.get("previous_text_preview", "")
        return (
            f"## Retry — Attempt {attempt_index + 1}\n\n"
            f"Your previous response could not be parsed as a JSON "
            f"action list.  The action evaluator expects either:\n\n"
            f"  * a JSON array wrapped in `<solution>...</solution>` "
            f"tags (preferred), or\n"
            f"  * a JSON array inside a fenced ```json``` block.\n\n"
            f"Either the wrapper was missing or the JSON inside it "
            f"didn't parse.  Re-emit your full action sequence in the "
            f"requested format.  The engine has not advanced — submit "
            f"a fresh complete solution from the puzzle's initial "
            f"pending decision.\n"
        )
    err = last_failure.get("error") or last_failure.get("status") or "unknown"
    failed_at = last_failure.get("failed_at_index")
    pending = last_failure.get("pending_after") or last_failure.get("pending")
    pending_txt = (
        json.dumps(pending, indent=2, default=str)
        if pending
        else "(no pending — duel ended without a win)"
    )
    failed_at_line = (
        f"Your previous submission failed at action index {failed_at}.\n\n"
        if failed_at is not None
        else ""
    )
    return (
        f"## Retry — Attempt {attempt_index + 1}\n\n"
        f"{failed_at_line}"
        f"Failure reason:\n\n"
        f"    {err}\n\n"
        f"The engine's pending decision at the point of failure was:\n\n"
        f"```json\n{pending_txt}\n```\n\n"
        f"The engine resets between attempts.  Submit a fresh complete "
        f"solution from the puzzle's initial pending decision — do NOT "
        f"assume anything from the failed attempt has taken effect.\n"
    )


# ---------------------------------------------------------------------------
# Mode-tail variants
# ---------------------------------------------------------------------------


def _mode_tail_bulk(attempts: int) -> str:
    if attempts == 1:
        return (
            "## How To Submit\n\n"
            "You have **one** attempt.  Output your final answer as a "
            "JSON array inside `<solution>` tags.  The evaluator runs "
            "your list against a fresh engine; you win iff the engine "
            "emits MSG_WIN with you as winner before your list runs "
            "out or any action is rejected.\n"
        )
    return (
        "## How To Submit\n\n"
        f"You have up to **{attempts} attempts**.  Output your answer "
        f"as a JSON array inside `<solution>` tags.  If your action list "
        f"errors or runs out before MSG_WIN, you'll be told the failure "
        f"reason + the engine's pending decision at the failure point, "
        f"then asked to submit a fresh complete list (the engine resets "
        f"between attempts).  You're scored on whether ANY of your "
        f"attempts wins.\n"
    )


def _mode_tail_interactive(forage: bool, restart_always: bool = True) -> str:
    if forage:
        forage_section = (
            "\n"
            "### Forage mode — gather information via inspection tools\n"
            "\n"
            "This run is in **--forage** mode.  The system prompt above "
            "deliberately does NOT contain a state dump or a card "
            "glossary.  You have all four read-only inspection tools to "
            "forage for what you need:\n"
            "- `get_state` — full current state snapshot, including "
            "your deck contents, opponent's set spell/trap identities, "
            "opponent's hand, etc.\n"
            "- `pending_decision` — current pending decision in detail.\n"
            "- `inspect_card` — look up any card by its 8-digit code "
            "(full effect text + ATK/DEF/level/race/attribute).\n"
            "- `get_glossary` — engine enums (location bitmasks, "
            "message type IDs, etc.).\n"
            "\n"
            "Inspection tools do not advance the game; they only read "
            "state.  Use them as needed to gather the information "
            "you'd use to plan.\n"
        )
    else:
        forage_section = (
            "\n"
            "### Inspection tools (default mode)\n"
            "\n"
            "The system prompt above contains the FULL omniscient game "
            "state (your deck contents, opponent's set spell/trap "
            "identities, opponent's hand if any) AND the FULL card "
            "glossary (every card in the puzzle, with effect text + "
            "stats), so you don't need to look any of that up.  Three "
            "inspection tools are still available:\n"
            "- `get_state` — full current state snapshot.  Useful when "
            "you want to ground yourself in the actual current state "
            "rather than your own running mental model — particularly "
            "after a long exchange.\n"
            "- `pending_decision` — current pending decision in detail.  "
            "Useful when the per-turn observation summary isn't enough.\n"
            "- `get_glossary` — one-shot dump of every OCG engine enum "
            "(position / location / phase / attribute / race / type-flag "
            "/ status-flag / move-reason / link-marker / win-reason "
            "codes).  The notation block below covers a subset; call "
            "`get_glossary` once at the start if you want the full "
            "decoder for raw numeric fields you might see in events.\n"
            "\n"
            "`inspect_card` is NOT provided in this mode — every card "
            "in the puzzle is already documented in the glossary above.  "
            "Re-run with `--forage` if you want to test the harder mode "
            "where card lookups must be earned via tool calls.\n"
        )

    restart_section = (
        (
            "\n"
            "### Restart\n"
            "\n"
            "If you make an irrecoverable mistake or get genuinely stuck, "
            "call `restart` to reset the puzzle to its initial conditions.  "
            "Conversation history is preserved (so you can learn from prior "
            "attempts) but the engine is rebuilt from scratch.  The "
            "tool-call budget keeps ticking — restart is not free.\n"
        )
        if restart_always
        else ""
    )

    return (
        "## How To Play (interactive)\n\n"
        "The engine drives the duel and halts at each pending decision.  "
        "Each pending decision names the exact response verb you must "
        "call (e.g. `select_chain`, `select_idlecmd`).  Reply with one "
        "or more tool_use blocks per turn:\n"
        "\n"
        "- **Single-action turn**: one tool_use per response — the "
        "engine processes it, the next pending decision comes back to "
        "you.  Use this when the next decision genuinely needs thought.\n"
        "- **Multi-action turn**: several tool_use blocks per response "
        "— the harness dispatches them sequentially.  **Chain windows "
        "between your batched actions are auto-declined** unless you "
        "include an explicit `select_chain` action targeting that "
        "window.  Use this to script deterministic sequences (summon "
        "→ place → set position) without writing trivial chain "
        "declines.\n"
        f"{forage_section}"
        f"{restart_section}"
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_bulk_prompt(
    *,
    instance: dict,
    card_db: CardDB,
    harness: Harness,
    attempts: int = 1,
    show_solution: bool = False,
    attempt_index: int = 0,
    last_failure: dict | None = None,
) -> str:
    """Build the user-message prompt for n-attempts bulk mode.

    ``harness`` must be a freshly-initialised Harness on the puzzle's
    initial state — the caller spins one up via
    ``harness.start(instance['lua_setup'])`` first so the omniscient
    state renderer can query the live engine for deck contents etc.
    """
    sections: list[str] = []
    sections.append(puzzle_preamble.PUZZLE_PREAMBLE)
    sections.append(carve_rules_reference(instance.get("prompt", "")))
    sections.append(render_omniscient_state(harness, card_db))
    sections.append(render_full_glossary(instance, card_db))
    sections.append(grammar.render_action_grammar("bulk"))
    if show_solution:
        sections.append(_solution_walkthrough_section(instance))
    sections.append(_mode_tail_bulk(attempts))
    sections.append(_retry_context(attempt_index, last_failure))
    sections.append(puzzle_preamble.TASK_LINE)
    return "\n\n".join(s.strip() for s in sections if s and s.strip()) + "\n"


def build_interactive_system_prompt(
    *,
    instance: dict,
    card_db: CardDB,
    harness: Harness,
    forage: bool = False,
    show_solution: bool = False,
) -> str:
    """Build the system prompt for fully interactive mode.

    Without ``--forage`` (the default) the system prompt is RICH: full
    omniscient state (deck contents revealed, opp set spell/trap
    identities revealed) + full card glossary for every card in the
    puzzle.  The model has no inspection tools — everything it could
    look up is already in the prompt.

    With ``--forage`` the system prompt is LEAN: no state JSON, no
    card glossary.  The model is given inspection tools (`get_state`,
    `pending_decision`, `inspect_card`, `get_glossary`) and is
    expected to FORAGE for information by calling them — testing
    agentic information-gathering capability.  Per-turn observations
    still carry the visible game state in both modes.
    """
    sections: list[str] = []
    sections.append(puzzle_preamble.PUZZLE_PREAMBLE)
    sections.append(carve_rules_reference(instance.get("prompt", "")))
    if not forage:
        # Default mode: hand the model everything up front.
        sections.append(render_omniscient_state(harness, card_db))
        sections.append(render_full_glossary(instance, card_db))
    # In forage mode the system prompt deliberately omits the state +
    # glossary; the model uses inspection tools to gather what it
    # needs.  Per-turn observations (built by the runner) still carry
    # the current visible state in both modes.
    sections.append(grammar.render_action_grammar("interactive"))
    if show_solution:
        sections.append(_solution_walkthrough_section(instance))
    sections.append(_mode_tail_interactive(forage=forage, restart_always=True))
    sections.append(puzzle_preamble.COMMON_NOTATION)
    sections.append(puzzle_preamble.TASK_LINE)
    return "\n\n".join(s.strip() for s in sections if s and s.strip()) + "\n"
