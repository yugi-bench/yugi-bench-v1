"""Build the YuGiOh-Bench dataset from EDOPro puzzle scripts.

Reads EDOPro puzzle Lua files and card databases, producing one JSONL entry
per puzzle with game state JSON, card details, an LLM prompt and the gold NL
solution. A pre-built dataset is already shipped at ``data/yugioh_bench.jsonl``;
you only need this script if you want to regenerate it (e.g. after upstream
updates to EDOPro).

Run ``python src/dataset/build_benchmark.py --help`` for options, including
``--overwrite`` to regenerate over an existing ``data/yugioh_bench.jsonl``
idempotently.
"""

from __future__ import annotations

import sys
from pathlib import Path as _Path
_REPO_ROOT = _Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))


import argparse
import json
import os
import re
import sqlite3
import sys
import hashlib
from pathlib import Path
from typing import Optional


# ---------------------------------------------------------------------------
# Card database
# ---------------------------------------------------------------------------

CARD_TYPES = {
    0x1: "Monster", 0x2: "Spell", 0x4: "Trap",
    0x10: "Normal", 0x20: "Effect", 0x40: "Fusion",
    0x80: "Ritual", 0x200: "Spirit", 0x400: "Union",
    0x800: "Gemini", 0x1000: "Tuner", 0x2000: "Synchro",
    0x4000: "Token", 0x8000: "Quick-Play", 0x10000: "Continuous",
    0x20000: "Equip", 0x40000: "Field", 0x80000: "Counter",
    0x100000: "Flip", 0x200000: "Toon", 0x400000: "Xyz",
    0x800000: "Pendulum", 0x1000000: "Link",
}
RACES = {
    0x1: "Warrior", 0x2: "Spellcaster", 0x4: "Fairy",
    0x8: "Fiend", 0x10: "Zombie", 0x20: "Machine",
    0x40: "Aqua", 0x80: "Pyro", 0x100: "Rock",
    0x200: "Winged Beast", 0x400: "Plant", 0x800: "Insect",
    0x1000: "Thunder", 0x2000: "Dragon", 0x4000: "Beast",
    0x8000: "Beast-Warrior", 0x10000: "Dinosaur", 0x20000: "Fish",
    0x40000: "Sea Serpent", 0x80000: "Reptile",
    0x100000: "Psychic", 0x200000: "Divine-Beast",
    0x400000: "Creator God", 0x800000: "Wyrm",
    0x1000000: "Cyberse", 0x2000000: "Illusion",
}
ATTRIBUTES = {
    0x1: "EARTH", 0x2: "WATER", 0x4: "FIRE",
    0x8: "WIND", 0x10: "LIGHT", 0x20: "DARK", 0x40: "DIVINE",
}


def decode_bitmask(value: int, table: dict) -> list[str]:
    return [name for bit, name in sorted(table.items()) if value & bit]


class CardDatabase:
    def __init__(self, cdb_paths: list[Path]):
        self._texts: dict[int, tuple[str, str]] = {}
        self._datas: dict[int, dict] = {}
        for p in cdb_paths:
            self._load(p)

    def _load(self, path: Path) -> None:
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        for row in conn.execute("SELECT * FROM texts"):
            cid = row["id"]
            if cid not in self._texts:
                self._texts[cid] = (row["name"], row["desc"])
        for row in conn.execute("SELECT * FROM datas"):
            cid = row["id"]
            if cid not in self._datas:
                self._datas[cid] = dict(row)
        conn.close()

    def lookup(self, card_id: int) -> Optional[dict]:
        if card_id not in self._texts:
            return None
        name, desc = self._texts[card_id]
        data = self._datas.get(card_id, {})
        type_val = data.get("type", 0)
        types = decode_bitmask(type_val, CARD_TYPES)
        is_monster = bool(type_val & 0x1)
        is_link = bool(type_val & 0x1000000)
        is_xyz = bool(type_val & 0x400000)

        info: dict = {"name": name, "card_types": types, "description": desc}
        if is_monster:
            atk = data.get("atk")
            info["atk"] = atk if atk is not None and atk >= 0 else "?"
            if not is_link:
                def_ = data.get("def")
                info["def"] = def_ if def_ is not None and def_ >= 0 else "?"
            lv = data.get("level", 0) & 0xFF
            if is_link:
                info["link_rating"] = lv
            elif is_xyz:
                info["rank"] = lv
            else:
                info["level"] = lv
            races = decode_bitmask(data.get("race", 0), RACES)
            if races:
                info["race"] = races[0]
            attrs = decode_bitmask(data.get("attribute", 0), ATTRIBUTES)
            if attrs:
                info["attribute"] = attrs[0]
        return info


# ---------------------------------------------------------------------------
# Lua parsing → JSON game state
# ---------------------------------------------------------------------------

LOCATION_MAP = {
    "LOCATION_HAND": "hand",
    "LOCATION_DECK": "deck",
    "LOCATION_MZONE": "monster_zone",
    "LOCATION_SZONE": "spell_zone",
    "LOCATION_FZONE": "spell_zone",  # Field Zone is part of spell/trap zone
    "LOCATION_PZONE": "spell_zone",  # Pendulum Zone is part of spell/trap zone
    "LOCATION_GRAVE": "graveyard",
    "LOCATION_EXTRA": "extra_deck",
    "LOCATION_REMOVED": "banished",
}

POSITION_MAP = {
    "POS_FACEDOWN": "face_down",
    "POS_FACEDOWN_DEFENSE": "face_down_defense",
    "POS_FACEUP": "face_up",
    "POS_FACEUP_ATTACK": "face_up_attack",
    "POS_FACEUP_DEFENSE": "face_up_defense",
}


def parse_game_state(lua_text: str, db: CardDatabase) -> dict:
    """Parse Lua puzzle setup into a JSON game state object."""
    state = {
        "player": {
            "life_points": 8000,
            "hand": [], "monster_zone": [], "spell_zone": [],
            "graveyard": [], "banished": [], "deck": [], "extra_deck": [],
        },
        "opponent": {
            "life_points": 8000,
            "hand": [], "monster_zone": [], "spell_zone": [],
            "graveyard": [], "banished": [], "deck": [], "extra_deck": [],
        },
    }

    # Parse life points
    for m in re.finditer(r'Debug\.SetPlayerInfo\((\d+)\s*,\s*(\d+)', lua_text):
        player_id, lp = int(m.group(1)), int(m.group(2))
        side = "player" if player_id == 0 else "opponent"
        state[side]["life_points"] = lp

    # Parse card placements
    add_card_re = re.compile(
        r'Debug\.AddCard\(\s*(\d+)\s*,'   # card_id
        r'\s*(\d+)\s*,'                     # owner
        r'\s*(\d+)\s*,'                     # controller
        r'\s*(\w+)\s*,'                     # location
        r'\s*(\d+)\s*,'                     # zone_index
        r'\s*(\w+)'                         # position
    )
    for m in add_card_re.finditer(lua_text):
        card_id = int(m.group(1))
        owner = int(m.group(2))
        controller = int(m.group(3))
        location_const = m.group(4)
        zone_index = int(m.group(5))
        position_const = m.group(6)

        location = LOCATION_MAP.get(location_const, location_const)
        position = POSITION_MAP.get(position_const, position_const)
        side = "player" if controller == 0 else "opponent"

        info = db.lookup(card_id)
        card_name = info["name"] if info else f"Unknown({card_id})"

        card_entry: dict = {
            "card_id": card_id,
            "card_name": card_name,
            "position": position,
            "zone": zone_index,
        }
        # Add owner info if different from controller (e.g. stolen cards)
        if owner != controller:
            card_entry["owner"] = "player" if owner == 0 else "opponent"

        state[side][location].append(card_entry)

    return state


def format_card_details(card_ids: list[int], db: CardDatabase) -> dict:
    """Build a card_id → card_info mapping as JSON."""
    details = {}
    for cid in sorted(set(card_ids)):
        info = db.lookup(cid)
        if info:
            details[str(cid)] = info
    return details


# ---------------------------------------------------------------------------
# Solution extraction (kept for gold reference)
# ---------------------------------------------------------------------------

def strip_solution(lua_text: str) -> str:
    """Remove all solution content from a Lua puzzle file."""
    stripped = re.sub(
        r'--\[\[(?:(?!\]\]).)*?[Ss]olution.*?\]\]',
        '', lua_text, flags=re.DOTALL
    )
    match = re.search(r'(aux\.BeginPuzzle\(\))', stripped)
    if match:
        before = stripped[:match.end()]
        after = stripped[match.end():]
        after = re.sub(r'--.*', '', after)
        after = after.strip()
        stripped = before + '\n'
    else:
        stripped = re.sub(r'\n\s*--\s*(?:Puzzle )?[Ss]olution.*', '', stripped, flags=re.DOTALL)
    stripped = re.sub(r'\n\s*--\s*Solution\s*\(Video\).*\n', '\n', stripped, flags=re.IGNORECASE)
    return stripped.rstrip() + '\n'


def extract_solution(lua_text: str) -> Optional[str]:
    """Extract the solution text from puzzle comments."""
    if re.search(r'--\s*Solution\s*\(Video\)', lua_text, re.IGNORECASE):
        if not re.search(r'--\[\[.*?Solution.*?\n.*?\w.*?\]\]', lua_text, re.DOTALL | re.IGNORECASE):
            after_puzzle = lua_text.split('aux.BeginPuzzle()')[-1] if 'aux.BeginPuzzle()' in lua_text else ''
            sol_lines = re.findall(r'--\s*\d+\s+\w.*', after_puzzle)
            if not sol_lines:
                return None

    sol_match = re.search(
        r'Solution\s*:?\s*\n(.*?)(?:\]\]|$)',
        lua_text, re.DOTALL | re.IGNORECASE
    )
    if not sol_match:
        if 'aux.BeginPuzzle()' in lua_text:
            after = lua_text.split('aux.BeginPuzzle()')[-1]
            sol_lines = re.findall(r'--\s*(\d+\s+.+)', after)
            if sol_lines:
                return "\n".join(l.strip() for l in sol_lines)
        return None

    raw = sol_match.group(1).strip()
    lines = []
    for line in raw.split("\n"):
        line = line.strip().lstrip("-").strip()
        if not line or re.match(r'^[=\-\*\+\~\^]+$', line):
            continue
        if re.match(r'^\d+$', line):
            continue
        if re.match(r'^Part\s+\d+', line, re.IGNORECASE):
            continue
        if re.match(r'^Completion Reward', line, re.IGNORECASE):
            continue
        lines.append(line)
    return "\n".join(lines) if lines else None


def merge_continuation_lines(text: str) -> list[str]:
    """Split solution text into steps, merging continuation lines."""
    step_start = re.compile(
        r'^\s*(?:'
        r'(?:step\s*)?\d+\s*[\.\)\:\-]'
        r'|\d+\s+(?:activate|summon|set|attack|enter|flip|change|special|discard|switch|sacrifice|choose|equip|use|normal|tribute|synchro|xyz|fusion|link|ritual|battle|end|pendulum)\b'
        r'|[\-\*]'
        r'|(?:activate|summon|set|attack|enter|flip|change|special|discard|switch|sacrifice|choose|equip|use|normal|tribute|synchro|xyz|fusion|link|ritual|battle|end|pendulum)\b'
        r')',
        re.IGNORECASE,
    )
    raw_lines = [l.strip() for l in text.strip().split('\n') if l.strip()]
    if not raw_lines:
        return []
    merged = [raw_lines[0]]
    for line in raw_lines[1:]:
        if step_start.match(line):
            merged.append(line)
        else:
            merged[-1] = merged[-1] + ' ' + line
    steps = []
    for line in merged:
        cleaned = re.sub(r'^\s*(?:step\s*)?\d+\s*[\.\)\:\-\s]\s*', '', line, flags=re.IGNORECASE)
        cleaned = re.sub(r'^\s*[\-\*]\s*', '', cleaned).strip()
        if cleaned:
            steps.append(cleaned)
    return steps


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def extract_metadata(lua_text: str) -> dict:
    meta = {}
    msg_match = re.search(r'--\[\[message\s*\n(.*?)\]\]', lua_text, re.DOTALL)
    if msg_match:
        msg = msg_match.group(1)
        cx = re.search(r'Complexity:\s*(.+)', msg, re.IGNORECASE)
        if cx:
            meta["complexity"] = cx.group(1).strip().rstrip(".")
        obj = re.search(r'Objective:\s*(.+)', msg, re.IGNORECASE)
        if obj:
            meta["objective"] = obj.group(1).strip()
    meta.setdefault("objective", "Win this turn")
    meta["is_rush"] = "DUEL_MODE_RUSH" in lua_text
    meta["uses_advanced_api"] = bool(re.search(r'(?<!Debug\.)(?:Effect|Duel)\.', lua_text))
    lps = re.findall(r'Debug\.SetPlayerInfo\((\d+),(\d+),', lua_text)
    for pid, lp in lps:
        if pid == "0":
            meta["player_lp"] = int(lp)
        else:
            meta["opponent_lp"] = int(lp)
    return meta


def extract_card_ids(lua_text: str) -> list[int]:
    return [int(m) for m in re.findall(r'Debug\.AddCard\(\s*(0?\d+)', lua_text)]


def extract_hints(lua_text: str) -> list[str]:
    hints = re.findall(r'Debug\.ShowHint\("([^"]+)"\)', lua_text)
    return [h for h in hints if not re.match(r'^Win\b', h, re.IGNORECASE)]


def title_from_filename(path: Path) -> str:
    title = path.stem
    for prefix in ("[GX_Spirit_Caller]", "[WCS2006]", "[WCS2007]", "[WCS2008]",
                   "[Nightmare Troubadour]", "[RUSH]", "Naim_DuelLinks_"):
        title = title.replace(prefix, "")
    return title.strip()


def category_from_path(path: Path, puzzle_root: Path) -> str:
    rel = path.relative_to(puzzle_root)
    parts = rel.parts
    return parts[0] if len(parts) >= 2 else "Uncategorized"


# ---------------------------------------------------------------------------
# Prompt formatting — JSON structured
# ---------------------------------------------------------------------------

RULES_PREAMBLE = """\
# Yu-Gi-Oh! Trading Card Game — Rules Reference

## Card Types

**Monster Cards** have ATK (attack) and DEF (defense) values, a Level (1-12), \
an Attribute (LIGHT, DARK, EARTH, WATER, FIRE, WIND), and a Race (e.g. Warrior, \
Spellcaster, Dragon). Monsters can be in face-up Attack Position or face-up/\
face-down Defense Position.
- **Normal Monsters** have no effects (their text is flavour only).
- **Effect Monsters** have effects described in their card text.
- **Fusion Monsters** are stored in the Extra Deck and are Summoned by using a \
Fusion Spell (e.g. "Polymerization") with the required Fusion Materials.
- **Synchro Monsters** are stored in the Extra Deck. To Synchro Summon: send 1 \
Tuner + 1 or more non-Tuner monsters you control to the Graveyard whose total \
Levels exactly equal the Synchro Monster's Level.
- **Xyz Monsters** are stored in the Extra Deck and have a Rank instead of a \
Level. To Xyz Summon: overlay 2+ monsters you control with the same Level equal \
to the Xyz Monster's Rank. The overlay materials become attached to the Xyz \
Monster and can be detached to pay costs.
- **Link Monsters** are stored in the Extra Deck, have a Link Rating instead of \
Level/DEF, and have Link Arrows. To Link Summon: send monsters you control to \
the GY whose total equals the Link Rating (a Link Monster can count as 1, or as \
its own Link Rating).
- **Ritual Monsters** are Summoned from the hand using a specific Ritual Spell \
Card by tributing monsters whose total Levels meet or exceed the Ritual Monster's \
Level.
- **Pendulum Monsters** can be placed in Pendulum Zones as Spells. With two \
Pendulum Scales set, you can Pendulum Summon monsters from hand/Extra Deck whose \
Levels are between the two Scales (exclusive).

**Spell Cards** are activated from the hand or field.
- **Normal Spells**: activated, effect resolves, then sent to GY.
- **Quick-Play Spells**: can be activated from the hand during your turn, or from \
a set position during either player's turn (like a Trap).
- **Continuous Spells**: remain on the field after activation.
- **Equip Spells**: equipped to a monster, remain on the field.
- **Field Spells**: placed in the Field Zone, one per player.
- **Ritual Spells**: used to Ritual Summon a specific Ritual Monster.

**Trap Cards** must be Set for at least one turn before they can be activated \
(unless a card effect says otherwise). In these puzzles, all set Traps are \
available to activate immediately.
- **Normal Traps**: activated, effect resolves, sent to GY.
- **Continuous Traps**: remain on the field.
- **Counter Traps**: activated in response to another card/effect (Spell Speed 3).

## Turn Structure

A turn has these phases (puzzles typically start in Main Phase 1):
1. **Main Phase 1**: Normal Summon/Set a monster, activate Spells/Traps, \
activate monster effects, Special Summon via card effects.
2. **Battle Phase**: Declare attacks with your Attack Position monsters. When a \
monster attacks, compare ATK values:
   - vs. Attack Position: lower-ATK monster is destroyed; controller takes damage \
equal to the difference. If equal, both are destroyed.
   - vs. Defense Position: if attacker's ATK > defender's DEF, defender is \
destroyed (no damage). If ATK <= DEF, attacker's controller takes damage equal to \
the difference. If ATK = DEF, nothing happens.
   - **Direct attack**: if the opponent has no monsters, you attack their LP directly \
for your monster's full ATK.
3. **Main Phase 2**: same actions as Main Phase 1 (after the Battle Phase).

## Key Mechanics

- **Normal Summon**: once per turn, from hand. Level 4 or lower can be summoned \
directly. Level 5-6 requires 1 Tribute (send 1 monster you control to GY). Level \
7+ requires 2 Tributes.
- **Special Summon**: summoning via a card effect. No inherent once-per-turn limit \
(unless the card says so). Does not use your Normal Summon.
- **Flip Summon**: manually flip a face-down Defense monster to face-up Attack. \
Does not use your Normal Summon. Triggers Flip effects.
- **Change position**: once per turn per monster, you can change a monster's \
battle position (ATK ↔ DEF). Cannot change position of a monster summoned this turn.
- **Graveyard (GY)**: where destroyed/used cards go. Many effects interact with it.
- **Banished**: cards removed from play. Some effects banish as a cost or can \
retrieve banished cards.
- **Chain**: when multiple effects activate in response to each other, they form a \
chain and resolve in reverse order (last activated resolves first).
- **Life Points (LP)**: each player starts with LP set by the puzzle. Reduce the \
opponent's LP to 0 to win.
- **Damage**: Battle damage from attacks, or Effect damage from card effects.\
"""

ACTION_SCHEMA = """\
## Action Schema

Your solution must be a JSON array of action objects inside <solution> tags. \
Each action represents one decision you make. The game engine will process \
each action in order, and sub-decisions (like choosing targets) are embedded \
in the action.

### Action Types

**Main Phase actions:**
- `{"action": "summon", "card": "Card Name", "position": "ATK"}` — Normal Summon \
a monster from your hand. Specify `"ATK"` or `"DEF"`. If the monster requires \
Tributes (Level 5-6: 1, Level 7+: 2), list them in `"tribute"`.
- `{"action": "set_monster", "card": "Card Name"}` — Set a monster face-down.
- `{"action": "set_spell", "card": "Card Name"}` — Set a Spell/Trap face-down.
- `{"action": "activate", "card": "Card Name"}` — Activate a Spell, Trap, or \
monster effect. If the effect requires targets or selections, include them in \
`"targets"` (cards the effect acts on) and `"selections"` (other choices like \
materials, tributes, discards).
- `{"action": "special_summon", "card": "Card Name", "position": "ATK"}` — \
Special Summon a monster (via an inherent summon procedure, not via activating \
a card effect).
- `{"action": "change_position", "card": "Card Name", "position": "DEF"}` — \
Change a monster's battle position.
- `{"action": "flip_summon", "card": "Card Name"}` — Flip Summon a face-down monster.
- `{"action": "to_battle_phase"}` — Enter the Battle Phase.
- `{"action": "to_end_phase"}` — End your turn.

**Battle Phase actions:**
- `{"action": "attack", "card": "Attacker Name", "target": "Defender Name"}` — \
Declare an attack. Use `"target": "DIRECT"` for a direct attack.
- `{"action": "activate", "card": "Card Name"}` — Activate a card during battle.
- `{"action": "to_main_phase_two"}` — Proceed to Main Phase 2.
- `{"action": "to_end_phase"}` — End the turn from battle.

**Responding to effects:**
- `{"action": "chain", "card": "Card Name"}` — Chain an effect in response.
- `{"action": "pass"}` — Decline to chain / pass on an optional effect.
- `{"action": "yes"}` / `{"action": "no"}` — Respond to a yes/no prompt.
- `{"action": "select", "cards": ["Card A", "Card B"]}` — Select cards when \
prompted (for targets, materials, tributes, etc.).
- `{"action": "select_position", "position": "ATK"}` — Choose a card's position \
when prompted.

### Card References

Use the exact `card_name` from the game state. When multiple copies of a card \
exist, add `"zone"` to disambiguate (e.g., `"zone": 2` for the card in zone \
index 2). You may also use `"card_id"` for precision.\
"""

SAMPLE_DIR = Path(__file__).resolve().parent / "sample"


def load_example_block() -> str:
    """Build the worked-example block from sample/puzzle.lua + sample/solution.json.

    The sample is a self-contained mini-puzzle that is verified to win against
    the real OCG engine (see sample/README.md). Embedding it here keeps the
    prompt's example independent of any puzzle in the benchmark itself.
    """
    sol_path = SAMPLE_DIR / "solution.json"
    if not sol_path.exists():
        return ""
    solution = json.loads(sol_path.read_text())
    # Pretty-print the solution, preserving comments so the example reads as a
    # worked walkthrough rather than a bare JSON blob.
    pretty = json.dumps(solution, indent=2, ensure_ascii=False)
    return f"""\

### Worked Example

Below is a complete solution for a separate mini-puzzle (not part of the \
benchmark). The player controls a face-up Defense-Position Mystical Elf with \
Monster Reborn and La Jinn in hand and Blue-Eyes White Dragon in the \
Graveyard; the opponent controls Battle Ox and Mystical Elf, both in Attack. \
Opponent has 3100 LP. A winning solution:

```json
{pretty}
```\
"""


ACTION_SCHEMA_EXAMPLE = load_example_block()

SYSTEM_PROMPT = """\
You are solving a Yu-Gi-Oh! duel puzzle. You will be given:
1. A rules reference for the Yu-Gi-Oh! Trading Card Game
2. The current game state as structured JSON (your hand, field, graveyard, etc.)
3. Detailed card information for every card in the puzzle
4. An action schema defining the JSON format for your solution

Your task: determine the exact sequence of actions to achieve the stated \
objective (usually: win this turn by reducing the opponent's LP to 0).

You may reason through your strategy however you like. When you have determined \
your final answer, output it as a JSON array inside <solution> tags.

Only include actions YOU actively choose — do not include automatic engine \
resolution, opponent responses, or mandatory effects that trigger without your \
input. Include sub-decisions (target selection, position choice, etc.) as \
separate action objects immediately after the action that triggers them.\
"""


def build_prompt(game_state: dict, card_details: dict, meta: dict,
                 hints: list[str], include_example: bool = True) -> str:
    """Build the full prompt for a puzzle instance."""
    parts = [
        RULES_PREAMBLE,
        "",
        "---",
        "",
        "# Puzzle",
        "",
        f"**Objective:** {meta.get('objective', 'Win this turn')}",
        f"**Your LP:** {meta.get('player_lp', '?')}",
        f"**Opponent's LP:** {meta.get('opponent_lp', '?')}",
    ]
    if meta.get("complexity"):
        parts.append(f"**Difficulty:** {meta['complexity']}")
    if hints:
        parts.append("")
        parts.append("**Hints:**")
        for h in hints:
            parts.append(f"- {h}")

    parts.append("")
    parts.append("## Game State")
    parts.append("```json")
    parts.append(json.dumps(game_state, indent=2, ensure_ascii=False))
    parts.append("```")

    parts.append("")
    parts.append("## Card Details")
    parts.append("```json")
    parts.append(json.dumps(card_details, indent=2, ensure_ascii=False))
    parts.append("```")

    parts.append("")
    parts.append(ACTION_SCHEMA)
    if include_example:
        parts.append(ACTION_SCHEMA_EXAMPLE)

    parts.append("")
    parts.append("## Task")
    parts.append(f"Determine the sequence of actions to {meta.get('objective', 'win this turn').lower()}.")
    parts.append("Think through the puzzle, then provide your final answer as a JSON array inside <solution> tags.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


# Puzzles excluded from the dataset because of upstream engine bugs that
# leave the harness in an unsatisfiable state — the LLM cannot possibly
# respond correctly through no fault of its own.
#
# All three trip a libocgcore quirk on ritual-tribute paths: the engine
# emits MSG_SELECT_SUM with min_count=0, max_count=0 alongside a
# non-zero accumulator/sum target.  No valid response exists (max_=0
# means only `[]` passes the count check, but `[]` cannot satisfy the
# sum target, so the engine MSG_RETRYs forever and the harness gives
# up).
#
# TODO(upstream): file an issue with edo9300/ygopro-core (or the
# Lua scripts in ProjectIgnis/CardScripts if it's data-side) and
# remove these from the exclusion list once fixed.
EXCLUDED_PUZZLES: set[str] = {
    # First three found in the gold-solution sweep:
    "yugioh_puzzle_50e87709",  # Nightmare Troubadour, Puzzle A12 (?/10)
    "yugioh_puzzle_f57ee82a",  # GX Spirit Caller, A01_Underdog_Power (4/10)
    "yugioh_puzzle_fffedec8",  # World Championship, 33_Match Point (5/10)
    # Three more found in the no-gold residual sweep — same
    # SELECT_SUM(min=0,max=0) bug pattern, confirmed in their JSONLs:
    "yugioh_puzzle_76bc467a",  # Miscellaneous, Eroldin_09_Deny_Absorb_and_Wipe
    "yugioh_puzzle_e795cef3",  # Nightmare Troubadour, Puzzle I04
    "yugioh_puzzle_a29d83ef",  # Miscellaneous, Furtie_Hubo_03_Gallis_FTK
}


def _first_existing(*candidates: Path) -> Path:
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def _default_puzzle_root() -> Path:
    """Default ``--puzzle-root`` looks at ``vendor/puzzles`` first.

    setup.sh clones the upstream ProjectIgnis/Puzzles repo at a pinned
    SHA into ``vendor/puzzles``; the build then walks that tree.  Falls
    back to a sibling ``../puzzles`` for legacy out-of-tree layouts and
    to the EDOPro client install layout for hosts that have an EDOPro
    install nearby.
    """
    env = os.environ.get("YGO_PUZZLE_ROOT")
    if env:
        return Path(env)
    return _first_existing(
        REPO_ROOT / "vendor" / "puzzles",
        REPO_ROOT.parent / "puzzles",
        # macOS EDOPro client install: puzzles live under
        # ./puzzles/Canon collection/.
        Path.home() / "Library" / "Application Support" / "EDOPro" / "puzzles" / "Canon collection",
    )


def _default_cdb_dir() -> Path:
    env = os.environ.get("YGO_DB_DIR")
    if env:
        return Path(env)
    return _first_existing(
        REPO_ROOT / "vendor" / "distribution" / "expansions",
        REPO_ROOT.parent / "distribution" / "expansions",
    )


def _default_script_dir() -> Path:
    env = os.environ.get("YGO_CARD_SCRIPT_DIR")
    if env:
        return Path(env)
    sd = os.environ.get("YGO_SCRIPT_DIR")
    if sd:
        base = Path(sd)
    else:
        base = _first_existing(
            REPO_ROOT / "vendor" / "distribution" / "script",
            REPO_ROOT.parent / "distribution" / "script",
        )
    return base / "official" if base.name != "official" else base


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Build the YuGiOh-Bench dataset from EDOPro puzzle scripts.")
    ap.add_argument("--puzzle-root", type=Path,
                    default=_default_puzzle_root(),
                    help="Path to the EDOPro puzzles directory.  "
                         "Default search: vendor/puzzles (populated by "
                         "setup.sh's pinned ProjectIgnis/Puzzles clone), "
                         "then ../puzzles, then $YGO_PUZZLE_ROOT, then "
                         "an EDOPro client install if present.")
    ap.add_argument("--cdb-dir", type=Path, default=_default_cdb_dir(),
                    help="Directory containing EDOPro card .cdb SQLite files "
                         "(default: vendor/distribution/expansions or "
                         "../distribution/expansions or $YGO_DB_DIR)")
    ap.add_argument("--script-dir", type=Path, default=_default_script_dir(),
                    help="Directory containing card Lua scripts (c*.lua)")
    ap.add_argument("--output", type=Path,
                    default=REPO_ROOT / "data" / "yugioh_bench.jsonl",
                    help="Output JSONL path")
    ap.add_argument("--overwrite", action="store_true",
                    help="Overwrite an existing output file")
    ap.add_argument("--no-example", action="store_true",
                    help="Omit the worked example from the action schema "
                         "in each puzzle prompt")
    ap.add_argument("--lean", action="store_true",
                    help="Omit Konami-derived bulk text fields "
                         "(card_details, prompt) from the JSONL.  The "
                         "released dataset uses --lean; the runner "
                         "rebuilds the omitted fields locally from the "
                         "BabelCDB clone via src/dataset/enrich.py.  See "
                         "the appendix of the paper for the rationale.")
    args = ap.parse_args()

    if args.output.exists() and not args.overwrite:
        print(f"error: {args.output} already exists; pass --overwrite to regenerate",
              file=sys.stderr)
        return 1

    # Load card databases
    cdb_dir = args.cdb_dir
    cdb_paths = [p for p in [
        cdb_dir / "cards.cdb", cdb_dir / "cards-rush.cdb",
        cdb_dir / "cards-unofficial.cdb", cdb_dir / "cards-unofficial-new.cdb",
        cdb_dir / "goat-entries.cdb", cdb_dir / "cards-skills.cdb",
        cdb_dir / "cards-skills-unofficial.cdb",
    ] if p.exists()]
    if not cdb_paths:
        print(f"error: no card .cdb files found in {cdb_dir}", file=sys.stderr)
        return 1
    db = CardDatabase(cdb_paths)
    print(f"Loaded {len(db._texts)} cards from {len(cdb_paths)} databases")

    # Check for card Lua scripts
    script_dir = args.script_dir
    has_scripts = script_dir.is_dir()
    if has_scripts:
        script_count = sum(1 for _ in script_dir.glob("c*.lua"))
        print(f"Found {script_count} card Lua scripts in {script_dir}")
    else:
        print(f"warning: script dir {script_dir} not found; "
              f"cards_with_scripts will be empty", file=sys.stderr)

    # Find puzzles
    puzzle_root = args.puzzle_root
    if not puzzle_root.is_dir():
        print(f"error: puzzle root {puzzle_root} not found", file=sys.stderr)
        return 1
    lua_files = sorted(
        f for f in puzzle_root.rglob("*.lua")
        if ".git" not in f.parts
    )
    print(f"Found {len(lua_files)} puzzle files")

    # Process each puzzle
    dataset = []
    skipped = {"rush": 0, "tutorial": 0, "engine_bug": 0}
    counts = {"with_gold": 0, "without_gold": 0}

    for lua_path in lua_files:
        category = category_from_path(lua_path, puzzle_root)
        lua_text = lua_path.read_text(encoding="utf-8", errors="replace")
        meta = extract_metadata(lua_text)

        if meta["is_rush"]:
            skipped["rush"] += 1
            continue
        if category == "Tutorials":
            skipped["tutorial"] += 1
            continue

        solution = extract_solution(lua_text)
        # Unsolved puzzles (no gold solution in the upstream Lua) are
        # KEPT in the full benchmark with gold_solution=null and
        # metadata.has_gold_solution=false.  The Verified subset
        # (data/yugioh_bench_verified.jsonl) filters these out — see
        # src/dataset/build_verified_subset.py.
        has_gold_solution = bool(solution)
        counts["with_gold" if has_gold_solution else "without_gold"] += 1

        lua_stripped = strip_solution(lua_text)
        card_ids = extract_card_ids(lua_text)
        hints = extract_hints(lua_text)
        title = title_from_filename(lua_path)

        # Build JSON game state
        game_state = parse_game_state(lua_stripped, db)
        card_details = format_card_details(card_ids, db)

        # Check which cards have Lua scripts (for code variant)
        cards_with_scripts = []
        if has_scripts:
            for cid in sorted(set(card_ids)):
                script_path = script_dir / f"c{cid}.lua"
                if script_path.exists():
                    cards_with_scripts.append(cid)

        prompt = build_prompt(game_state, card_details, meta, hints,
                              include_example=not args.no_example)

        solution_steps = merge_continuation_lines(solution) if solution else []

        # Stable ID from file path hash
        rel_path = str(lua_path.relative_to(puzzle_root))
        path_hash = hashlib.sha256(rel_path.encode()).hexdigest()[:8]
        instance_id = f"yugioh_puzzle_{path_hash}"

        if instance_id in EXCLUDED_PUZZLES:
            skipped["engine_bug"] += 1
            print(f"  skipped (engine-bug exclusion): {instance_id}  {rel_path}",
                  file=sys.stderr)
            continue

        instance = {
            "instance_id": instance_id,
            "system_prompt": SYSTEM_PROMPT,
            "game_state": game_state,
            "gold_solution": solution,  # may be None for unsolved puzzles
            "gold_solution_steps": solution_steps,
            "num_solution_steps": len(solution_steps),
            "lua_setup": lua_stripped,
            "card_ids": sorted(set(card_ids)),
            "cards_with_scripts": cards_with_scripts,
            "metadata": {
                "source": category,
                "title": title,
                "file": rel_path,
                "objective": meta.get("objective", "Win this turn"),
                "complexity": meta.get("complexity"),
                "player_lp": meta.get("player_lp"),
                "has_gold_solution": has_gold_solution,
                "opponent_lp": meta.get("opponent_lp"),
                "uses_advanced_api": meta.get("uses_advanced_api", False),
            },
        }
        # Konami-derived bulk text fields (card_details + the rendered
        # prompt that embeds card_details) are included only when --lean
        # is OFF.  The released dataset uses --lean; src/dataset/enrich.py
        # rebuilds them locally from BabelCDB at install time.  See the
        # paper's appendix for the rationale.
        if not args.lean:
            instance["card_details"] = card_details
            instance["prompt"] = prompt
        dataset.append(instance)

    print(f"\nBuilt {len(dataset)} instances")
    print(f"  with gold solution    : {counts['with_gold']}  (Verified subset)")
    print(f"  without gold solution : {counts['without_gold']}  (full only)")
    print(f"Skipped: {skipped}")

    # Sort by source then title for stable ordering
    dataset.sort(key=lambda x: (x["metadata"]["source"], x["metadata"]["title"]))

    for i, inst in enumerate(dataset):
        inst["seq_id"] = i

    # Write JSONL
    jsonl_path = args.output
    jsonl_path.parent.mkdir(parents=True, exist_ok=True)
    with open(jsonl_path, "w") as f:
        for inst in dataset:
            f.write(json.dumps(inst, ensure_ascii=False) + "\n")
    print(f"Wrote {jsonl_path}")

    # Stats
    cats = {}
    for inst in dataset:
        src = inst["metadata"]["source"]
        cats[src] = cats.get(src, 0) + 1
    print(f"\nBy source:")
    for src, n in sorted(cats.items(), key=lambda x: -x[1]):
        print(f"  {src}: {n}")

    steps = [inst["num_solution_steps"] for inst in dataset
             if inst["metadata"].get("has_gold_solution")]
    if steps:
        print(f"\nSolution steps (gold-solution puzzles only): "
              f"min={min(steps)}, max={max(steps)}, avg={sum(steps)/len(steps):.1f}")

    # Card script coverage
    if has_scripts:
        with_scripts = sum(1 for inst in dataset if inst["cards_with_scripts"])
        total_scripted = sum(len(inst["cards_with_scripts"]) for inst in dataset)
        total_cards = sum(len(inst["card_ids"]) for inst in dataset)
        print(f"\nCard scripts: {total_scripted}/{total_cards} unique card-puzzle pairs have Lua scripts")
        print(f"  ({with_scripts}/{len(dataset)} puzzles have at least one scripted card)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
