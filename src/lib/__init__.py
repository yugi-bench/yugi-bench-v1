"""Shared prompt-building library used by both release modes.

This module is the single source of truth for:

* The puzzle preamble — the "this is a puzzle, win this turn" framing.
* The action grammar — the response-verb sequence patterns Yu-Gi-Oh
  decisions decompose into.
* The card glossary — auto-rendered oracle text for every card_id
  that appears in the puzzle. N-attempts bulk mode and fully
  interactive no-forage mode get the full glossary; fully interactive
  ``--forage`` mode strips it and exposes inspection tools instead.
* The state renderer — full omniscient state vs visible-only state.
* The prompt builder — assembles snippets into one prompt according
  to the (mode, attempts, forage, show_solution) configuration.

The unified ``runner.py`` consumes from here for both the bulk path
(``runner.py --attempts N``) and the interactive path
(``runner.py --interactive``); behaviour differences across modes
live in this module's mode argument, not in per-mode prompts.
"""
