"""Smoke test: bulk-mode auto-opponent policy.

Verifies that the passive-opponent logic in ``engine.replay`` can drive
any pending decision for the non-scoring player without crashing.  We
feed the unit tests a synthetic PendingDecision for each ``MSG_SELECT_*``
message type and check that ``_pick_passive_opponent_response`` returns
a structurally-valid (tool_name, args) pair.
"""

from __future__ import annotations

import pytest

from engine.core import (
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
    IdleCmd,
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
)
from engine.harness import PendingDecision
from engine.replay import _pick_passive_opponent_response
from engine.tools import TOOL_TO_HARNESS_METHOD


def _pd(msg_type: int, msg_name: str, parsed) -> PendingDecision:
    return PendingDecision(
        msg_type=msg_type,
        msg_name=msg_name,
        parsed=parsed,
        player=1,
    )


CASES = [
    # idlecmd — engine asks opponent what they want to do this main phase
    (
        MSG_SELECT_IDLECMD,
        "MSG_SELECT_IDLECMD",
        IdleCmd(
            player=1, options=[], can_battle_phase=False, can_end_phase=True, can_shuffle=False
        ),
    ),
    # battlecmd
    (
        MSG_SELECT_BATTLECMD,
        "MSG_SELECT_BATTLECMD",
        BattleCmd(player=1, options=[], can_main2=True, can_end_phase=True),
    ),
    # yesno / effectyn
    (
        MSG_SELECT_EFFECTYN,
        "MSG_SELECT_EFFECTYN",
        SelectEffectYn(player=1, code=0, con=0, loc=0, seq=0, pos=0, desc=0),
    ),
    (MSG_SELECT_YESNO, "MSG_SELECT_YESNO", SelectYesNo(player=1, desc=0)),
    # option
    (MSG_SELECT_OPTION, "MSG_SELECT_OPTION", SelectOption(player=1, options=[1, 2, 3])),
    # chain — not forced, should decline
    (MSG_SELECT_CHAIN, "MSG_SELECT_CHAIN", SelectChain(player=1, forced=False, cards=[])),
    (
        MSG_SELECT_CHAIN,
        "MSG_SELECT_CHAIN (forced)",
        SelectChain(player=1, forced=True, cards=[{"code": 123, "desc": 0}]),
    ),
    # card selection
    (
        MSG_SELECT_CARD,
        "MSG_SELECT_CARD",
        SelectCard(
            player=1, cancelable=False, min_=1, max_=1, cards=[{"code": 123}, {"code": 456}]
        ),
    ),
    (
        MSG_SELECT_CARD,
        "MSG_SELECT_CARD (cancelable zero-min)",
        SelectCard(player=1, cancelable=True, min_=0, max_=3, cards=[{"code": 123}]),
    ),
    # tribute
    (
        MSG_SELECT_TRIBUTE,
        "MSG_SELECT_TRIBUTE",
        SelectCard(
            player=1,
            cancelable=False,
            min_=2,
            max_=2,
            cards=[{"code": 1}, {"code": 2}],
            is_tribute=True,
        ),
    ),
    # unselect_card (finishable)
    (
        MSG_SELECT_UNSELECT_CARD,
        "MSG_SELECT_UNSELECT_CARD",
        SelectUnselectCard(
            player=1,
            cancelable=False,
            finishable=True,
            min_=1,
            max_=2,
            selectable_cards=[{"code": 1}],
            selected_cards=[],
        ),
    ),
    # place
    (
        MSG_SELECT_PLACE,
        "MSG_SELECT_PLACE",
        SelectPlace(player=1, min_=1, field_mask=0x0),
    ),  # all zones open
    (MSG_SELECT_DISFIELD, "MSG_SELECT_DISFIELD", SelectPlace(player=1, min_=1, field_mask=0x0)),
    # position
    (
        MSG_SELECT_POSITION,
        "MSG_SELECT_POSITION",
        SelectPosition(player=1, code=0, positions=0x5),
    ),  # ATK + DEF
    # counter
    (
        MSG_SELECT_COUNTER,
        "MSG_SELECT_COUNTER",
        SelectCounter(player=1, counter_type=0, count=2, cards=[{"code": 1, "counter": 3}]),
    ),
    # sum
    (
        MSG_SELECT_SUM,
        "MSG_SELECT_SUM",
        SelectSum(
            player=1,
            mode=0,
            sumval=5,
            min_=1,
            max_=3,
            mandatory_cards=[],
            optional_cards=[{"code": 1}],
        ),
    ),
    # sort
    (MSG_SORT_CARD, "MSG_SORT_CARD", SortCard(player=1, cards=[{"code": 1}, {"code": 2}])),
    (MSG_SORT_CHAIN, "MSG_SORT_CHAIN", SortCard(player=1, cards=[{"code": 1}])),
    # announce
    (MSG_ANNOUNCE_RACE, "MSG_ANNOUNCE_RACE", AnnounceRace(player=1, count=1, available=0x3)),
    (MSG_ANNOUNCE_ATTRIB, "MSG_ANNOUNCE_ATTRIB", AnnounceAttrib(player=1, count=1, available=0x3)),
    (MSG_ANNOUNCE_CARD, "MSG_ANNOUNCE_CARD", AnnounceCard(player=1, opcodes=[])),
    (MSG_ANNOUNCE_NUMBER, "MSG_ANNOUNCE_NUMBER", AnnounceNumber(player=1, numbers=[1, 2])),
    (MSG_ROCK_PAPER_SCISSORS, "MSG_ROCK_PAPER_SCISSORS", None),  # RPS has no parsed struct
]


@pytest.mark.parametrize("mt,name,parsed", CASES, ids=[c[1] for c in CASES])
def test_passive_opponent_returns_valid_tool(mt: int, name: str, parsed) -> None:
    pd = _pd(mt, name, parsed)
    tool_name, kwargs = _pick_passive_opponent_response(pd)
    assert tool_name in TOOL_TO_HARNESS_METHOD, (
        f"passive returned tool {tool_name!r} which is not a response verb"
    )
    assert isinstance(kwargs, dict)


def test_passive_declines_optional_chain() -> None:
    chain = SelectChain(player=1, forced=False, cards=[{"code": 1}])
    pd = _pd(MSG_SELECT_CHAIN, "MSG_SELECT_CHAIN", chain)
    tool, args = _pick_passive_opponent_response(pd)
    assert tool == "select_chain"
    assert args == {"index": None}, "optional chain must decline"


def test_passive_takes_forced_chain() -> None:
    chain = SelectChain(player=1, forced=True, cards=[{"code": 1}])
    pd = _pd(MSG_SELECT_CHAIN, "MSG_SELECT_CHAIN", chain)
    tool, args = _pick_passive_opponent_response(pd)
    assert args == {"index": 0}, "forced chain must pick a numeric index"


def test_passive_declines_optional_yesno_effectyn() -> None:
    for mt, name, parsed in (
        (MSG_SELECT_YESNO, "yesno", SelectYesNo(player=1, desc=0)),
        (
            MSG_SELECT_EFFECTYN,
            "effectyn",
            SelectEffectYn(player=1, code=0, con=0, loc=0, seq=0, pos=0, desc=0),
        ),
    ):
        pd = _pd(mt, name, parsed)
        tool, args = _pick_passive_opponent_response(pd)
        assert args == {"accept": False}, f"opponent must decline optional {name}"
