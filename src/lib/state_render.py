"""State rendering — full omniscient vs visible-only.

Wraps ``engine.state.build_state`` (which gives the player-perspective
view, like a real game) and adds an OMNISCIENT mode that reveals
normally-hidden information:
- Your deck contents (in order, with full card names)
- Your extra deck contents
- Opponent's set spell/trap identities (face-up reveal)
- Opponent's set monster identities
- Opponent's hand contents (face-up reveal)
- Opponent's deck + extra deck contents

Used by:
- N-attempts bulk mode prompts (always omniscient — the model has to
  plan everything upfront)
- Fully interactive mode with ``--forage`` (omniscient as the initial
  state baseline)
- Fully interactive mode without ``--forage`` (visible-only — model
  gets only what the engine reveals as gameplay progresses)
"""

from __future__ import annotations

import json

from engine.core import (
    LOCATION_DECK,
    LOCATION_EXTRA,
    LOCATION_HAND,
    LOCATION_MZONE,
    LOCATION_SZONE,
    CardDB,
    OCGEngine,
)
from engine.harness import Harness
from engine.state import build_state


def _card_label(code: int, card_db: CardDB) -> str:
    info = card_db.get(int(code)) or {}
    name = info.get("name") or f"Card #{code}"
    return f"{name} (`{code}`)"


def _query_codes(engine: OCGEngine, con: int, loc: int) -> list[int]:
    """Return the list of card codes in a given location for a player."""
    try:
        cards = engine.query_location(con, loc) or []
    except Exception:
        return []
    out: list[int] = []
    for c in cards:
        if not isinstance(c, dict):
            continue
        code = c.get("code")
        if isinstance(code, int) and code > 0:
            out.append(code)
    return out


def render_omniscient_state(harness: Harness, card_db: CardDB) -> str:
    """Render the COMPLETE game state with all hidden info revealed.

    Includes player + opponent deck contents, extra deck contents,
    opponent face-down spell/trap identities, opponent hand if any.
    Built for puzzle-mode (where the engine isn't enforcing real-duel
    epistemic limits — the puzzle author chose what to expose).

    Output is a JSON code block under a ``## Game State (full)`` heading.
    """
    engine = harness.engine
    visible = build_state(harness, card_db, perspective=0, include_decision=False)

    # Annotate decks for both players, in the engine's stored order.
    deck_p0 = _query_codes(engine, 0, LOCATION_DECK)
    deck_p1 = _query_codes(engine, 1, LOCATION_DECK)
    extra_p0 = _query_codes(engine, 0, LOCATION_EXTRA)
    extra_p1 = _query_codes(engine, 1, LOCATION_EXTRA)
    hand_p1 = _query_codes(engine, 1, LOCATION_HAND)

    def _card_dicts(codes: list[int]) -> list[dict]:
        return [
            {"code": c, "name": (card_db.get(c) or {}).get("name", f"Card #{c}")} for c in codes
        ]

    # Reveal opponent's set spells/traps + set monsters by querying the
    # engine for their underlying card codes (visible state masks
    # face-down opp cards).
    opp = visible.get("opponent", {}) or {}
    for zone_key, loc in (("monster_zone", LOCATION_MZONE), ("spell_trap_zone", LOCATION_SZONE)):
        zones = opp.get(zone_key) or []
        for entry in zones:
            if not isinstance(entry, dict):
                continue
            seq = entry.get("zone_index") or entry.get("zone")
            if entry.get("empty") or seq is None:
                continue
            try:
                card = engine.query_card(1, loc, int(seq))
            except Exception:
                card = None
            if card and card.get("code"):
                code = int(card["code"])
                entry.setdefault("card", {})
                entry["card"]["code"] = code
                entry["card"]["name"] = (card_db.get(code) or {}).get("name", f"Card #{code}")
                entry["card"]["_revealed_from_face_down"] = True

    payload = {
        "player": visible.get("you") or visible.get("player") or {},
        "opponent": opp,
        "phase": visible.get("phase"),
        "turn_player": visible.get("turn_player"),
        "turn": visible.get("turn"),
        "decks_revealed": {
            "player": _card_dicts(deck_p0),
            "opponent": _card_dicts(deck_p1),
        },
        "extra_decks_revealed": {
            "player": _card_dicts(extra_p0),
            "opponent": _card_dicts(extra_p1),
        },
        "opponent_hand_revealed": _card_dicts(hand_p1),
    }
    rendered = json.dumps(payload, indent=2, default=str)
    return (
        "## Game State (full — all hidden info revealed)\n\n"
        "Both decks, both extra decks, opponent's hand, and opponent's "
        "set spell/trap identities are shown below.  This puzzle "
        "exposes them up front so you can plan complete sequences "
        "without guessing.\n\n"
        f"```json\n{rendered}\n```\n"
    )


def render_visible_state(harness: Harness, card_db: CardDB) -> str:
    """Render the player-perspective game state — real-duel visibility.

    Your hand + field shown in full; opponent's face-up cards visible;
    opponent's face-down cards shown as 'set spell/trap (unknown)';
    deck counts visible but not contents.  Identical to what
    ``engine.state.build_state`` already produces for ``perspective=0``.
    """
    state = build_state(harness, card_db, perspective=0, include_decision=False)
    rendered = json.dumps(state, indent=2, default=str)
    return (
        "## Game State (visible)\n\n"
        "What you can see right now in the duel.  Opponent's set cards "
        "are shown as anonymous markers — you do NOT know their "
        "identities until they activate or are otherwise revealed.  "
        "Your deck contents are NOT shown (count only).\n\n"
        f"```json\n{rendered}\n```\n"
    )
