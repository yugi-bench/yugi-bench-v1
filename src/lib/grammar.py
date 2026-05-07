"""Action-sequence grammar — the response-verb pattern reference.

Yu-Gi-Oh game actions decompose into predictable sequences of engine
pending-decisions: a "Normal Summon" intent expands to ``select_idlecmd
+ select_place + select_chain``, a fusion summon expands further, etc.
This module documents that grammar in a form the model can consume.

Both release modes consume it through ``lib.prompt_builder``. The
grammar text adapts mildly to ``mode``:
- ``mode="bulk"`` (n-attempts bulk mode): "you submit one JSON list,
  the evaluator runs it; chain-window tolerance forgives extras and
  missed optional chains".
- ``mode="interactive"`` (fully interactive mode): "you submit one or
  more actions per turn against the engine; if you batch multiple
  actions in a single turn, the harness auto-declines any chain
  window between them unless you include an explicit `select_chain`
  action".

The verb body (which prompts decompose into which sequences, the
index semantics, the available verb list) is identical across modes.
"""

from __future__ import annotations

from engine.tools import TOOL_TO_HARNESS_METHOD, TOOLS

_OPENING = """\
## Response Verbs

The engine drives the duel and halts at each **pending decision** \
— a prompt from the OCG rules engine asking you to make a single \
choice.  Each call has the form

```json
{"tool": "<verb>", "args": {...}}
```

The verbs below correspond one-to-one to the engine's internal \
`field::process(Processors::X&)` hooks.  The `args` object must \
match the verb's argument shape exactly — no fabrications, no \
DSL-style high-level actions.
"""


_AUTO_OPPONENT = """\
### You play player 0; opponent decisions are auto-resolved

You submit decisions **only for player 0** (yourself).  The engine's \
auto-opponent answers all opponent-side decisions with a passive \
policy: decline every chain offered to them, decline every optional \
yes/no prompt, and take the first legal choice for anything forced.  \
You do **not** include any decisions for player 1 in your solution, \
and you do **not** need to predict whether they will chain to your \
plays — they won't, unless the engine forces them to.  Plan purely \
around your own turn.
"""


_CHAIN_TOLERANCE_BULK = """\
### Chain-window tolerance (emit them liberally)

The evaluator is **tolerant about optional chain windows** and \
forgives two predictable mismatches in chain-window placement:
- A `{"tool": "select_chain", "args": {"index": null}}` in your \
list that the engine skipped (because no triggers fired) is \
silently **dropped** — not an error.
- An engine-emitted optional `select_chain` that your list didn't \
anticipate is silently **auto-declined** — your next non-chain \
action still runs against the post-chain state.

Practical implication: **when in doubt, include a null \
`select_chain` after every play.**  Extras are free; misses are \
free (for optional chains only).  Forced chains (`forced=true`) \
and every non-chain decision type remain strict.
"""


_CHAIN_AUTO_DECLINE_INTERACTIVE = """\
### Chain auto-decline (when you batch actions)

If you submit MULTIPLE actions in a single response (i.e. several \
tool_use blocks in one turn), the harness dispatches them \
sequentially against the engine.  Any `select_chain` window the \
engine raises BETWEEN your batched actions is **automatically \
declined** (`index=null`) UNLESS you included an explicit \
`select_chain` action targeting that window.  This lets you script \
deterministic sequences (e.g. summon → place → set position) in \
one turn without writing the trivial chain declines yourself.

If you submit a SINGLE action per response, no auto-decline fires \
— each chain window comes back to you for explicit handling.  Use \
single-action turns when you genuinely need to think about whether \
to chain (e.g. responding to an opponent's set spell/trap \
activation, deciding optimal effect-card timing, etc.).

Forced chains (`forced=true`) are NEVER auto-declined — they \
always come back to you for an explicit numeric pick.
"""


_DECOMPOSITIONS = """\
### How a turn decomposes into decisions

A single gameplay intent ('Normal Summon La Jinn to zone 2') almost \
always expands into **three or more** pending decisions.  The \
scenarios below cover the vast majority of what you'll emit.

**Phase transition (Main → Battle → End)**
```
  {"tool": "select_idlecmd", "args": {"command": "to_battle_phase"}}
  {"tool": "select_chain",   "args": {"index": null}}    // opponent declines
```
The `select_chain` after a phase transition is the engine offering \
a chain window to the opponent's set spell/traps.  With auto-opponent \
active you still emit this `select_chain` — but it's your own \
chain-window from your perspective (decline if you have nothing to \
chain).  Every phase transition emits exactly one chain window.

**Normal Summon, Level 1–4 (no tribute)**
```
  {"tool": "select_idlecmd", "args": {"command": "summon", "index": N}}
  {"tool": "select_place",   "args": {"places": [{"player": 0, "location": 4, "sequence": S}]}}
  {"tool": "select_chain",   "args": {"index": null}}
```
`N` is the offset into `summon_cards` of the pending decision (NOT \
your hand index).  `S` is a free monster zone 0–4.

**Normal Summon, Level 5–6 (1 tribute)**
```
  {"tool": "select_idlecmd", "args": {"command": "summon", "index": N}}
  {"tool": "select_tribute", "args": {"indices": [T]}}   // T indexes the tribute prompt
  {"tool": "select_place",   "args": {"places": [{"player": 0, "location": 4, "sequence": S}]}}
  {"tool": "select_chain",   "args": {"index": null}}
```
Level 7+ uses `"indices": [T1, T2]` for two tributes.  The \
`select_tribute` prompt's `cards` list is your own face-up/face-down \
monsters you may tribute.

**Set a monster (face-down DEF)**
```
  {"tool": "select_idlecmd", "args": {"command": "set_monster", "index": N}}
  {"tool": "select_place",   "args": {"places": [{"player": 0, "location": 4, "sequence": S}]}}
  {"tool": "select_chain",   "args": {"index": null}}
```

**Set a spell / trap from hand**
```
  {"tool": "select_idlecmd", "args": {"command": "set_spell", "index": N}}
  {"tool": "select_place",   "args": {"places": [{"player": 0, "location": 8, "sequence": S}]}}
```
No chain window after a Set — the card is face-down and hasn't \
triggered anything.  `location=8` is the spell/trap zone.

**Activate a Normal / Quick-Play Spell from hand (no explicit target)**
```
  {"tool": "select_idlecmd", "args": {"command": "activate", "index": N}}
  {"tool": "select_place",   "args": {"places": [{"player": 0, "location": 8, "sequence": S}]}}
  {"tool": "select_chain",   "args": {"index": null}}    // chain window before resolution
  // If the card has a cost (discard, pay LP, tribute) the engine
  // asks via select_card / select_tribute / select_option BEFORE
  // the final chain.  Each Spell's card text tells you its cost.
  {"tool": "select_chain",   "args": {"index": null}}    // chain window after resolution
```
**Critical — hand vs field location governs `select_place`:**
  - If the activate option's card location is **hand**, the engine \
emits `select_place` IMMEDIATELY after `select_idlecmd` (the card \
moves onto the spell/trap zone before resolving).  Emit it.
  - If the location is **spell_zone** (card is already set on the \
field), the engine skips `select_place` and goes directly to the \
next prompt (usually a `select_chain` or `select_card` for the \
effect's target).  Do NOT emit `select_place`.
Miss this distinction and every subsequent decision is off by one.

**Activate a Spell / Trap that targets a card on the field**
```
  {"tool": "select_idlecmd", "args": {"command": "activate", "index": N}}
  {"tool": "select_place",   "args": {"places": [{"player": 0, "location": 8, "sequence": S}]}}  // IF from hand; omit IF from field
  {"tool": "select_card",    "args": {"indices": [K1]}}  // target 1
  {"tool": "select_card",    "args": {"indices": [K2]}}  // target 2 or cost — MANY spells emit 2+ select_cards
  {"tool": "select_chain",   "args": {"index": null}}
  {"tool": "select_chain",   "args": {"index": null}}
```
**Many Spells emit MULTIPLE `select_card` prompts** for a single \
activation — one per cost and one per target.  For example:
  - *Tribute to the Doomed*: emits 2× select_card — the first for \
the card to discard (index into your hand list), the second for \
the monster to destroy (index into the field-wide targeting list).
  - *Pot of Avarice*: emits 1× select_card listing your GY \
monsters, with min=max=5 (you must pick exactly 5 indices).
  - *Smashing Ground* / *Mystical Space Typhoon*: usually 1× \
select_card (pure target, no cost).
  - *Raigeki* (wipe all): 0× select_card (the effect has no choice).
Plan each spell's pending-decision stream from its printed text: \
costs → targets → chain-windows → resolution → one more chain.

**Activate a set Spell / Trap on the field**
The flow is identical to activating from hand, except the card is \
already on the field, so `select_place` is NOT emitted.  Cost + \
target prompts still apply.

**Flip Summon / change battle position (repos)**
```
  {"tool": "select_idlecmd", "args": {"command": "repos", "index": N}}
  {"tool": "select_chain",   "args": {"index": null}}
```
`N` indexes `repos_cards` (your face-down DEF monsters and your \
face-up monsters eligible to change position).  The engine flips \
the position automatically — no `select_position` needed.

**Fusion / Synchro / Xyz / Link Summon (via an effect that summons \
from the Extra Deck)**
```
  // First activate the triggering spell/effect (e.g. Polymerization):
  {"tool": "select_idlecmd", "args": {"command": "activate", "index": N}}
  // Effect prompts you to pick the Extra-Deck monster to summon:
  {"tool": "select_card",    "args": {"indices": [X]}}   // X into fusion-target list
  // Then pick the required materials:
  {"tool": "select_card",    "args": {"indices": [M1, M2, ...]}}
  // Pick the position of the summoned monster:
  {"tool": "select_position", "args": {"position": 1}}   // 1=face-up ATK
  // Chain window after summon:
  {"tool": "select_chain",   "args": {"index": null}}
```
Synchro summons follow the same shape but the materials are a Tuner \
plus non-Tuners; you pick them as a single `select_card` with \
indices equal to the required combination.  Xyz summon materials \
need `select_unselect_card` (one pick at a time, finishable).  Link \
summon uses `select_card` / `select_unselect_card` depending on the \
summoning proc.

**Xyz Summon — one-pick-at-a-time material selection**
```
  // ... after the Xyz target is picked ...
  {"tool": "select_unselect_card", "args": {"index": 0}}   // pick the first material
  {"tool": "select_unselect_card", "args": {"index": 1}}   // pick the second
  {"tool": "select_unselect_card", "args": {"index": null}} // null = finish
```
The `cards` list in `select_unselect_card` has both `selectable_cards` \
(not yet picked) and `selected_cards` (already picked — re-picking \
un-selects).  Use `null` to finish when you've selected enough.

**Battle Phase: declare an attack against a monster**
```
  {"tool": "select_battlecmd", "args": {"command": "attack", "index": A}}
  // `A` indexes attackable_cards — the attacker.
  // (Battle-replay chain window:)
  {"tool": "select_chain",    "args": {"index": null}}
  // (Damage-step chain window — opponent's traps could fire here:)
  {"tool": "select_chain",    "args": {"index": null}}
  // Control returns to select_battlecmd (attack another, to_m2, to_ep).
```

**Battle Phase: declare a DIRECT attack (opp has no monsters)**
```
  {"tool": "select_battlecmd", "args": {"command": "attack", "index": A}}
  {"tool": "select_chain",    "args": {"index": null}}
  // No damage-step chain window — no defender means no trigger
  // effects can fire.  Control returns directly to select_battlecmd.
```
Key distinction: direct attacks emit **one** post-attack \
`select_chain`, while monster-on-monster attacks emit **two** (the \
battle-replay window plus the damage-step window).  Misreading \
this causes an off-by-one failure immediately after the attack.  \
Check the opponent's `monster_zone` in the game state: if empty, \
treat every attack as direct.

**Battle Phase: end / go to Main Phase 2**
```
  {"tool": "select_battlecmd", "args": {"command": "to_main_phase_2"}}
  {"tool": "select_chain",    "args": {"index": null}}
  // Now you're in M2 — select_idlecmd again.
  // Or, to end the turn directly:
  {"tool": "select_battlecmd", "args": {"command": "to_end_phase"}}
  {"tool": "select_chain",    "args": {"index": null}}
```
"""


_INDEX_SEMANTICS = """\
### Index semantics — where your `index` values come from

The engine maintains per-decision lists that you index into, and \
these lists are NOT identical to your hand / field / graveyard \
ordering.  For:
- `select_idlecmd` / `select_battlecmd`: the pending decision carries \
an `options` list grouped by command type (summon / sp_summon / \
repos / set_monster / set_spell / activate).  Your `index` is the \
0-based position **within that command's group**, in the engine's \
enumeration order:
    - `summon` / `set_monster`: enumerates your hand, top-first.  \
Only cards whose summon conditions are met appear.  Level 5-6 \
require ≥1 tributable monster; Level 7+ require ≥2.
    - `activate`: enumerates **hand first, then field set cards**.  \
Cards with multiple activatable effects appear once per effect (so \
the same card_id can appear at multiple consecutive indices with \
different `desc` payloads).  If your hand has [CardA, CardA, CardB, \
CardC-set-field] and all are activatable, indices will be 0,1,2 for \
hand + 3 for field.  Duplicate cards in hand each count.
    - `repos`: enumerates your face-up monsters + face-down DEF \
monsters, but ONLY those eligible (haven't changed position this \
turn, weren't summoned this turn).
    - `sp_summon`: enumerates cards in your hand / GY / banished \
zone / extra deck that can self-special-summon right now.
- `select_card` / `select_tribute` / `select_unselect_card`: the \
pending decision carries a `cards` array built by the activating \
card's effect.  Target prompts typically list field-wide monsters \
(both sides) or all cards meeting the filter.  Cost prompts \
(discard) list your hand.  You can't know exact ordering ahead of \
time — in ambiguous cases, `0` is the safest guess; if the card is \
the only legal pick, any index ≥ the size is rejected.
- `select_place`: you're picking YOUR zone to place a newly-summoned \
/ newly-activated card.  `player=0`, `location=4` for MZ or `8` for \
SZ, `sequence=0..4` for normal zones.  Avoid already-occupied \
zones (engine rejects).

### Rules of the pending-decision stream

- **MSG_WIN preempts everything.**  If one of your plays reduces \
the opponent's LP to 0 (or decks them out), the engine emits \
MSG_WIN immediately and terminates.  You do NOT emit further \
`select_chain` or `select_battlecmd` after a winning attack — the \
engine stops asking.
- **Forced chains** (no `null` allowed): when the opponent has a \
mandatory trigger effect (e.g. a Continuous Trap that MUST \
activate), `select_chain` arrives with `forced=true` in its \
pending.  In that case you must pick a numeric index.  Auto-opponent \
handles *most* forced chains on the opponent's side, but if YOUR \
card forces you to respond to your own trigger, include it.
- **Multi-round chain resolution**: if you activate effect A and \
opponent (auto) declines, the engine may still prompt a second \
`select_chain` as it progresses through resolution steps.  When in \
doubt, emit `{"index": null}` — too many spurious declines are \
MUCH better than too few (the engine merely replays the decision).
- **Optional-trigger yes/no**: when a card like Marauding Captain \
or Breaker the Magical Warrior has an optional summon-trigger \
effect, the engine asks `select_effectyn` (`accept: bool`) for \
*you*.  Emit it.  Auto-opponent handles the opponent's side.
- **Target range**: `select_card` prompts carry `min_` and `max_` \
in their pending-decision structure (not visible in this prompt).  \
The engine infers this from the targeting card; emit exactly the \
required number of indices.

### Card-selection notation

- `indices` are 0-based offsets into the `cards` list of the \
pending decision (not into your hand or any field zone).
- `places` are `{player, location, sequence}` triples.  `location` \
uses the OCG bitmask — `0x04`/`4` = monster zone (MZ), `0x08`/`8` = \
spell/trap zone (SZ).  `sequence` is the 0-based zone index: MZ \
uses 0..4 (main zones) and 5,6 (extra monster zones); SZ uses 0..4 \
(spell/trap zones) and 6,7 (pendulum zones).  `player: 0` always — \
you're placing YOUR card in YOUR zone.
- `position` bitmask — `0x1` face-up ATK, `0x2` face-down ATK \
(rare), `0x4` face-up DEF, `0x8` face-down DEF.
- `command` for `select_idlecmd` is one of: `summon` | `sp_summon` | \
`activate` | `repos` | `set_monster` | `set_spell` | \
`to_battle_phase` | `to_end_phase` | `shuffle_hand` — and must \
match what the pending decision offers.  `select_battlecmd` \
commands are: `attack` | `activate` | `to_main_phase_2` | \
`to_end_phase`.
"""


def _render_verb_list() -> str:
    lines: list[str] = ["### Available verbs", ""]
    for tool in TOOLS:
        if tool["name"] not in TOOL_TO_HARNESS_METHOD:
            continue
        desc = (tool.get("description") or "").strip().split("\n", 1)[0]
        lines.append(f"- **`{tool['name']}`** — {desc}")
        schema = tool.get("input_schema") or {}
        props = schema.get("properties") or {}
        if props:
            args_summary = ", ".join(f"`{k}: {p.get('type', '?')}`" for k, p in props.items())
            lines.append(f"  - args: {args_summary}")
    lines.append("")
    lines.append(
        "Always pick the responder named by the pending decision — other verbs will be rejected."
    )
    return "\n".join(lines)


def render_action_grammar(mode: str = "bulk") -> str:
    """Render the response-verb action grammar.

    ``mode`` is one of:
    - ``"bulk"``: n-attempts bulk mode — model submits a JSON list
      once (or up to N times when N>1); evaluator runs it with
      chain-window tolerance.
    - ``"interactive"``: fully interactive mode — model submits actions
      per turn; batched actions auto-decline chain windows in between.
    """
    if mode not in ("bulk", "interactive"):
        raise ValueError(f"unknown grammar mode: {mode!r}")
    chain_section = _CHAIN_TOLERANCE_BULK if mode == "bulk" else _CHAIN_AUTO_DECLINE_INTERACTIVE
    return "\n".join(
        [
            _OPENING,
            _AUTO_OPPONENT,
            chain_section,
            _DECOMPOSITIONS,
            _INDEX_SEMANTICS,
            _render_verb_list(),
        ]
    )
