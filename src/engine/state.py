"""Observation builder — render engine state + pending decision.

The observation is a JSON-safe dict suitable for prompting an LLM.  It
splits into two halves:

  ``board``    — from ``OCGEngine.get_snapshot`` + ``build_observation``.
                 Perspective-filtered (hidden opponent info is stripped).
  ``decision`` — a structured view of the current SELECT message.  Only
                 legal responses are enumerated so the responder can pick
                 a valid action without guessing the wire format.

A None decision indicates the duel has ended; ``winner`` will be set.
"""

from __future__ import annotations

from typing import Any

from .core import (
    MSG_ANNOUNCE_ATTRIB,
    MSG_ANNOUNCE_CARD,
    MSG_ANNOUNCE_NUMBER,
    MSG_ANNOUNCE_RACE,
    MSG_ROCK_PAPER_SCISSORS,
    MSG_SELECT_BATTLECMD,
    MSG_SELECT_CARD,
    MSG_SELECT_CHAIN,
    MSG_SELECT_COUNTER,
    MSG_SELECT_DISFIELD,
    MSG_SELECT_EFFECTYN,
    MSG_SELECT_IDLECMD,
    MSG_SELECT_OPTION,
    MSG_SELECT_PLACE,
    MSG_SELECT_POSITION,
    MSG_SELECT_SUM,
    MSG_SELECT_TRIBUTE,
    MSG_SELECT_UNSELECT_CARD,
    MSG_SELECT_YESNO,
    MSG_SORT_CARD,
    MSG_SORT_CHAIN,
    AnnounceAttrib,
    AnnounceCard,
    AnnounceNumber,
    AnnounceRace,
    BattleCmd,
    CardDB,
    IdleCmd,
    IdleCmdOption,
    SelectCard,
    SelectChain,
    SelectCounter,
    SelectEffectYn,
    SelectOption,
    SelectPlace,
    SelectPosition,
    SelectSum,
    SelectUnselectCard,
    SelectYesNo,
    SortCard,
    build_observation,
    render_location,
    render_position,
    resolve_desc_id,
)
from .harness import Harness, PendingDecision


# Reverse bitmasks used by SELECT_PLACE — see playerop.cpp:504-596.  The
# 32-bit flag is the set of *unavailable* places; a 1 bit means "cannot
# place here".  Layout:
#   bits  0..6   : self MZONE slots 0..6 (extra monster zones are 5,6)
#   bits  8..12  : self SZONE slots 0..4
#   bits  14,15  : self pendulum zones (sz slots 6,7)
#   bits 16..22  : opponent MZONE
#   bits 24..28  : opponent SZONE
#   bits 30,31   : opponent pendulum
def _available_places(flag: int, player: int) -> list[dict]:
    """Enumerate legal (player, location, sequence) picks for SELECT_PLACE."""
    avail = ~flag & 0xFFFFFFFF
    places: list[dict] = []
    # self side
    for seq in range(7):
        if avail & (1 << seq):
            places.append({"player": player, "location": "monster_zone", "sequence": seq})
    for seq in range(5):
        if avail & (1 << (seq + 8)):
            places.append({"player": player, "location": "spell_zone", "sequence": seq})
    for seq in range(2):
        if avail & (1 << (seq + 14)):
            places.append({"player": player, "location": "pendulum_zone", "sequence": seq + 6})
    # opponent
    opp = 1 - player
    for seq in range(7):
        if avail & (1 << (seq + 16)):
            places.append({"player": opp, "location": "monster_zone", "sequence": seq})
    for seq in range(5):
        if avail & (1 << (seq + 24)):
            places.append({"player": opp, "location": "spell_zone", "sequence": seq})
    for seq in range(2):
        if avail & (1 << (seq + 30)):
            places.append({"player": opp, "location": "pendulum_zone", "sequence": seq + 6})
    return places


def _card_label(card: dict | None, card_db: CardDB) -> dict:
    """One-line human-ish label for a card reference."""
    if not card:
        return {}
    code = card.get("code", 0)
    label = {
        "code": code,
        "name": (card_db.get(code) or {}).get("name", f"Card#{code}") if code else "<face-down>",
        "controller": "you" if card.get("con") == 0 else "opponent",
        "location": render_location(card.get("loc", 0)),
        "sequence": card.get("seq", 0),
        "index": card.get("index"),
    }
    if "pos" in card and card["pos"]:
        label["position"] = render_position(card["pos"])
    return label


def build_decision(decision: PendingDecision, card_db: CardDB) -> dict:
    """Render a SELECT message into a legal-moves view."""
    msg_type = decision.msg_type
    p = decision.parsed

    out: dict[str, Any] = {
        "msg_type": decision.msg_name,
        "player": decision.player,
    }

    if msg_type == MSG_SELECT_IDLECMD:
        assert isinstance(p, IdleCmd)
        out["choices"] = _idle_choices(p, card_db)
        out["responder"] = "select_idlecmd"
    elif msg_type == MSG_SELECT_BATTLECMD:
        assert isinstance(p, BattleCmd)
        out["choices"] = _battle_choices(p, card_db)
        out["responder"] = "select_battlecmd"
    elif msg_type == MSG_SELECT_EFFECTYN:
        assert isinstance(p, SelectEffectYn)
        card = {"code": p.code, "con": p.con, "loc": p.loc, "seq": p.seq, "pos": p.pos}
        out["about"] = _card_label(card, card_db)
        out["description"] = resolve_desc_id(p.desc, card_db)
        out["responder"] = "select_effectyn"
    elif msg_type == MSG_SELECT_YESNO:
        assert isinstance(p, SelectYesNo)
        out["description"] = resolve_desc_id(p.desc, card_db)
        out["responder"] = "select_yesno"
    elif msg_type == MSG_SELECT_OPTION:
        assert isinstance(p, SelectOption)
        out["options"] = [resolve_desc_id(d, card_db) for d in p.options]
        out["responder"] = "select_option"
    elif msg_type == MSG_SELECT_CARD:
        assert isinstance(p, SelectCard)
        out["min"] = p.min_
        out["max"] = p.max_
        out["cancelable"] = p.cancelable
        out["cards"] = [_card_label(c, card_db) for c in p.cards]
        out["responder"] = "select_card"
    elif msg_type == MSG_SELECT_TRIBUTE:
        assert isinstance(p, SelectCard)
        out["min"] = p.min_
        out["max"] = p.max_
        out["cancelable"] = p.cancelable
        out["cards"] = [_card_label(c, card_db) for c in p.cards]
        out["responder"] = "select_tribute"
    elif msg_type == MSG_SELECT_CHAIN:
        assert isinstance(p, SelectChain)
        out["forced"] = p.forced
        out["cards"] = [
            {**_card_label(c, card_db), "description": resolve_desc_id(c["desc"], card_db)}
            for c in p.cards
        ]
        out["responder"] = "select_chain"
    elif msg_type == MSG_SELECT_POSITION:
        assert isinstance(p, SelectPosition)
        out["about_code"] = p.code
        out["about_name"] = (card_db.get(p.code) or {}).get("name", f"Card#{p.code}")
        out["allowed_positions"] = [pos for pos in (0x1, 0x2, 0x4, 0x8) if p.positions & pos]
        out["allowed_positions_named"] = [
            render_position(pos) for pos in (0x1, 0x2, 0x4, 0x8) if p.positions & pos
        ]
        out["responder"] = "select_position"
    elif msg_type in (MSG_SELECT_PLACE, MSG_SELECT_DISFIELD):
        assert isinstance(p, SelectPlace)
        out["count"] = p.min_
        out["disable_field"] = msg_type == MSG_SELECT_DISFIELD
        out["places"] = _available_places(p.field_mask, p.player)
        out["responder"] = "select_place"
    elif msg_type == MSG_SELECT_SUM:
        assert isinstance(p, SelectSum)
        out["acc"] = p.sumval
        out["mode"] = "exact" if p.mode == 0 else "min_total"
        out["min"] = p.min_
        out["max"] = p.max_
        out["mandatory_cards"] = [_card_label(c, card_db) for c in p.mandatory_cards]
        out["optional_cards"] = [_card_label(c, card_db) for c in p.optional_cards]
        out["responder"] = "select_sum"
    elif msg_type == MSG_SELECT_UNSELECT_CARD:
        assert isinstance(p, SelectUnselectCard)
        out["min"] = p.min_
        out["max"] = p.max_
        out["finishable"] = p.finishable
        out["cancelable"] = p.cancelable
        out["selectable_cards"] = [_card_label(c, card_db) for c in p.selectable_cards]
        out["selected_cards"] = [_card_label(c, card_db) for c in p.selected_cards]
        out["index_hint"] = (
            "0..{n_sel}-1 picks a selectable card; "
            "{n_sel}..{n_sel}+{n_selected}-1 un-picks a selected card"
        ).format(n_sel=len(p.selectable_cards), n_selected=len(p.selected_cards))
        out["responder"] = "select_unselect_card"
    elif msg_type == MSG_SELECT_COUNTER:
        assert isinstance(p, SelectCounter)
        out["counter_type"] = p.counter_type
        out["total_needed"] = p.count
        out["cards"] = [{**_card_label(c, card_db), "counter": c["counter"]} for c in p.cards]
        out["responder"] = "select_counter"
    elif msg_type in (MSG_SORT_CARD, MSG_SORT_CHAIN):
        assert isinstance(p, SortCard)
        out["cards"] = [_card_label(c, card_db) for c in p.cards]
        out["responder"] = "sort_card"
    elif msg_type == MSG_ANNOUNCE_RACE:
        assert isinstance(p, AnnounceRace)
        out["count"] = p.count
        out["available_mask"] = p.available
        out["responder"] = "announce_race"
    elif msg_type == MSG_ANNOUNCE_ATTRIB:
        assert isinstance(p, AnnounceAttrib)
        out["count"] = p.count
        out["available_mask"] = p.available
        out["responder"] = "announce_attribute"
    elif msg_type == MSG_ANNOUNCE_CARD:
        assert isinstance(p, AnnounceCard)
        out["opcodes"] = p.opcodes
        out["responder"] = "announce_card"
    elif msg_type == MSG_ANNOUNCE_NUMBER:
        assert isinstance(p, AnnounceNumber)
        out["numbers"] = p.numbers
        out["responder"] = "announce_number"
    elif msg_type == MSG_ROCK_PAPER_SCISSORS:
        out["responder"] = "rock_paper_scissors"
    else:
        out["raw"] = getattr(p, "__dict__", p)
    return out


def _idle_choices(cmd: IdleCmd, card_db: CardDB) -> list[dict]:
    choices: list[dict] = []
    by_cat: dict[int, list[IdleCmdOption]] = {}
    for opt in cmd.options:
        by_cat.setdefault(opt.category, []).append(opt)
    for cat, command in (
        (0, "summon"),
        (1, "sp_summon"),
        (2, "repos"),
        (3, "set_monster"),
        (4, "set_spell"),
        (5, "activate"),
    ):
        for opt in by_cat.get(cat, []):
            entry: dict = {
                "command": command,
                "index": opt.index,
                "card": {
                    "code": opt.code,
                    "name": (card_db.get(opt.code) or {}).get("name", f"Card#{opt.code}"),
                    "controller": "you" if opt.con == 0 else "opponent",
                    "location": render_location(opt.loc),
                    "sequence": opt.seq,
                },
            }
            if cat == 5 and opt.desc:
                entry["description"] = resolve_desc_id(opt.desc, card_db)
            choices.append(entry)
    if cmd.can_battle_phase:
        choices.append({"command": "to_battle_phase"})
    if cmd.can_end_phase:
        choices.append({"command": "to_end_phase"})
    if cmd.can_shuffle:
        choices.append({"command": "shuffle_hand"})
    return choices


def _battle_choices(cmd: BattleCmd, card_db: CardDB) -> list[dict]:
    choices: list[dict] = []
    by_cat: dict[int, list] = {0: [], 1: []}
    for opt in cmd.options:
        by_cat.setdefault(opt.category, []).append(opt)
    for opt in by_cat[0]:
        entry = {
            "command": "activate",
            "index": opt.index,
            "card": {
                "code": opt.code,
                "name": (card_db.get(opt.code) or {}).get("name", f"Card#{opt.code}"),
                "controller": "you" if opt.con == 0 else "opponent",
                "location": render_location(opt.loc),
                "sequence": opt.seq,
            },
        }
        if opt.desc:
            entry["description"] = resolve_desc_id(opt.desc, card_db)
        choices.append(entry)
    for opt in by_cat[1]:
        entry = {
            "command": "attack",
            "index": opt.index,
            "card": {
                "code": opt.code,
                "name": (card_db.get(opt.code) or {}).get("name", f"Card#{opt.code}"),
                "controller": "you" if opt.con == 0 else "opponent",
                "location": render_location(opt.loc),
                "sequence": opt.seq,
            },
            "direct_attackable": bool(opt.direct_attackable),
        }
        choices.append(entry)
    if cmd.can_main2:
        choices.append({"command": "to_main_phase_2"})
    if cmd.can_end_phase:
        choices.append({"command": "to_end_phase"})
    return choices


def build_state(
    harness: Harness,
    card_db: CardDB,
    *,
    perspective: int = 0,
    include_decision: bool = True,
    events: list[dict] | None = None,
) -> dict:
    """Full observation for this turn: board + pending decision + events."""
    snapshot = harness.engine.get_snapshot()
    obs = build_observation(
        snapshot,
        perspective=perspective,
        card_db=card_db,
        phase=harness.state.phase or None,
        turn_player=harness.state.turn_player,
        turn_count=harness.state.turn_count,
    )
    obs["game_over"] = harness.state.game_over
    if harness.state.winner is not None:
        obs["winner"] = "you" if harness.state.winner == perspective else "opponent"
        if harness.state.win_reason is not None:
            obs["win_reason"] = harness.state.win_reason
    if include_decision and harness.pending is not None:
        obs["decision"] = build_decision(harness.pending, card_db)
    if events:
        obs["events_since_last_decision"] = events
    return obs
