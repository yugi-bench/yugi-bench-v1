"""Tool schemas for fully interactive mode LLM tool-use.

Provider-agnostic JSON Schema — the ``tools`` list is consumable as-is by
Anthropic's `tools=[...]` and OpenAI's `tools=[{"type":"function","function":...}]`
(with a tiny wrapper).  Each response tool maps 1:1 to a ``Harness.respond_*``
method.  Three inspection tools (``get_state``, ``pending_decision``,
``inspect_card``) surface state without mutating anything.

Dispatch is trivial: ``getattr(harness, TOOL_TO_METHOD[name])(**kwargs)``.
Tool names match the harness method names (minus the ``respond_`` prefix)
so the map is mechanical.
"""
from __future__ import annotations

from typing import Any


# ---------------------------------------------------------------------------
# Inspection tools — read-only.
# ---------------------------------------------------------------------------

INSPECTION_TOOLS: list[dict[str, Any]] = [
    {
        "name": "get_state",
        "description": (
            "Return the full game-state observation: board per player (monsters, "
            "spells/traps, hand, graveyard, banished, deck, extra), life points, "
            "turn / phase, and the current pending decision (if any) with its "
            "legal moves.  Call this first, then pick a response tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "perspective": {
                    "type": "integer",
                    "enum": [0, 1],
                    "description": (
                        "Whose view to render (0 or 1).  Hides the opponent's "
                        "face-down / hand cards.  Defaults to the player whose "
                        "decision is pending."
                    ),
                },
            },
            "required": [],
        },
    },
    {
        "name": "pending_decision",
        "description": (
            "Return just the current pending decision — which SELECT / ANNOUNCE "
            "the engine is waiting on, the responder name to call, and the legal "
            "moves structured for that responder.  Lighter than get_state."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "inspect_card",
        "description": (
            "Look up a card by its 8-digit card code (e.g. 46986414 for Dark "
            "Magician).  Returns name, types, attribute, race, attack, defense, "
            "level, and full oracle text.  Use this when you see a card code in "
            "the state but need to know what the card does."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "card_code": {
                    "type": "integer",
                    "description": "8-digit card code (passcode).",
                },
            },
            "required": ["card_code"],
        },
    },
    {
        "name": "get_glossary",
        "description": (
            "One-shot dump of every OCG engine enum you may encounter in "
            "observations: position, location, phase, attribute, race, "
            "type flags, status flags, move reason flags, link markers, "
            "and win-reason codes.  Call this once at the start of an "
            "episode; future observations will already render these in "
            "human-readable form.  Use it when you see a numeric mask "
            "you can't interpret (e.g. AnnounceRace.available)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


# ---------------------------------------------------------------------------
# Response tools — one per Harness.respond_* method.
# ---------------------------------------------------------------------------

RESPONSE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "select_battlecmd",
        "description": (
            "Respond to MSG_SELECT_BATTLECMD (battle phase choice).  Pick one of: "
            "'activate' (chain a trigger), 'attack' (declare attack), "
            "'to_main_phase_2' (go to M2), 'to_end_phase' (end turn).  For "
            "'activate'/'attack', pass the index into the respective options "
            "list in the pending decision."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": ["activate", "attack", "to_main_phase_2", "to_end_phase"],
                },
                "index": {
                    "type": "integer",
                    "minimum": 0,
                    "description": (
                        "Index into the matching options list "
                        "(activatable_options or attackable_options).  "
                        "Ignored for 'to_main_phase_2' / 'to_end_phase'."
                    ),
                    "default": 0,
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "select_idlecmd",
        "description": (
            "Respond to MSG_SELECT_IDLECMD (main-phase choice).  Pick one of: "
            "'summon', 'sp_summon', 'repos', 'set_monster', 'set_spell', "
            "'activate' (each takes an 'index'), or one of the simple commands "
            "'to_battle_phase', 'to_end_phase', 'shuffle_hand'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "enum": [
                        "summon", "sp_summon", "repos", "set_monster",
                        "set_spell", "activate",
                        "to_battle_phase", "to_end_phase", "shuffle_hand",
                    ],
                },
                "index": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": (
                        "Index into the matching options list.  Ignored for "
                        "'to_battle_phase' / 'to_end_phase' / 'shuffle_hand'."
                    ),
                },
            },
            "required": ["command"],
        },
    },
    {
        "name": "select_effectyn",
        "description": (
            "Respond to MSG_SELECT_EFFECTYN — a Yes/No prompt tied to a "
            "specific card's effect (e.g. 'Apply X?')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "accept": {
                    "type": "boolean",
                    "description": "True = yes (activate), False = no (decline).",
                },
            },
            "required": ["accept"],
        },
    },
    {
        "name": "select_yesno",
        "description": (
            "Respond to MSG_SELECT_YESNO — a generic Yes/No prompt (e.g. "
            "tribute a monster for a cost)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "accept": {"type": "boolean"},
            },
            "required": ["accept"],
        },
    },
    {
        "name": "select_option",
        "description": (
            "Respond to MSG_SELECT_OPTION by index into the options list "
            "(choose one of multiple effects)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "minimum": 0},
            },
            "required": ["index"],
        },
    },
    {
        "name": "select_card",
        "description": (
            "Respond to MSG_SELECT_CARD (generic card selection — targets, "
            "tributes, search picks, etc.).  Submit a list of indices into the "
            "'cards' array of the pending decision.  Selection size must be "
            "within [min_, max_]."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "indices": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0},
                    "description": "Indices into the pending decision's 'cards' list.",
                },
                "cancel": {
                    "type": "boolean",
                    "default": False,
                    "description": "Cancel the selection (only if cancelable=true).",
                },
            },
            "required": ["indices"],
        },
    },
    {
        "name": "select_card_codes",
        "description": (
            "Respond to MSG_SELECT_CARD variants that use select_cards_codes "
            "(distinct from select_cards).  Wire format matches select_card."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "indices": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0},
                },
                "cancel": {"type": "boolean", "default": False},
            },
            "required": ["indices"],
        },
    },
    {
        "name": "select_unselect_card",
        "description": (
            "Respond to MSG_SELECT_UNSELECT_CARD (one pick at a time, with "
            "previously-picked cards re-offered so they can be unpicked).  "
            "Pass 'index' into the combined list (selectable + selected) or "
            "null to finish/cancel."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {
                    "type": ["integer", "null"],
                    "minimum": 0,
                    "description": (
                        "Index into concatenation of selectable_cards + "
                        "selected_cards.  null = finish/cancel (only if "
                        "finishable or cancelable)."
                    ),
                },
            },
            "required": ["index"],
        },
    },
    {
        "name": "select_chain",
        "description": (
            "Respond to MSG_SELECT_CHAIN — pick which chainable effect to "
            "activate, or decline (null) if not forced."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {
                    "type": ["integer", "null"],
                    "minimum": 0,
                    "description": (
                        "Index into pending.cards.  null = decline "
                        "(disallowed when 'forced' is true)."
                    ),
                },
            },
            "required": ["index"],
        },
    },
    {
        "name": "select_place",
        "description": (
            "Respond to MSG_SELECT_PLACE (or MSG_SELECT_DISFIELD) — specify "
            "where to place cards.  Submit exactly 'min_' places.  Each place "
            "is (player, location, sequence)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "places": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "player":   {"type": "integer", "enum": [0, 1]},
                            "location": {
                                "type": "integer",
                                "description": (
                                    "OCG location constant: 0x04=MZ, 0x08=SZ, "
                                    "0x10=GY, 0x20=banished, 0x40=extra, etc."
                                ),
                            },
                            "sequence": {"type": "integer", "minimum": 0},
                        },
                        "required": ["player", "location", "sequence"],
                    },
                    "description": "Places to pick — length must equal pending.min_.",
                },
            },
            "required": ["places"],
        },
    },
    {
        "name": "select_position",
        "description": (
            "Respond to MSG_SELECT_POSITION — pick a battle position for a "
            "card.  Valid values: 0x1 FACEUP_ATK, 0x2 FACEDOWN_ATK, "
            "0x4 FACEUP_DEF, 0x8 FACEDOWN_DEF.  Must be a single bit in the "
            "pending decision's 'positions' mask."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "position": {
                    "type": "integer",
                    "enum": [1, 2, 4, 8],
                },
            },
            "required": ["position"],
        },
    },
    {
        "name": "select_tribute",
        "description": (
            "Respond to MSG_SELECT_TRIBUTE — pick monsters to tribute.  Same "
            "wire format as select_card."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "indices": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0},
                },
                "cancel": {"type": "boolean", "default": False},
            },
            "required": ["indices"],
        },
    },
    {
        "name": "select_counter",
        "description": (
            "Respond to MSG_SELECT_COUNTER — distribute a total 'count' of "
            "counters across the eligible cards.  'counts[i]' ≤ "
            "pending.cards[i].counter for each i, and sum(counts) must equal "
            "pending.count."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "counts": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0},
                    "description": (
                        "One entry per card in pending.cards (same order)."
                    ),
                },
            },
            "required": ["counts"],
        },
    },
    {
        "name": "select_sum",
        "description": (
            "Respond to MSG_SELECT_SUM — pick a subset of cards whose values "
            "sum appropriately (e.g. tributes with matching levels).  Returns "
            "a list of indices into pending.optional_cards."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "indices": {
                    "type": "array",
                    "items": {"type": "integer", "minimum": 0},
                },
            },
            "required": ["indices"],
        },
    },
    {
        "name": "sort_card",
        "description": (
            "Respond to MSG_SORT_CARD / MSG_SORT_CHAIN — give a permutation of "
            "0..n-1 where entry i is the new position of the card originally "
            "at position i.  Pass null to skip sorting (writes -1)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ordering": {
                    "type": ["array", "null"],
                    "items": {"type": "integer", "minimum": 0},
                    "description": (
                        "Permutation of [0, n-1], or null to skip (card order "
                        "unchanged)."
                    ),
                },
            },
            "required": ["ordering"],
        },
    },
    {
        "name": "announce_race",
        "description": (
            "Respond to MSG_ANNOUNCE_RACE — declare N races by setting N bits "
            "in a 64-bit mask.  popcount(races_mask) must equal pending.count "
            "and all bits must be in pending.available."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "races_mask": {"type": "integer", "minimum": 0},
            },
            "required": ["races_mask"],
        },
    },
    {
        "name": "announce_attribute",
        "description": (
            "Respond to MSG_ANNOUNCE_ATTRIB — declare N attributes by setting "
            "N bits in a 32-bit mask.  popcount(attribs_mask) must equal "
            "pending.count and all bits must be in pending.available."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "attribs_mask": {"type": "integer", "minimum": 0},
            },
            "required": ["attribs_mask"],
        },
    },
    {
        "name": "announce_card",
        "description": (
            "Respond to MSG_ANNOUNCE_CARD — declare a card by its 8-digit "
            "passcode (e.g. 89631139 for Blue-Eyes White Dragon).  The engine "
            "validates the code against the opcode filter; pick a card that "
            "satisfies the filter described in the pending decision."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "card_code": {"type": "integer", "minimum": 1},
            },
            "required": ["card_code"],
        },
    },
    {
        "name": "announce_number",
        "description": (
            "Respond to MSG_ANNOUNCE_NUMBER — pick a number by its index in "
            "the pending options list."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "index": {"type": "integer", "minimum": 0},
            },
            "required": ["index"],
        },
    },
    {
        "name": "rock_paper_scissors",
        "description": (
            "Respond to MSG_ROCK_PAPER_SCISSORS — 1 (rock), 2 (scissors), or "
            "3 (paper)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hand": {"type": "integer", "enum": [1, 2, 3]},
            },
            "required": ["hand"],
        },
    },
]


# ---------------------------------------------------------------------------
# Meta tools — change engine state but not via the pending-decision protocol.
# ---------------------------------------------------------------------------

META_TOOLS: list[dict[str, Any]] = [
    {
        "name": "restart",
        "description": (
            "Reset the puzzle to its initial conditions and discard all "
            "in-progress duel state. Use when you are stuck or have made an "
            "irrecoverable mistake. The conversation history is preserved "
            "(so you can learn from prior attempts) but the engine is "
            "rebuilt from the puzzle's initial setup. The tool-call budget "
            "keeps ticking — restart consumes 1 tool call like any other."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
]


TOOLS: list[dict[str, Any]] = INSPECTION_TOOLS + META_TOOLS + RESPONSE_TOOLS


# ---------------------------------------------------------------------------
# Dispatch map — tool name → Harness method (for response tools).
# Inspection tools are handled by the runner directly (they need access to
# card_db and observation rendering, not just the harness).
# ---------------------------------------------------------------------------

TOOL_TO_HARNESS_METHOD: dict[str, str] = {
    "select_battlecmd":       "respond_select_battlecmd",
    "select_idlecmd":         "respond_select_idlecmd",
    "select_effectyn":        "respond_select_effectyn",
    "select_yesno":           "respond_select_yesno",
    "select_option":          "respond_select_option",
    "select_card":            "respond_select_card",
    "select_card_codes":      "respond_select_card_codes",
    "select_unselect_card":   "respond_select_unselect_card",
    "select_chain":           "respond_select_chain",
    "select_place":           "respond_select_place",
    "select_position":        "respond_select_position",
    "select_tribute":         "respond_select_tribute",
    "select_counter":         "respond_select_counter",
    "select_sum":             "respond_select_sum",
    "sort_card":              "respond_sort_card",
    "announce_race":          "respond_announce_race",
    "announce_attribute":     "respond_announce_attribute",
    "announce_card":          "respond_announce_card",
    "announce_number":        "respond_announce_number",
    "rock_paper_scissors":    "respond_rock_paper_scissors",
}

INSPECTION_TOOL_NAMES: frozenset[str] = frozenset({
    "get_state", "pending_decision", "inspect_card", "get_glossary",
})

# Inspection tools always available regardless of --forage:
# - get_state / pending_decision: reflect EVOLVING engine state that
#   the initial prompt can't predict.  Useful for grounding the model
#   in the actual current state.
# - get_glossary: dumps every OCG engine enum (positions, locations,
#   phases, attributes, races, type-flags, status-flags, move-reason
#   flags, link markers, win-reason codes).  The prompt's notation
#   block only covers a subset, so the full decoder is genuinely
#   useful even when the rest of the prompt is rich.
ALWAYS_AVAILABLE_INSPECTION: frozenset[str] = frozenset({
    "get_state", "pending_decision", "get_glossary",
})

# Inspection tools whose output IS just a re-read of static info
# already in the system prompt (default mode).  Gated on --forage:
# only available when the prompt is lean.
# - inspect_card: every card_id in the puzzle is in the prompt's
#   glossary section already.
FORAGE_ONLY_INSPECTION: frozenset[str] = frozenset({
    "inspect_card",
})

META_TOOL_NAMES: frozenset[str] = frozenset({"restart"})


def normalize_place_arg(places: list[dict]) -> list[tuple[int, int, int]]:
    """Convert the JSON-Schema place objects into the tuple form the harness expects."""
    out = []
    for p in places:
        out.append((int(p["player"]), int(p["location"]), int(p["sequence"])))
    return out


def coerce_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Translate a JSON-Schema-shaped ``args`` dict into the exact kwargs the
    corresponding ``Harness.respond_*`` method accepts.

    Most tool schemas already match the method signatures directly; the only
    non-trivial cases involve nullable indices and the list-of-object shape
    used by ``select_place``.

    Shared between the fully interactive runner and the n-attempts bulk
    replay evaluators (which both drive the harness from JSON action lists).
    """
    if tool_name == "select_place":
        return {"places": normalize_place_arg(args.get("places", []))}
    if tool_name in ("select_unselect_card", "select_chain"):
        idx = args.get("index", None)
        return {"index": None if idx is None else int(idx)}
    if tool_name == "sort_card":
        ordering = args.get("ordering", None)
        return {"ordering": None if ordering is None else [int(x) for x in ordering]}
    return dict(args)


def as_openai_tools() -> list[dict[str, Any]]:
    """Wrap TOOLS in OpenAI's function-calling envelope."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in TOOLS
    ]


def as_anthropic_tools() -> list[dict[str, Any]]:
    """TOOLS is already in Anthropic's format — return a shallow copy."""
    return [dict(t) for t in TOOLS]
