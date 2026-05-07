"""SELECT / ANNOUNCE wire-format parser tests.

Every ``MSG_SELECT_*``, ``MSG_SORT_*``, ``MSG_ANNOUNCE_*`` and
``MSG_ROCK_PAPER_SCISSORS`` parser is exercised here. Wire bytes are
hand-crafted per ``yugioh/edopro/ocgcore/playerop.cpp`` so any future drift
— field add/remove, endianness flip, struct-packing change — fails loudly.

Each test builds a ``bytes`` stream matching the upstream C++ emitter, then
feeds it to the corresponding parser via ``OCGEngine._parse_single_message``.
We bypass ``OCGEngine.__init__`` because the parsers are pure — they only
read from a ``MessageReader`` and never touch ``self.lib``.
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
    """Uninitialised OCGEngine — its parser methods are pure."""
    return OCGEngine.__new__(OCGEngine)


def loc_info(con: int, loc: int, seq: int, pos: int) -> bytes:
    """Serialise a C ``loc_info`` struct: u8, u8, u32, u32."""
    return struct.pack("<BBII", con, loc, seq, pos)


def u8(v: int) -> bytes:  return struct.pack("<B", v)
def u16(v: int) -> bytes: return struct.pack("<H", v)
def u32(v: int) -> bytes: return struct.pack("<I", v)
def u64(v: int) -> bytes: return struct.pack("<Q", v)


def _parse(eng: OCGEngine, msg_type: int, body: bytes):
    return eng._parse_single_message(msg_type, MessageReader(body))


# ---------------------------------------------------------------------------
# SELECT_IDLECMD — playerop.cpp §IdleCmd (~line 18)
# ---------------------------------------------------------------------------
def test_select_idle_cmd_all_categories(eng: OCGEngine) -> None:
    """Five categories (summon/spsummon/repos/mset/ssset) each with one
    card, then activate with a desc+opcode, then canBP/canEP/canShuffle
    flags. Categories 0/1/3/4 use u32 seq; category 2 (repos) uses u8 seq.
    """
    player = 0
    cards = [
        # cat 0: summon — seq u32
        u32(0x11111) + u8(0) + u8(0x02) + u32(0x00000005),
        # cat 1: spsummon — seq u32
        u32(0x22222) + u8(0) + u8(0x02) + u32(0x00000006),
        # cat 2: repos — seq u8
        u32(0x33333) + u8(1) + u8(0x04) + u8(0x07),
        # cat 3: mset — seq u32
        u32(0x44444) + u8(0) + u8(0x02) + u32(0x00000008),
        # cat 4: ssset — seq u32
        u32(0x55555) + u8(0) + u8(0x08) + u32(0x00000009),
    ]
    # cat 5: activate — code, con, loc, seq u32, desc u64, op-type u8
    act = (u32(0x66666) + u8(0) + u8(0x08)
           + u32(0x0000000A) + u64(0xDEADBEEF12345678) + u8(0))
    body = u8(player)
    for card in cards:
        body += u32(1) + card  # each category prefixed by count=1
    body += u32(1) + act       # activate prefix (u32 count) + one entry
    body += u8(1) + u8(0) + u8(1)  # can_bp, can_ep, can_shuffle

    idle = _parse(eng, engine.MSG_SELECT_IDLECMD, body)
    assert idle.player == 0
    assert idle.can_battle_phase is True
    assert idle.can_end_phase is False
    assert idle.can_shuffle is True
    cats = {o.category: o for o in idle.options}
    assert cats.keys() == {0, 1, 2, 3, 4, 5}
    assert cats[0].code == 0x11111 and cats[0].seq == 0x05
    assert cats[2].seq == 0x07          # repos seq is u8
    assert cats[5].code == 0x66666 and cats[5].desc == 0xDEADBEEF12345678


# ---------------------------------------------------------------------------
# SELECT_BATTLECMD — playerop.cpp:2118 (MSG_ATTACK write path) and
# the Processors::SelectBattleCmd emitter. Wire format:
#   u8 player, u32 activate_count, [u32 code, u8 con, u8 loc, u32 seq,
#   u64 desc, u8 op_type]*, u32 attack_count, [u32 code, u8 con, u8 loc,
#   u8 seq, u8 direct_attackable]*, u8 can_m2, u8 can_ep.
# ---------------------------------------------------------------------------
def test_select_battle_cmd(eng: OCGEngine) -> None:
    body = (
        u8(0)
        + u32(1)                                           # activate count
        + u32(0xABCD) + u8(0) + u8(0x04) + u32(3)
        + u64(0x1122334455667788) + u8(0)
        + u32(2)                                           # attack count
        + u32(0x1000) + u8(0) + u8(0x04) + u8(0) + u8(1)
        + u32(0x2000) + u8(0) + u8(0x04) + u8(1) + u8(0)
        + u8(1)                                            # can_m2
        + u8(0)                                            # can_ep
    )
    bc = _parse(eng, engine.MSG_SELECT_BATTLECMD, body)
    assert bc.player == 0
    assert bc.can_main2 is True and bc.can_end_phase is False
    act = [o for o in bc.options if o.category == 0]
    atk = [o for o in bc.options if o.category == 1]
    assert len(act) == 1 and act[0].code == 0xABCD
    assert len(atk) == 2
    assert atk[0].direct_attackable == 1
    assert atk[1].direct_attackable == 0


# ---------------------------------------------------------------------------
# SELECT_CARD — playerop.cpp:279
# Wire: u8 player, u8 cancelable, u32 min, u32 max, u32 count,
#       [u32 code, loc_info]*.
# ---------------------------------------------------------------------------
def test_select_card_no_tribute(eng: OCGEngine) -> None:
    body = (u8(1) + u8(1) + u32(1) + u32(3) + u32(2)
            + u32(0x1234) + loc_info(0, 0x02, 1, 0x01)
            + u32(0x5678) + loc_info(1, 0x04, 0, 0x01))
    sc = _parse(eng, engine.MSG_SELECT_CARD, body)
    assert sc.player == 1
    assert sc.cancelable is True
    assert sc.min_ == 1 and sc.max_ == 3
    assert len(sc.cards) == 2
    assert sc.cards[0] == {"code": 0x1234, "con": 0, "loc": 0x02,
                           "seq": 1, "pos": 0x01, "index": 0}
    assert sc.is_tribute is False


# ---------------------------------------------------------------------------
# SELECT_TRIBUTE — playerop.cpp:639
# Difference: per-card tail replaces u32 pos with u8 release-param, which
# the parser discards and sets pos=0.
# ---------------------------------------------------------------------------
def test_select_tribute(eng: OCGEngine) -> None:
    body = (u8(0) + u8(0) + u32(1) + u32(1) + u32(1)
            + u32(0xAA) + u8(0) + u8(0x04) + u32(2) + u8(3))
    sc = _parse(eng, engine.MSG_SELECT_TRIBUTE, body)
    assert sc.is_tribute is True
    assert sc.cards[0]["pos"] == 0
    assert sc.cards[0]["code"] == 0xAA
    assert sc.cards[0]["seq"] == 2


# ---------------------------------------------------------------------------
# SELECT_CHAIN — playerop.cpp:454
# Wire: u8 player, u8 specount, u8 forced, u32 hint1, u32 hint2, u32 count,
#       [u32 code, loc_info, u64 desc, u8 client_mode]*.
# ---------------------------------------------------------------------------
def test_select_chain(eng: OCGEngine) -> None:
    body = (u8(0) + u8(1) + u8(1)               # player, specount, forced
            + u32(0xAAAA) + u32(0xBBBB)          # hint1, hint2
            + u32(1)                             # count
            + u32(0x42) + loc_info(0, 0x08, 3, 0x01)
            + u64(0x1234567890ABCDEF) + u8(0))
    sc = _parse(eng, engine.MSG_SELECT_CHAIN, body)
    assert sc.player == 0 and sc.forced is True
    assert sc.cards[0]["desc"] == 0x1234567890ABCDEF
    assert sc.cards[0]["code"] == 0x42
    assert sc.cards[0]["con"] == 0
    assert sc.cards[0]["loc"] == 0x08


# ---------------------------------------------------------------------------
# SELECT_EFFECTYN — playerop.cpp:160
# Wire: u8 player, u32 code, loc_info, u64 desc.
# ---------------------------------------------------------------------------
def test_select_effect_yn(eng: OCGEngine) -> None:
    body = (u8(1) + u32(0xDEAD)
            + loc_info(1, 0x04, 2, engine.POS_FACEUP_ATTACK)
            + u64(0xCAFEBABE))
    yn = _parse(eng, engine.MSG_SELECT_EFFECTYN, body)
    assert yn.player == 1 and yn.code == 0xDEAD
    assert yn.desc == 0xCAFEBABE
    assert yn.con == 1 and yn.loc == 0x04 and yn.seq == 2
    assert yn.pos == engine.POS_FACEUP_ATTACK


# ---------------------------------------------------------------------------
# SELECT_YESNO — playerop.cpp:184. Wire: u8 player, u64 desc.
# ---------------------------------------------------------------------------
def test_select_yesno(eng: OCGEngine) -> None:
    yn = _parse(eng, engine.MSG_SELECT_YESNO, u8(0) + u64(0x1000))
    assert yn.player == 0 and yn.desc == 0x1000


# ---------------------------------------------------------------------------
# SELECT_OPTION — playerop.cpp:205. Wire: u8 player, u8 count, u64[count].
# ---------------------------------------------------------------------------
def test_select_option(eng: OCGEngine) -> None:
    body = u8(0) + u8(2) + u64(0x1) + u64(0x2)
    opt = _parse(eng, engine.MSG_SELECT_OPTION, body)
    assert opt.player == 0 and opt.options == [0x1, 0x2]


# ---------------------------------------------------------------------------
# SELECT_POSITION — playerop.cpp:599. Wire: u8 player, u32 code, u8 positions.
# ---------------------------------------------------------------------------
def test_select_position(eng: OCGEngine) -> None:
    body = u8(1) + u32(0x5566) + u8(engine.POS_FACEUP_DEFENSE)
    sp = _parse(eng, engine.MSG_SELECT_POSITION, body)
    assert sp.player == 1 and sp.code == 0x5566
    assert sp.positions == engine.POS_FACEUP_DEFENSE


# ---------------------------------------------------------------------------
# SELECT_PLACE / SELECT_DISFIELD — playerop.cpp:504-596
# Wire: u8 player, u8 min, u32 field_mask (per-controller zones packed).
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("msg", [engine.MSG_SELECT_PLACE,
                                  engine.MSG_SELECT_DISFIELD])
def test_select_place(msg: int, eng: OCGEngine) -> None:
    body = u8(0) + u8(1) + u32(0x0000001F)
    sp = _parse(eng, msg, body)
    assert sp.player == 0
    assert sp.min_ == 1
    assert sp.field_mask == 0x0000001F


# ---------------------------------------------------------------------------
# SELECT_SUM — playerop.cpp:781
# Wire: u8 player, u8 mode, u32 sumval, u32 min, u32 max,
#       u32 mandatory_count, [u32 code, loc_info, u32 op_param]*,
#       u32 optional_count, ...same tail...
# ---------------------------------------------------------------------------
def test_select_sum(eng: OCGEngine) -> None:
    mandatory = (u32(0x1) + loc_info(0, 0x04, 0, 0x01) + u32(4))
    optional = (u32(0x2) + loc_info(0, 0x04, 1, 0x01) + u32(3)
                + u32(0x3) + loc_info(0, 0x04, 2, 0x01) + u32(5))
    body = (u8(0) + u8(0) + u32(8) + u32(1) + u32(5)
            + u32(1) + mandatory + u32(2) + optional)
    ss = _parse(eng, engine.MSG_SELECT_SUM, body)
    assert ss.player == 0 and ss.mode == 0 and ss.sumval == 8
    assert ss.min_ == 1 and ss.max_ == 5
    assert [c["code"] for c in ss.mandatory_cards] == [0x1]
    assert [c["code"] for c in ss.optional_cards] == [0x2, 0x3]
    assert ss.optional_cards[1]["op_param"] == 5


# ---------------------------------------------------------------------------
# SELECT_UNSELECT_CARD — playerop.cpp:388
# Wire: u8 player, u8 finishable, u8 cancelable, u32 min, u32 max,
#       u32 sel_count, [u32 code, loc_info]*,
#       u32 already_count, [u32 code, loc_info]*.
# ---------------------------------------------------------------------------
def test_select_unselect_card(eng: OCGEngine) -> None:
    body = (u8(0) + u8(0) + u8(1) + u32(1) + u32(1)
            + u32(1) + u32(0x1) + loc_info(0, 0x04, 0, 0x01)
            + u32(1) + u32(0x2) + loc_info(0, 0x04, 1, 0x01))
    su = _parse(eng, engine.MSG_SELECT_UNSELECT_CARD, body)
    assert su.player == 0
    assert su.finishable is False
    assert su.cancelable is True
    assert len(su.selectable_cards) == 1 and su.selectable_cards[0]["code"] == 0x1
    assert len(su.selected_cards) == 1 and su.selected_cards[0]["code"] == 0x2


# ---------------------------------------------------------------------------
# SELECT_COUNTER — playerop.cpp:704
# Wire: u8 player, u16 counter_type, u16 count, u32 num_cards,
#       [u32 code, u8 con, u8 loc, u8 seq, u16 counter]*.
# ---------------------------------------------------------------------------
def test_select_counter(eng: OCGEngine) -> None:
    body = (u8(0) + u16(0x10) + u16(3) + u32(2)
            + u32(0x1) + u8(0) + u8(0x04) + u8(0) + u16(2)
            + u32(0x2) + u8(0) + u8(0x04) + u8(1) + u16(1))
    sc = _parse(eng, engine.MSG_SELECT_COUNTER, body)
    assert sc.player == 0
    assert sc.counter_type == 0x10 and sc.count == 3
    assert sc.cards[0]["counter"] == 2
    assert sc.cards[1]["counter"] == 1


# ---------------------------------------------------------------------------
# SORT_CARD / SORT_CHAIN — playerop.cpp:871
# NB: SORT_CARD uses ``u32 loc`` per card (not u8!); playerop.cpp:899.
# Wire: u8 player, u32 count, [u32 code, u8 con, u32 loc, u32 seq]*.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("msg", [engine.MSG_SORT_CARD, engine.MSG_SORT_CHAIN])
def test_sort_card(msg: int, eng: OCGEngine) -> None:
    body = (u8(0) + u32(2)
            + u32(0xAA) + u8(0) + u32(0x00000004) + u32(0)
            + u32(0xBB) + u8(0) + u32(0x00000004) + u32(1))
    s = _parse(eng, msg, body)
    assert s.player == 0
    assert [c["code"] for c in s.cards] == [0xAA, 0xBB]
    assert all(c["loc"] == 4 for c in s.cards)


# ---------------------------------------------------------------------------
# ROCK_PAPER_SCISSORS — playerop.cpp:1121. Wire: u8 player.
# (Pre-fix, the parser was hard-coded with the 130/131/132 swap; this test
# is the authoritative regression guard.)
# ---------------------------------------------------------------------------
def test_rock_paper_scissors(eng: OCGEngine) -> None:
    out = _parse(eng, engine.MSG_ROCK_PAPER_SCISSORS, u8(1))
    assert out == {"player": 1}


# ---------------------------------------------------------------------------
# ANNOUNCE_RACE — playerop.cpp:914.  Wire: u8 player, u8 count, u64 mask.
# ---------------------------------------------------------------------------
def test_announce_race(eng: OCGEngine) -> None:
    body = u8(0) + u8(2) + u64(engine.RACE_DRAGON | engine.RACE_BEAST)
    ar = _parse(eng, engine.MSG_ANNOUNCE_RACE, body)
    assert ar.player == 0 and ar.count == 2
    assert ar.available == engine.RACE_DRAGON | engine.RACE_BEAST


# ---------------------------------------------------------------------------
# ANNOUNCE_ATTRIB — playerop.cpp:949.  Wire: u8 player, u8 count, u32 mask.
# ---------------------------------------------------------------------------
def test_announce_attrib(eng: OCGEngine) -> None:
    body = u8(1) + u8(1) + u32(engine.ATTRIBUTE_LIGHT)
    aa = _parse(eng, engine.MSG_ANNOUNCE_ATTRIB, body)
    assert aa.player == 1 and aa.count == 1
    assert aa.available == engine.ATTRIBUTE_LIGHT


# ---------------------------------------------------------------------------
# ANNOUNCE_CARD — playerop.cpp:1075.
# Wire: u8 player, u8 count, u64 opcodes[count].
# ---------------------------------------------------------------------------
def test_announce_card(eng: OCGEngine) -> None:
    body = (u8(0) + u8(3)
            + u64(engine.OPCODE_ISCODE)
            + u64(0x12345678)
            + u64(engine.OPCODE_OR))
    ac = _parse(eng, engine.MSG_ANNOUNCE_CARD, body)
    assert ac.player == 0
    assert ac.opcodes == [engine.OPCODE_ISCODE, 0x12345678, engine.OPCODE_OR]


# ---------------------------------------------------------------------------
# ANNOUNCE_NUMBER — playerop.cpp:1099.
# Wire: u8 player, u8 count, u64 numbers[count].
# ---------------------------------------------------------------------------
def test_announce_number(eng: OCGEngine) -> None:
    body = u8(0) + u8(3) + u64(1) + u64(2) + u64(3)
    an = _parse(eng, engine.MSG_ANNOUNCE_NUMBER, body)
    assert an.numbers == [1, 2, 3]


# ---------------------------------------------------------------------------
# Round-trip: every SELECT/ANNOUNCE msg type must parse *something*, never
# return ``{"raw": ...}``. Guards against accidental dispatcher regression.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("msg,sample", [
    (engine.MSG_SELECT_YESNO,  u8(0) + u64(0)),
    (engine.MSG_SELECT_OPTION, u8(0) + u8(0)),
    (engine.MSG_SELECT_PLACE,  u8(0) + u8(1) + u32(0)),
    (engine.MSG_SELECT_DISFIELD, u8(0) + u8(1) + u32(0)),
    (engine.MSG_ROCK_PAPER_SCISSORS, u8(0)),
])
def test_no_raw_fallback_for_selects(msg: int, sample: bytes, eng: OCGEngine) -> None:
    out = _parse(eng, msg, sample)
    # The default branch returns ``{"raw": ...}``; structured parsers
    # return a dataclass or a structured dict without that key.
    if isinstance(out, dict):
        assert "raw" not in out, f"{engine.MSG_NAME.get(msg, msg)} fell through to raw"
