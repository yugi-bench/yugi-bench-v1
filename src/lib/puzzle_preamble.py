"""Puzzle preamble — the framing that's identical across both release modes.

Tells the model:
- This is a puzzle, not a real duel.
- The win condition is "win THIS turn, by any engine-recognised route".
- Ending the turn without winning is an automatic loss.
- The non-damage win conditions to watch for.

This text is mode-agnostic.  Mode-specific notes about inspection
tools, action batching, and attempts go in the mode tail produced by
``lib.prompt_builder``, not here.
"""

from __future__ import annotations


PUZZLE_PREAMBLE = """\
THIS IS A PUZZLE — READ CAREFULLY
- The puzzle is winnable on THIS TURN.  You must achieve a winning
  game state during the turn you are given — there is no "next turn"
  to plan for.  Ending your turn without having won IS AN AUTOMATIC
  LOSS, even if your opponent looks like they would lose on a future
  turn (e.g. empty deck, low LP).  Do NOT rely on future opponent
  deck-out, fatigue, or any "next turn" outcome — the win must
  register during YOUR turn.
- "Win" here means any legal win condition that the engine recognises
  on YOUR turn.  Common winning lines include:
    * Reducing opponent LP to 0 via battle damage and/or effect damage
      (burn cards, piercing effects, etc.)
    * Forcing the opponent to attempt a draw with an empty deck during
      YOUR turn — e.g. via a card effect that makes them draw, mills
      their last card, or otherwise triggers their deck-out check now,
      not on their next draw phase.
    * Assembling an instant-win condition (e.g. all five Exodia pieces
      in hand) during your turn.
    * Activating any "you win the duel" condition card whose effect
      resolves on your turn.
  The puzzle is solved the moment the engine emits MSG_WIN with you
  as winner — by any such route.  Damage is the most common path,
  but not the only one; check your hand and deck for non-damage win
  enablers before committing to a damage-only plan that falls short.
"""


COMMON_NOTATION = """\
COMMON CARD-SELECTION NOTATION
- `indices` are 0-based offsets into the `cards` list in the pending
  decision.
- Places are `{player, location, sequence}` triples.  Locations: 0x04=MZ
  (monster zone), 0x08=SZ (spell/trap zone).  Sequence: monster zone
  0..4 (main zones) or 5,6 (EMZ); spell/trap zones 0..4, pendulum 6,7.
- Positions: 1=face-up ATK, 2=face-down ATK (rare), 4=face-up DEF,
  8=face-down DEF.
"""


TASK_LINE = "## Task\nWin this puzzle THIS TURN.\n"
