"""Response-encoding tests — every ``Harness.respond_*`` method has its
wire bytes pinned against ``yugioh/edopro/ocgcore/playerop.cpp``.

The harness calls through to ``OCGEngine.set_response_int`` /
``set_response_bytes``, which in production invoke the ``OCG_DuelSetResponse``
C function.  We swap the engine for a ``FakeEngine`` that records what would
have been sent, so tests verify byte-exact encoding without touching the
shared library.

Each ``advance()`` call after responding normally drains queued messages.
To avoid chaining into the harness's ``advance()`` loop we patch it to a
no-op after the initial pending-decision stub is installed.
"""

from __future__ import annotations

import struct

import pytest

from engine import core as engine
from engine.core import (
    AnnounceAttrib,
    AnnounceCard,
    AnnounceNumber,
    AnnounceRace,
    BattleCmd,
    BattleCmdOption,
    IdleCmd,
    IdleCmdOption,
    SelectCard,
    SelectChain,
    SelectCounter,
    SelectOption,
    SelectPlace,
    SelectPosition,
    SelectSum,
    SelectUnselectCard,
    SortCard,
)
from engine.harness import Harness, HarnessError, InvalidResponseError, PendingDecision, StepResult


# ---------------------------------------------------------------------------
# FakeEngine — records what the harness would have written.
# ---------------------------------------------------------------------------
class FakeEngine:
    def __init__(self) -> None:
        self.responses: list[bytes] = []

    def set_response_int(self, value: int) -> None:
        self.responses.append(struct.pack("<i", value))

    def set_response_bytes(self, data: bytes) -> None:
        self.responses.append(bytes(data))

    @property
    def last(self) -> bytes:
        assert self.responses, "no response recorded"
        return self.responses[-1]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    eng = FakeEngine()
    h = Harness.__new__(Harness)
    h.engine = eng
    h.state = None  # type: ignore[assignment] — unused in the respond_* path
    h._pending = None
    h._retry_count = 0
    h._started = True
    # Stub out advance() so responding doesn't trigger the real loop.
    monkeypatch.setattr(h, "advance", lambda: StepResult())
    return h


def _pend(harness: Harness, msg_type: int, parsed) -> None:
    harness._pending = PendingDecision(
        msg_type=msg_type,
        msg_name=engine.MSG_NAME[msg_type],
        parsed=parsed,
        player=0,
    )


# ---------------------------------------------------------------------------
# Yes/No / effect-yn / option / position / chain / announce-card / announce-number /
# rock-paper-scissors — all single ``int32`` responses.
# ---------------------------------------------------------------------------
def test_respond_effect_yn(harness: Harness) -> None:
    _pend(
        harness,
        engine.MSG_SELECT_EFFECTYN,
        engine.SelectEffectYn(player=0, code=1, con=0, loc=0, seq=0, pos=0, desc=0),
    )
    harness.respond_select_effectyn(True)
    assert harness.engine.last == struct.pack("<i", 1)
    _pend(
        harness,
        engine.MSG_SELECT_EFFECTYN,
        engine.SelectEffectYn(player=0, code=1, con=0, loc=0, seq=0, pos=0, desc=0),
    )
    harness.respond_select_effectyn(False)
    assert harness.engine.last == struct.pack("<i", 0)


def test_respond_yes_no(harness: Harness) -> None:
    _pend(harness, engine.MSG_SELECT_YESNO, engine.SelectYesNo(player=0, desc=0))
    harness.respond_select_yesno(True)
    assert harness.engine.last == struct.pack("<i", 1)


def test_respond_option(harness: Harness) -> None:
    _pend(harness, engine.MSG_SELECT_OPTION, SelectOption(player=0, options=[10, 20, 30]))
    harness.respond_select_option(2)
    assert harness.engine.last == struct.pack("<i", 2)


def test_respond_option_out_of_range(harness: Harness) -> None:
    _pend(harness, engine.MSG_SELECT_OPTION, SelectOption(player=0, options=[10]))
    with pytest.raises(InvalidResponseError):
        harness.respond_select_option(3)


def test_respond_position(harness: Harness) -> None:
    _pend(
        harness,
        engine.MSG_SELECT_POSITION,
        SelectPosition(
            player=0, code=0, positions=engine.POS_FACEUP_ATTACK | engine.POS_FACEDOWN_DEFENSE
        ),
    )
    harness.respond_select_position(engine.POS_FACEUP_ATTACK)
    assert harness.engine.last == struct.pack("<i", engine.POS_FACEUP_ATTACK)


def test_respond_position_rejects_unavailable(harness: Harness) -> None:
    _pend(
        harness,
        engine.MSG_SELECT_POSITION,
        SelectPosition(player=0, code=0, positions=engine.POS_FACEUP_ATTACK),
    )
    with pytest.raises(InvalidResponseError):
        harness.respond_select_position(engine.POS_FACEDOWN_DEFENSE)


def test_respond_chain_decline(harness: Harness) -> None:
    _pend(
        harness, engine.MSG_SELECT_CHAIN, SelectChain(player=0, forced=False, cards=[{"index": 0}])
    )
    harness.respond_select_chain(None)
    assert harness.engine.last == struct.pack("<i", -1)


def test_respond_chain_forced_cannot_decline(harness: Harness) -> None:
    _pend(
        harness, engine.MSG_SELECT_CHAIN, SelectChain(player=0, forced=True, cards=[{"index": 0}])
    )
    with pytest.raises(InvalidResponseError):
        harness.respond_select_chain(None)


def test_respond_chain_select(harness: Harness) -> None:
    _pend(
        harness,
        engine.MSG_SELECT_CHAIN,
        SelectChain(player=0, forced=False, cards=[{"index": 0}, {"index": 1}]),
    )
    harness.respond_select_chain(1)
    assert harness.engine.last == struct.pack("<i", 1)


def test_respond_announce_card(harness: Harness) -> None:
    _pend(harness, engine.MSG_ANNOUNCE_CARD, AnnounceCard(player=0, opcodes=[]))
    harness.respond_announce_card(0x12345678)
    assert harness.engine.last == struct.pack("<i", 0x12345678)


def test_respond_announce_number(harness: Harness) -> None:
    _pend(harness, engine.MSG_ANNOUNCE_NUMBER, AnnounceNumber(player=0, numbers=[1, 2, 3]))
    harness.respond_announce_number(2)
    assert harness.engine.last == struct.pack("<i", 2)


def test_respond_rock_paper_scissors(harness: Harness) -> None:
    for hand in (1, 2, 3):
        _pend(harness, engine.MSG_ROCK_PAPER_SCISSORS, {"player": 0})
        harness.respond_rock_paper_scissors(hand)
        assert harness.engine.last == struct.pack("<i", hand)


def test_respond_rock_paper_scissors_invalid(harness: Harness) -> None:
    _pend(harness, engine.MSG_ROCK_PAPER_SCISSORS, {"player": 0})
    with pytest.raises(InvalidResponseError):
        harness.respond_rock_paper_scissors(0)
    with pytest.raises(InvalidResponseError):
        harness.respond_rock_paper_scissors(4)


# ---------------------------------------------------------------------------
# IdleCmd / BattleCmd — response packs ``(s << 16) | t`` as int32.
# ---------------------------------------------------------------------------
def test_respond_idle_cmd_summon(harness: Harness) -> None:
    opt = IdleCmdOption(category=0, index=2, code=0, con=0, loc=0, seq=0)
    idle = IdleCmd(
        player=0, options=[opt], can_battle_phase=True, can_end_phase=True, can_shuffle=True
    )
    _pend(harness, engine.MSG_SELECT_IDLECMD, idle)
    harness.respond_select_idlecmd("summon", 0)
    expected = struct.pack("<i", (2 << 16) | 0)  # s=index=2, t=summon=0
    assert harness.engine.last == expected


def test_respond_idle_cmd_to_battle_phase(harness: Harness) -> None:
    idle = IdleCmd(
        player=0, options=[], can_battle_phase=True, can_end_phase=True, can_shuffle=True
    )
    _pend(harness, engine.MSG_SELECT_IDLECMD, idle)
    harness.respond_select_idlecmd("to_battle_phase")
    assert harness.engine.last == struct.pack("<i", 6)  # t=6, s=0


def test_respond_idle_cmd_to_end_phase_disabled(harness: Harness) -> None:
    idle = IdleCmd(
        player=0, options=[], can_battle_phase=True, can_end_phase=False, can_shuffle=False
    )
    _pend(harness, engine.MSG_SELECT_IDLECMD, idle)
    with pytest.raises(InvalidResponseError):
        harness.respond_select_idlecmd("to_end_phase")


def test_respond_battle_cmd_attack(harness: Harness) -> None:
    opt = BattleCmdOption(category=1, index=3, code=0, con=0, loc=0, seq=0)
    bc = BattleCmd(player=0, options=[opt], can_main2=True, can_end_phase=True)
    _pend(harness, engine.MSG_SELECT_BATTLECMD, bc)
    harness.respond_select_battlecmd("attack", 0)
    assert harness.engine.last == struct.pack("<i", (3 << 16) | 1)


def test_respond_battle_cmd_to_m2(harness: Harness) -> None:
    bc = BattleCmd(player=0, options=[], can_main2=True, can_end_phase=True)
    _pend(harness, engine.MSG_SELECT_BATTLECMD, bc)
    harness.respond_select_battlecmd("to_main_phase_2")
    assert harness.engine.last == struct.pack("<i", 2)


# ---------------------------------------------------------------------------
# SELECT_CARD family — cancel path, multi-index path, range errors.
# Wire: int32 type (0 or -1), uint32 count, uint32[count] indices.
# ---------------------------------------------------------------------------
def _mk_select_card(n: int, min_: int, max_: int, cancelable: bool = False) -> SelectCard:
    cards = [{"code": i, "con": 0, "loc": 0x02, "seq": i, "pos": 0, "index": i} for i in range(n)]
    return SelectCard(
        player=0, cancelable=cancelable, min_=min_, max_=max_, cards=cards, is_tribute=False
    )


def test_respond_select_card_one(harness: Harness) -> None:
    sc = _mk_select_card(3, 1, 1)
    _pend(harness, engine.MSG_SELECT_CARD, sc)
    harness.respond_select_card([1])
    assert harness.engine.last == struct.pack("<iII", 0, 1, 1)


def test_respond_select_card_multi(harness: Harness) -> None:
    sc = _mk_select_card(5, 2, 3)
    _pend(harness, engine.MSG_SELECT_CARD, sc)
    harness.respond_select_card([0, 2, 4])
    assert harness.engine.last == struct.pack("<iIIII", 0, 3, 0, 2, 4)


def test_respond_select_card_cancel(harness: Harness) -> None:
    sc = _mk_select_card(2, 1, 1, cancelable=True)
    _pend(harness, engine.MSG_SELECT_CARD, sc)
    harness.respond_select_card([], cancel=True)
    assert harness.engine.last == struct.pack("<i", -1)


def test_respond_select_card_cancel_rejected(harness: Harness) -> None:
    sc = _mk_select_card(2, 1, 1, cancelable=False)
    _pend(harness, engine.MSG_SELECT_CARD, sc)
    with pytest.raises(InvalidResponseError):
        harness.respond_select_card([], cancel=True)


def test_respond_select_card_duplicate(harness: Harness) -> None:
    sc = _mk_select_card(3, 2, 3)
    _pend(harness, engine.MSG_SELECT_CARD, sc)
    with pytest.raises(InvalidResponseError):
        harness.respond_select_card([0, 0])


def test_respond_select_card_out_of_range(harness: Harness) -> None:
    sc = _mk_select_card(2, 1, 1)
    _pend(harness, engine.MSG_SELECT_CARD, sc)
    with pytest.raises(InvalidResponseError):
        harness.respond_select_card([3])


# ---------------------------------------------------------------------------
# SELECT_UNSELECT_CARD — wire: int32 count, int32 index (or single -1).
# ---------------------------------------------------------------------------
def test_respond_select_unselect_pick(harness: Harness) -> None:
    su = SelectUnselectCard(
        player=0,
        finishable=False,
        cancelable=True,
        min_=1,
        max_=1,
        selectable_cards=[{"index": 0}, {"index": 1}],
        selected_cards=[],
    )
    _pend(harness, engine.MSG_SELECT_UNSELECT_CARD, su)
    harness.respond_select_unselect_card(1)
    assert harness.engine.last == struct.pack("<ii", 1, 1)


def test_respond_select_unselect_cancel(harness: Harness) -> None:
    su = SelectUnselectCard(
        player=0,
        finishable=True,
        cancelable=False,
        min_=1,
        max_=1,
        selectable_cards=[{"index": 0}],
        selected_cards=[],
    )
    _pend(harness, engine.MSG_SELECT_UNSELECT_CARD, su)
    harness.respond_select_unselect_card(None)
    assert harness.engine.last == struct.pack("<i", -1)


def test_respond_select_unselect_cannot_cancel(harness: Harness) -> None:
    su = SelectUnselectCard(
        player=0,
        finishable=False,
        cancelable=False,
        min_=1,
        max_=1,
        selectable_cards=[{"index": 0}],
        selected_cards=[],
    )
    _pend(harness, engine.MSG_SELECT_UNSELECT_CARD, su)
    with pytest.raises(InvalidResponseError):
        harness.respond_select_unselect_card(None)


# ---------------------------------------------------------------------------
# SELECT_PLACE / SELECT_DISFIELD — playerop.cpp:504
# Wire: (player u8, loc u8, seq u8) × N, concatenated.
# ---------------------------------------------------------------------------
def test_respond_select_place(harness: Harness) -> None:
    sp = SelectPlace(player=0, min_=2, field_mask=0)
    _pend(harness, engine.MSG_SELECT_PLACE, sp)
    harness.respond_select_place([(0, 0x04, 1), (1, 0x08, 3)])
    assert harness.engine.last == bytes([0, 0x04, 1, 1, 0x08, 3])


def test_respond_select_place_wrong_count(harness: Harness) -> None:
    sp = SelectPlace(player=0, min_=2, field_mask=0)
    _pend(harness, engine.MSG_SELECT_PLACE, sp)
    with pytest.raises(InvalidResponseError):
        harness.respond_select_place([(0, 0x04, 1)])


# ---------------------------------------------------------------------------
# SELECT_COUNTER — playerop.cpp:704
# Wire: int16[N], one per card in the order the engine emitted them.
# ---------------------------------------------------------------------------
def test_respond_select_counter(harness: Harness) -> None:
    sc = SelectCounter(
        player=0,
        counter_type=0x10,
        count=3,
        cards=[{"counter": 2, "index": 0}, {"counter": 2, "index": 1}],
    )
    _pend(harness, engine.MSG_SELECT_COUNTER, sc)
    harness.respond_select_counter([1, 2])
    assert harness.engine.last == struct.pack("<hh", 1, 2)


def test_respond_select_counter_wrong_sum(harness: Harness) -> None:
    sc = SelectCounter(
        player=0,
        counter_type=0x10,
        count=3,
        cards=[{"counter": 2, "index": 0}, {"counter": 2, "index": 1}],
    )
    _pend(harness, engine.MSG_SELECT_COUNTER, sc)
    with pytest.raises(InvalidResponseError):
        harness.respond_select_counter([1, 1])


def test_respond_select_counter_exceeds_card(harness: Harness) -> None:
    sc = SelectCounter(
        player=0,
        counter_type=0x10,
        count=3,
        cards=[{"counter": 2, "index": 0}, {"counter": 5, "index": 1}],
    )
    _pend(harness, engine.MSG_SELECT_COUNTER, sc)
    with pytest.raises(InvalidResponseError):
        harness.respond_select_counter([3, 0])


# ---------------------------------------------------------------------------
# SELECT_SUM — playerop.cpp:781 — same type=0 wire format as SelectCard.
# ---------------------------------------------------------------------------
def test_respond_select_sum(harness: Harness) -> None:
    ss = SelectSum(
        player=0,
        mode=0,
        sumval=8,
        min_=1,
        max_=2,
        mandatory_cards=[],
        optional_cards=[{"index": i, "code": i, "op_param": 4} for i in range(3)],
    )
    _pend(harness, engine.MSG_SELECT_SUM, ss)
    harness.respond_select_sum([0, 1])
    assert harness.engine.last == struct.pack("<iIII", 0, 2, 0, 1)


# ---------------------------------------------------------------------------
# SORT_CARD / SORT_CHAIN — playerop.cpp:871
# Wire: int8[N] — the permutation. Skip = single int8(-1).
# ---------------------------------------------------------------------------
def test_respond_sort_card_ordering(harness: Harness) -> None:
    sc = SortCard(player=0, cards=[{"index": i} for i in range(3)])
    _pend(harness, engine.MSG_SORT_CARD, sc)
    harness.respond_sort_card([2, 0, 1])
    assert harness.engine.last == bytes([2, 0, 1])


def test_respond_sort_card_skip(harness: Harness) -> None:
    sc = SortCard(player=0, cards=[{"index": i} for i in range(3)])
    _pend(harness, engine.MSG_SORT_CARD, sc)
    harness.respond_sort_card(None)
    assert harness.engine.last == struct.pack("<b", -1)


def test_respond_sort_card_non_permutation(harness: Harness) -> None:
    sc = SortCard(player=0, cards=[{"index": i} for i in range(3)])
    _pend(harness, engine.MSG_SORT_CARD, sc)
    with pytest.raises(InvalidResponseError):
        harness.respond_sort_card([0, 0, 1])
    with pytest.raises(InvalidResponseError):
        harness.respond_sort_card([0, 1])


# ---------------------------------------------------------------------------
# ANNOUNCE_RACE — playerop.cpp:914 — response is uint64 mask.
# ANNOUNCE_ATTRIBUTE — playerop.cpp:949 — response is uint32 mask.
# ---------------------------------------------------------------------------
def test_respond_announce_race(harness: Harness) -> None:
    ar = AnnounceRace(
        player=0, count=2, available=engine.RACE_DRAGON | engine.RACE_BEAST | engine.RACE_WARRIOR
    )
    _pend(harness, engine.MSG_ANNOUNCE_RACE, ar)
    picked = engine.RACE_DRAGON | engine.RACE_WARRIOR
    harness.respond_announce_race(picked)
    assert harness.engine.last == struct.pack("<Q", picked)


def test_respond_announce_race_wrong_count(harness: Harness) -> None:
    ar = AnnounceRace(player=0, count=2, available=engine.RACE_DRAGON)
    _pend(harness, engine.MSG_ANNOUNCE_RACE, ar)
    with pytest.raises(InvalidResponseError):
        harness.respond_announce_race(engine.RACE_DRAGON)


def test_respond_announce_race_outside_available(harness: Harness) -> None:
    ar = AnnounceRace(player=0, count=1, available=engine.RACE_DRAGON)
    _pend(harness, engine.MSG_ANNOUNCE_RACE, ar)
    with pytest.raises(InvalidResponseError):
        harness.respond_announce_race(engine.RACE_BEAST)


def test_respond_announce_attribute(harness: Harness) -> None:
    aa = AnnounceAttrib(player=0, count=1, available=engine.ATTRIBUTE_LIGHT | engine.ATTRIBUTE_DARK)
    _pend(harness, engine.MSG_ANNOUNCE_ATTRIB, aa)
    harness.respond_announce_attribute(engine.ATTRIBUTE_LIGHT)
    assert harness.engine.last == struct.pack("<I", engine.ATTRIBUTE_LIGHT)


def test_respond_announce_attribute_rejects_bit_outside(harness: Harness) -> None:
    aa = AnnounceAttrib(player=0, count=1, available=engine.ATTRIBUTE_LIGHT)
    _pend(harness, engine.MSG_ANNOUNCE_ATTRIB, aa)
    with pytest.raises(InvalidResponseError):
        harness.respond_announce_attribute(engine.ATTRIBUTE_DARK)


# ---------------------------------------------------------------------------
# _require rejects mismatched prompts.
# ---------------------------------------------------------------------------
def test_wrong_responder_rejected(harness: Harness) -> None:
    _pend(harness, engine.MSG_SELECT_YESNO, engine.SelectYesNo(player=0, desc=0))
    with pytest.raises(HarnessError):
        harness.respond_select_effectyn(True)
