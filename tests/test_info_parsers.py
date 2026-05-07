"""Info-message wire-format parser tests.

Every non-SELECT ``MSG_*`` parser — i.e. the messages the engine emits
during regular duel progression — is exercised here. Wire bytes are
hand-crafted per upstream ``yugioh/edopro/ocgcore/field.cpp`` /
``processor.cpp`` emitter sites so any future drift in field order,
widths, or endianness fails loudly.

Each test builds a ``bytes`` stream matching the upstream C++ emitter,
then feeds it to ``OCGEngine._parse_single_message`` via
``MessageReader``. We bypass ``OCGEngine.__init__`` because the info
parsers are pure — they only read from a ``MessageReader`` and never
touch ``self.lib``.
"""
from __future__ import annotations

import struct

import pytest

import engine.core as engine
from engine.core import MessageReader, OCGEngine


# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------
@pytest.fixture
def eng() -> OCGEngine:
    return OCGEngine.__new__(OCGEngine)


def loc_info(con: int, loc: int, seq: int, pos: int) -> bytes:
    """C loc_info struct: u8, u8, u32, u32 (little-endian, 10 bytes)."""
    return struct.pack("<BBII", con, loc, seq, pos)


def u8(v: int) -> bytes:  return struct.pack("<B", v)
def u16(v: int) -> bytes: return struct.pack("<H", v)
def u32(v: int) -> bytes: return struct.pack("<I", v)
def u64(v: int) -> bytes: return struct.pack("<Q", v)


def _parse(eng: OCGEngine, msg_type: int, body: bytes):
    return eng._parse_single_message(msg_type, MessageReader(body))


# ---------------------------------------------------------------------------
# Life-point / turn / phase family — short fixed-layout messages
# ---------------------------------------------------------------------------
def test_msg_win(eng: OCGEngine) -> None:
    body = u8(1) + u8(3)  # player 1 wins, reason 3 (deck-out)
    out = _parse(eng, engine.MSG_WIN, body)
    assert out == {"winner": 1, "reason": 3}


def test_msg_new_turn(eng: OCGEngine) -> None:
    out = _parse(eng, engine.MSG_NEW_TURN, u8(0))
    assert out == {"player": 0}


def test_msg_new_phase(eng: OCGEngine) -> None:
    out = _parse(eng, engine.MSG_NEW_PHASE, u16(engine.PHASE_BATTLE))
    assert out == {"phase": engine.PHASE_BATTLE}


def test_msg_damage(eng: OCGEngine) -> None:
    out = _parse(eng, engine.MSG_DAMAGE, u8(0) + u32(1800))
    assert out == {"player": 0, "amount": 1800}


def test_msg_recover(eng: OCGEngine) -> None:
    out = _parse(eng, engine.MSG_RECOVER, u8(1) + u32(500))
    assert out == {"player": 1, "amount": 500}


def test_msg_lpupdate(eng: OCGEngine) -> None:
    out = _parse(eng, engine.MSG_LPUPDATE, u8(1) + u32(6200))
    assert out == {"player": 1, "lp": 6200}


def test_msg_pay_lpcost(eng: OCGEngine) -> None:
    out = _parse(eng, engine.MSG_PAY_LPCOST, u8(0) + u32(1000))
    assert out == {"player": 0, "amount": 1000}


# ---------------------------------------------------------------------------
# Hint family
# ---------------------------------------------------------------------------
def test_msg_hint(eng: OCGEngine) -> None:
    body = u8(engine.HINT_EVENT) + u8(0) + u64(0x12345678_90ABCDEF)
    out = _parse(eng, engine.MSG_HINT, body)
    assert out == {"type": engine.HINT_EVENT, "player": 0,
                   "data": 0x12345678_90ABCDEF}


def test_msg_card_hint(eng: OCGEngine) -> None:
    body = (u8(0) + u8(engine.LOCATION_MZONE) + u32(2) + u32(engine.POS_FACEUP_ATTACK)
            + u8(engine.CHINT_DESC_ADD) + u64(999))
    out = _parse(eng, engine.MSG_CARD_HINT, body)
    assert out == {"con": 0, "loc": engine.LOCATION_MZONE, "seq": 2,
                   "pos": engine.POS_FACEUP_ATTACK,
                   "type": engine.CHINT_DESC_ADD, "value": 999}


# ---------------------------------------------------------------------------
# Confirm / Shuffle family
# ---------------------------------------------------------------------------
def test_msg_confirm_cards(eng: OCGEngine) -> None:
    player = 0
    body = u8(player) + u32(2)
    body += u32(0xAAAA) + u8(1) + u8(engine.LOCATION_HAND) + u32(0)
    body += u32(0xBBBB) + u8(1) + u8(engine.LOCATION_HAND) + u32(1)
    out = _parse(eng, engine.MSG_CONFIRM_CARDS, body)
    assert out["player"] == 0
    assert out["cards"] == [
        {"code": 0xAAAA, "con": 1, "loc": engine.LOCATION_HAND, "seq": 0},
        {"code": 0xBBBB, "con": 1, "loc": engine.LOCATION_HAND, "seq": 1},
    ]


def test_msg_confirm_decktop(eng: OCGEngine) -> None:
    body = u8(0) + u32(1) + u32(0x1234) + u8(0) + u8(engine.LOCATION_DECK) + u32(0)
    out = _parse(eng, engine.MSG_CONFIRM_DECKTOP, body)
    assert out["player"] == 0
    assert len(out["cards"]) == 1
    assert out["cards"][0]["code"] == 0x1234


def test_msg_confirm_extratop(eng: OCGEngine) -> None:
    body = u8(1) + u32(0)
    out = _parse(eng, engine.MSG_CONFIRM_EXTRATOP, body)
    assert out == {"player": 1, "cards": []}


def test_msg_shuffle_deck(eng: OCGEngine) -> None:
    out = _parse(eng, engine.MSG_SHUFFLE_DECK, u8(1))
    assert out == {"player": 1}


def test_msg_shuffle_hand(eng: OCGEngine) -> None:
    body = u8(0) + u32(3) + u32(0x1111) + u32(0x2222) + u32(0x3333)
    out = _parse(eng, engine.MSG_SHUFFLE_HAND, body)
    assert out == {"player": 0, "codes": [0x1111, 0x2222, 0x3333]}


def test_msg_shuffle_extra(eng: OCGEngine) -> None:
    body = u8(1) + u32(2) + u32(0x7777) + u32(0x8888)
    out = _parse(eng, engine.MSG_SHUFFLE_EXTRA, body)
    assert out == {"player": 1, "codes": [0x7777, 0x8888]}


# ---------------------------------------------------------------------------
# Dice / coin
# ---------------------------------------------------------------------------
def test_msg_toss_coin(eng: OCGEngine) -> None:
    body = u8(0) + u8(3) + u8(1) + u8(0) + u8(1)
    out = _parse(eng, engine.MSG_TOSS_COIN, body)
    assert out == {"player": 0, "results": [1, 0, 1]}


def test_msg_toss_dice(eng: OCGEngine) -> None:
    body = u8(1) + u8(2) + u8(6) + u8(3)
    out = _parse(eng, engine.MSG_TOSS_DICE, body)
    assert out == {"player": 1, "results": [6, 3]}


# ---------------------------------------------------------------------------
# Hand rock-paper-scissors result — packed u8 (bits0..1=p0, bits2..3=p1)
# ---------------------------------------------------------------------------
def test_msg_hand_res(eng: OCGEngine) -> None:
    # p0=rock(1), p1=paper(2) → packed = 0b1001 = 9
    out = _parse(eng, engine.MSG_HAND_RES, u8(0b1001))
    assert out == {"hand0": 1, "hand1": 2}


def test_msg_hand_res_max(eng: OCGEngine) -> None:
    # p0=scissors(3), p1=scissors(3) → packed = 0b1111 = 15
    out = _parse(eng, engine.MSG_HAND_RES, u8(0b1111))
    assert out == {"hand0": 3, "hand1": 3}


# ---------------------------------------------------------------------------
# Start / retry
# ---------------------------------------------------------------------------
def test_msg_start(eng: OCGEngine) -> None:
    out = _parse(eng, engine.MSG_START, u8(0))
    assert out == {"type": 0}


def test_msg_retry(eng: OCGEngine) -> None:
    out = _parse(eng, engine.MSG_RETRY, b"")
    assert out == {}


# ---------------------------------------------------------------------------
# Movement family — the heart of provenance tracking
# ---------------------------------------------------------------------------
def test_msg_move(eng: OCGEngine) -> None:
    code = 0x00004ce5
    prev = loc_info(0, engine.LOCATION_HAND, 2, 0)
    curr = loc_info(0, engine.LOCATION_MZONE, 3, engine.POS_FACEUP_ATTACK)
    reason = engine.REASON_SUMMON
    body = u32(code) + prev + curr + u32(reason)
    out = _parse(eng, engine.MSG_MOVE, body)
    assert out["code"] == code
    assert out["previous"] == {"con": 0, "loc": engine.LOCATION_HAND,
                               "seq": 2, "pos": 0}
    assert out["current"] == {"con": 0, "loc": engine.LOCATION_MZONE,
                              "seq": 3, "pos": engine.POS_FACEUP_ATTACK}
    assert out["reason"] == engine.REASON_SUMMON


def test_msg_pos_change(eng: OCGEngine) -> None:
    # All three seq/prev_pos/cur_pos are u8 in this message (not u32).
    body = (u32(0x12345) + u8(0) + u8(engine.LOCATION_MZONE) + u8(1)
            + u8(engine.POS_FACEDOWN_DEFENSE) + u8(engine.POS_FACEUP_ATTACK))
    out = _parse(eng, engine.MSG_POS_CHANGE, body)
    assert out == {
        "code": 0x12345,
        "con": 0, "loc": engine.LOCATION_MZONE, "seq": 1,
        "prev_pos": engine.POS_FACEDOWN_DEFENSE,
        "cur_pos": engine.POS_FACEUP_ATTACK,
    }


def test_msg_set(eng: OCGEngine) -> None:
    body = u32(0xABCD) + loc_info(1, engine.LOCATION_SZONE, 0,
                                   engine.POS_FACEDOWN_DEFENSE)
    out = _parse(eng, engine.MSG_SET, body)
    assert out == {"code": 0xABCD, "con": 1, "loc": engine.LOCATION_SZONE,
                   "seq": 0, "pos": engine.POS_FACEDOWN_DEFENSE}


def test_msg_swap(eng: OCGEngine) -> None:
    body = (u32(0x1111) + loc_info(0, engine.LOCATION_MZONE, 0,
                                     engine.POS_FACEUP_ATTACK)
            + u32(0x2222) + loc_info(1, engine.LOCATION_MZONE, 2,
                                       engine.POS_FACEUP_ATTACK))
    out = _parse(eng, engine.MSG_SWAP, body)
    assert out["card1"]["code"] == 0x1111
    assert out["card1"]["con"] == 0
    assert out["card2"]["code"] == 0x2222
    assert out["card2"]["con"] == 1


def test_msg_field_disabled(eng: OCGEngine) -> None:
    # Bitmask of disabled zones (bit-per-zone scheme).
    out = _parse(eng, engine.MSG_FIELD_DISABLED, u32(0x000000A5))
    assert out == {"disabled_mask": 0x000000A5}


# ---------------------------------------------------------------------------
# Summoning family — pre-resolution announcements
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("msg_type", [
    engine.MSG_SUMMONING, engine.MSG_SPSUMMONING, engine.MSG_FLIPSUMMONING,
])
def test_msg_summoning_family(eng: OCGEngine, msg_type: int) -> None:
    body = u32(0x5566) + loc_info(0, engine.LOCATION_MZONE, 2,
                                    engine.POS_FACEUP_ATTACK)
    out = _parse(eng, msg_type, body)
    assert out["code"] == 0x5566
    assert out["loc"] == engine.LOCATION_MZONE


@pytest.mark.parametrize("msg_type", [
    engine.MSG_SUMMONED, engine.MSG_SPSUMMONED, engine.MSG_FLIPSUMMONED,
])
def test_msg_summoned_family_empty(eng: OCGEngine, msg_type: int) -> None:
    out = _parse(eng, msg_type, b"")
    assert out == {}


# ---------------------------------------------------------------------------
# Chain family
# ---------------------------------------------------------------------------
def test_msg_chaining(eng: OCGEngine) -> None:
    body = (u32(0x7777)
            + loc_info(1, engine.LOCATION_SZONE, 3, engine.POS_FACEUP_ATTACK)
            + u8(0) + u8(engine.LOCATION_MZONE) + u32(0)  # triggering loc
            + u64(0xDEADBEEF)                                # desc
            + u32(2))                                        # chain_count
    out = _parse(eng, engine.MSG_CHAINING, body)
    assert out["code"] == 0x7777
    assert out["con"] == 1
    assert out["loc"] == engine.LOCATION_SZONE
    assert out["triggering_con"] == 0
    assert out["triggering_loc"] == engine.LOCATION_MZONE
    assert out["triggering_seq"] == 0
    assert out["desc"] == 0xDEADBEEF
    assert out["chain_count"] == 2


@pytest.mark.parametrize("msg_type", [
    engine.MSG_CHAINED, engine.MSG_CHAIN_SOLVING, engine.MSG_CHAIN_SOLVED,
    engine.MSG_CHAIN_NEGATED, engine.MSG_CHAIN_DISABLED,
])
def test_msg_chain_count_only(eng: OCGEngine, msg_type: int) -> None:
    out = _parse(eng, msg_type, u8(3))
    assert out == {"chain_count": 3}


def test_msg_chain_end(eng: OCGEngine) -> None:
    out = _parse(eng, engine.MSG_CHAIN_END, b"")
    assert out == {}


# ---------------------------------------------------------------------------
# Selection-result family
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("msg_type", [
    engine.MSG_CARD_SELECTED, engine.MSG_BECOME_TARGET,
])
def test_msg_card_selected_family(eng: OCGEngine, msg_type: int) -> None:
    body = (u32(2)
            + loc_info(0, engine.LOCATION_MZONE, 1, engine.POS_FACEUP_ATTACK)
            + loc_info(1, engine.LOCATION_MZONE, 2, engine.POS_FACEUP_DEFENSE))
    out = _parse(eng, msg_type, body)
    assert len(out["cards"]) == 2
    assert out["cards"][0]["con"] == 0
    assert out["cards"][1]["con"] == 1


def test_msg_random_selected(eng: OCGEngine) -> None:
    body = (u8(0) + u32(1)
            + loc_info(0, engine.LOCATION_HAND, 0, 0))
    out = _parse(eng, engine.MSG_RANDOM_SELECTED, body)
    assert out["player"] == 0
    assert out["cards"][0]["loc"] == engine.LOCATION_HAND


# ---------------------------------------------------------------------------
# Draw — code + position per card
# ---------------------------------------------------------------------------
def test_msg_draw(eng: OCGEngine) -> None:
    body = (u8(0) + u32(2)
            + u32(0xAAAA) + u32(engine.POS_FACEDOWN_DEFENSE)
            + u32(0xBBBB) + u32(engine.POS_FACEDOWN_DEFENSE))
    out = _parse(eng, engine.MSG_DRAW, body)
    assert out["player"] == 0
    assert out["count"] == 2
    assert out["cards"] == [
        {"code": 0xAAAA, "position": engine.POS_FACEDOWN_DEFENSE},
        {"code": 0xBBBB, "position": engine.POS_FACEDOWN_DEFENSE},
    ]


# ---------------------------------------------------------------------------
# Equip / target family
# ---------------------------------------------------------------------------
def test_msg_equip(eng: OCGEngine) -> None:
    body = (loc_info(0, engine.LOCATION_SZONE, 1, engine.POS_FACEUP_ATTACK)
            + loc_info(0, engine.LOCATION_MZONE, 2, engine.POS_FACEUP_ATTACK))
    out = _parse(eng, engine.MSG_EQUIP, body)
    assert out["equip"]["loc"] == engine.LOCATION_SZONE
    assert out["target"]["loc"] == engine.LOCATION_MZONE


def test_msg_unequip(eng: OCGEngine) -> None:
    body = loc_info(1, engine.LOCATION_SZONE, 0, engine.POS_FACEUP_ATTACK)
    out = _parse(eng, engine.MSG_UNEQUIP, body)
    assert out == {"loc_info": {"con": 1, "loc": engine.LOCATION_SZONE,
                                 "seq": 0, "pos": engine.POS_FACEUP_ATTACK}}


@pytest.mark.parametrize("msg_type", [
    engine.MSG_CARD_TARGET, engine.MSG_CANCEL_TARGET,
])
def test_msg_card_target_family(eng: OCGEngine, msg_type: int) -> None:
    body = (loc_info(0, engine.LOCATION_MZONE, 0, engine.POS_FACEUP_ATTACK)
            + loc_info(1, engine.LOCATION_MZONE, 2, engine.POS_FACEUP_ATTACK))
    out = _parse(eng, msg_type, body)
    assert out["source"]["con"] == 0
    assert out["target"]["con"] == 1


def test_msg_be_chain_target_raw(eng: OCGEngine) -> None:
    """Not emitted by shipped engine but parser has defensive raw fallback."""
    body = b"\x01\x02\x03"
    out = _parse(eng, engine.MSG_BE_CHAIN_TARGET, body)
    assert out == {"raw": b"\x01\x02\x03"}


# ---------------------------------------------------------------------------
# Combat family
# ---------------------------------------------------------------------------
def test_msg_attack(eng: OCGEngine) -> None:
    body = (loc_info(0, engine.LOCATION_MZONE, 0, engine.POS_FACEUP_ATTACK)
            + loc_info(1, engine.LOCATION_MZONE, 2, engine.POS_FACEUP_DEFENSE))
    out = _parse(eng, engine.MSG_ATTACK, body)
    assert out["attacker"]["con"] == 0
    assert out["target"]["con"] == 1


def test_msg_battle(eng: OCGEngine) -> None:
    body = (loc_info(0, engine.LOCATION_MZONE, 0, engine.POS_FACEUP_ATTACK)
            + u32(2500) + u32(1200) + u8(0)
            + loc_info(1, engine.LOCATION_MZONE, 2, engine.POS_FACEUP_ATTACK)
            + u32(1800) + u32(1500) + u8(1))
    out = _parse(eng, engine.MSG_BATTLE, body)
    assert out["attacker_attack"] == 2500
    assert out["attacker_defense"] == 1200
    assert out["attacker_destroyed"] is False
    assert out["target_attack"] == 1800
    assert out["target_defense"] == 1500
    assert out["target_destroyed"] is True


def test_msg_attack_disabled(eng: OCGEngine) -> None:
    out = _parse(eng, engine.MSG_ATTACK_DISABLED, b"")
    assert out == {}


@pytest.mark.parametrize("msg_type", [
    engine.MSG_DAMAGE_STEP_START, engine.MSG_DAMAGE_STEP_END,
])
def test_msg_damage_step_markers(eng: OCGEngine, msg_type: int) -> None:
    out = _parse(eng, msg_type, b"")
    assert out == {}


# ---------------------------------------------------------------------------
# Missed effect / counter family
# ---------------------------------------------------------------------------
def test_msg_missed_effect(eng: OCGEngine) -> None:
    body = (loc_info(0, engine.LOCATION_MZONE, 1, engine.POS_FACEUP_ATTACK)
            + u32(0xBEEF))
    out = _parse(eng, engine.MSG_MISSED_EFFECT, body)
    assert out["code"] == 0xBEEF
    assert out["loc"] == engine.LOCATION_MZONE
    assert out["seq"] == 1


def test_msg_add_counter(eng: OCGEngine) -> None:
    body = u16(0x0001) + u8(0) + u8(engine.LOCATION_MZONE) + u8(2) + u16(3)
    out = _parse(eng, engine.MSG_ADD_COUNTER, body)
    assert out == {"counter_type": 0x0001, "con": 0,
                   "loc": engine.LOCATION_MZONE, "seq": 2, "count": 3}


def test_msg_remove_counter(eng: OCGEngine) -> None:
    body = u16(0x0002) + u8(1) + u8(engine.LOCATION_MZONE) + u8(4) + u16(1)
    out = _parse(eng, engine.MSG_REMOVE_COUNTER, body)
    assert out == {"counter_type": 0x0002, "con": 1,
                   "loc": engine.LOCATION_MZONE, "seq": 4, "count": 1}


# ---------------------------------------------------------------------------
# Fallback — unknown msg_type must surface raw bytes, not crash
# ---------------------------------------------------------------------------
def test_unknown_msg_type_raw_fallback(eng: OCGEngine) -> None:
    # 0xFE is reserved in the upstream enum; treat as unknown here.
    unknown = 0xFE
    body = b"\xde\xad\xbe\xef"
    out = _parse(eng, unknown, body)
    assert out == {"raw": body}


# ---------------------------------------------------------------------------
# parse_messages — outer framing test. Each message is u32 length-prefixed.
# ---------------------------------------------------------------------------
def test_parse_messages_framing(eng: OCGEngine) -> None:
    """Two back-to-back messages in one buffer."""
    m1 = u8(engine.MSG_NEW_TURN) + u8(0)
    m2 = u8(engine.MSG_NEW_PHASE) + u16(engine.PHASE_DRAW)
    raw = u32(len(m1)) + m1 + u32(len(m2)) + m2
    msgs = eng.parse_messages(raw)
    assert len(msgs) == 2
    assert msgs[0] == (engine.MSG_NEW_TURN, {"player": 0})
    assert msgs[1] == (engine.MSG_NEW_PHASE, {"phase": engine.PHASE_DRAW})


def test_parse_messages_zero_length_terminator(eng: OCGEngine) -> None:
    """Engine emits a zero-length frame to mark end-of-batch."""
    m1 = u8(engine.MSG_CHAIN_END)
    raw = u32(len(m1)) + m1 + u32(0)
    msgs = eng.parse_messages(raw)
    assert len(msgs) == 1
    assert msgs[0] == (engine.MSG_CHAIN_END, {})
