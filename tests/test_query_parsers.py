"""QUERY_* wire-format parser tests.

Exercises the three query parsers in ``core.py``:
- ``_parse_query_response``          — one card's QUERY fields
- ``_parse_query_location_response`` — a whole location (deck/hand/…)
- ``_parse_query_field_response``    — the full-field snapshot

Per-card wire format (per EDOPro ``field::query_card`` /
``card::get_info_location``):
    repeated { u16 size ; u32 flag ; payload (size - 4 bytes) }
    terminator { u16 size ; u32 QUERY_END (0x80000000) }

Fixed-width flags emit a native-width integer. Special flags
(REASON_CARD, EQUIP_CARD, TARGET_CARD, OVERLAY_CARD, COUNTERS, LINK)
have bespoke payload layouts — each is pinned here.

A location query wraps the per-card payload in a u32 total-size header,
with each slot either a u16(0) empty-marker or a full card block.

A field query is its own beast: duel_options + two player structs
+ chain summary.
"""
from __future__ import annotations

import struct

import pytest

import engine.core as engine
from engine.core import (
    _parse_query_response,
    _parse_query_location_response,
    _parse_query_field_response,
)


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------
def u16(v: int) -> bytes: return struct.pack("<H", v)
def u32(v: int, signed: bool = False) -> bytes:
    return struct.pack("<i" if signed else "<I", v)
def u64(v: int) -> bytes: return struct.pack("<Q", v)
def u8(v: int) -> bytes: return struct.pack("<B", v)


def _field(flag: int, payload: bytes) -> bytes:
    """One QUERY field: u16 size, u32 flag, payload. `size` covers flag+payload."""
    size = 4 + len(payload)
    return u16(size) + u32(flag) + payload


def _card(fields: list[bytes]) -> bytes:
    """Concatenate fields and append QUERY_END terminator (size=4, flag=END)."""
    return b"".join(fields) + u16(4) + u32(engine.QUERY_END)


def loc_info(con: int, loc: int, seq: int, pos: int) -> bytes:
    return struct.pack("<BBII", con, loc, seq, pos)


# ---------------------------------------------------------------------------
# Fixed-width QUERY_* flags — one-field-per-test keeps failures surgical.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("flag,key,payload,expected", [
    (engine.QUERY_CODE,          "code",          u32(0x00004ce5), 0x00004ce5),
    (engine.QUERY_POSITION,      "position",      u32(engine.POS_FACEUP_ATTACK),
                                                    engine.POS_FACEUP_ATTACK),
    (engine.QUERY_ALIAS,         "alias",         u32(0x12345),    0x12345),
    (engine.QUERY_TYPE,          "type",          u32(engine.TYPE_MONSTER | engine.TYPE_EFFECT),
                                                    engine.TYPE_MONSTER | engine.TYPE_EFFECT),
    (engine.QUERY_LEVEL,         "level",         u32(4),          4),
    (engine.QUERY_RANK,          "rank",          u32(7),          7),
    (engine.QUERY_ATTRIBUTE,     "attribute",     u32(engine.ATTRIBUTE_DARK),
                                                    engine.ATTRIBUTE_DARK),
    (engine.QUERY_RACE,          "race",          u64(engine.RACE_WARRIOR),
                                                    engine.RACE_WARRIOR),
    (engine.QUERY_ATTACK,        "attack",        u32(2500),       2500),
    (engine.QUERY_DEFENSE,       "defense",       u32(2000),       2000),
    (engine.QUERY_BASE_ATTACK,   "base_attack",   u32(1800),       1800),
    (engine.QUERY_BASE_DEFENSE,  "base_defense",  u32(1200),       1200),
    (engine.QUERY_REASON,        "reason",        u32(engine.REASON_EFFECT),
                                                    engine.REASON_EFFECT),
    (engine.QUERY_OWNER,         "owner",         u8(1),           1),
    (engine.QUERY_STATUS,        "status",        u32(engine.STATUS_EFFECT_ENABLED),
                                                    engine.STATUS_EFFECT_ENABLED),
    (engine.QUERY_IS_PUBLIC,     "is_public",     u8(1),           1),
    (engine.QUERY_LSCALE,        "lscale",        u32(4),          4),
    (engine.QUERY_RSCALE,        "rscale",        u32(4),          4),
    (engine.QUERY_IS_HIDDEN,     "is_hidden",     u8(0),           0),
    (engine.QUERY_COVER,         "cover",         u32(0xdead),     0xdead),
])
def test_query_fixed_width_flag(flag: int, key: str,
                                 payload: bytes, expected: int) -> None:
    buf = _card([_field(flag, payload)])
    out = _parse_query_response(buf)
    assert out[key] == expected


def test_query_attack_defense_signed() -> None:
    """ATK/DEF are signed — ``?`` (unknown) is emitted as -2."""
    buf = _card([
        _field(engine.QUERY_ATTACK, u32(-2, signed=True)),
        _field(engine.QUERY_DEFENSE, u32(-1, signed=True)),
    ])
    out = _parse_query_response(buf)
    assert out["attack"] == -2
    assert out["defense"] == -1


# ---------------------------------------------------------------------------
# Special flags — variable-width payloads
# ---------------------------------------------------------------------------
def test_query_reason_card() -> None:
    li = loc_info(0, engine.LOCATION_SZONE, 3, engine.POS_FACEUP_ATTACK)
    buf = _card([_field(engine.QUERY_REASON_CARD, li)])
    out = _parse_query_response(buf)
    assert out["reason_card"] == {
        "con": 0, "loc": engine.LOCATION_SZONE,
        "seq": 3, "pos": engine.POS_FACEUP_ATTACK,
    }


def test_query_equip_card() -> None:
    li = loc_info(1, engine.LOCATION_MZONE, 2, engine.POS_FACEUP_ATTACK)
    buf = _card([_field(engine.QUERY_EQUIP_CARD, li)])
    out = _parse_query_response(buf)
    assert out["equip_card"] == {
        "con": 1, "loc": engine.LOCATION_MZONE,
        "seq": 2, "pos": engine.POS_FACEUP_ATTACK,
    }


def test_query_target_card_multiple() -> None:
    payload = u32(2) + loc_info(0, engine.LOCATION_MZONE, 0,
                                  engine.POS_FACEUP_ATTACK) + \
              loc_info(1, engine.LOCATION_MZONE, 2, engine.POS_FACEUP_ATTACK)
    buf = _card([_field(engine.QUERY_TARGET_CARD, payload)])
    out = _parse_query_response(buf)
    assert len(out["targets"]) == 2
    assert out["targets"][0]["con"] == 0
    assert out["targets"][1]["con"] == 1


def test_query_target_card_empty() -> None:
    buf = _card([_field(engine.QUERY_TARGET_CARD, u32(0))])
    out = _parse_query_response(buf)
    assert out["targets"] == []


def test_query_overlay_card() -> None:
    """Overlay units = XYZ materials stacked under the card."""
    payload = u32(3) + u32(0x1111) + u32(0x2222) + u32(0x3333)
    buf = _card([_field(engine.QUERY_OVERLAY_CARD, payload)])
    out = _parse_query_response(buf)
    assert out["overlay"] == [0x1111, 0x2222, 0x3333]


def test_query_counters() -> None:
    """Counters: packed u32 per entry — low u16 = type, high u16 = count."""
    # Two entries: type 0x1 x3, type 0x5 x7.
    payload = u32(2) + u32(0x00030001) + u32(0x00070005)
    buf = _card([_field(engine.QUERY_COUNTERS, payload)])
    out = _parse_query_response(buf)
    assert out["counters"] == {0x0001: 3, 0x0005: 7}


def test_query_link() -> None:
    """Link card: u32 link rating + u32 link-marker bitmask."""
    payload = u32(4) + u32(engine.LINK_MARKER_BOTTOM_LEFT
                            | engine.LINK_MARKER_BOTTOM_RIGHT)
    buf = _card([_field(engine.QUERY_LINK, payload)])
    out = _parse_query_response(buf)
    assert out["link"] == 4
    assert out["link_markers"] == (engine.LINK_MARKER_BOTTOM_LEFT
                                    | engine.LINK_MARKER_BOTTOM_RIGHT)


# ---------------------------------------------------------------------------
# Composite — many flags on one card, in typical emission order
# ---------------------------------------------------------------------------
def test_query_full_card_composite() -> None:
    """Realistic multi-field emission for a face-up attack monster."""
    buf = _card([
        _field(engine.QUERY_CODE, u32(0x00004ce5)),          # Dark Magician
        _field(engine.QUERY_POSITION, u32(engine.POS_FACEUP_ATTACK)),
        _field(engine.QUERY_ALIAS, u32(0)),
        _field(engine.QUERY_TYPE, u32(engine.TYPE_MONSTER | engine.TYPE_EFFECT)),
        _field(engine.QUERY_LEVEL, u32(7)),
        _field(engine.QUERY_ATTRIBUTE, u32(engine.ATTRIBUTE_DARK)),
        _field(engine.QUERY_RACE, u64(engine.RACE_SPELLCASTER)),
        _field(engine.QUERY_ATTACK, u32(2500, signed=True)),
        _field(engine.QUERY_DEFENSE, u32(2100, signed=True)),
        _field(engine.QUERY_STATUS, u32(engine.STATUS_EFFECT_ENABLED)),
        _field(engine.QUERY_OWNER, u8(0)),
    ])
    out = _parse_query_response(buf)
    assert out["code"] == 0x00004ce5
    assert out["position"] == engine.POS_FACEUP_ATTACK
    assert out["type"] == engine.TYPE_MONSTER | engine.TYPE_EFFECT
    assert out["level"] == 7
    assert out["attribute"] == engine.ATTRIBUTE_DARK
    assert out["race"] == engine.RACE_SPELLCASTER
    assert out["attack"] == 2500
    assert out["defense"] == 2100
    assert out["owner"] == 0


def test_query_end_terminates_early() -> None:
    """QUERY_END stops parsing — trailing bytes must not leak into the result."""
    buf = (_field(engine.QUERY_CODE, u32(0x1234))
           + u16(4) + u32(engine.QUERY_END)
           + b"\xff\xff\xff\xff")          # bytes past the terminator
    out = _parse_query_response(buf)
    assert out["code"] == 0x1234
    assert "_end_offset" in out
    # _end_offset points past the QUERY_END header (10 bytes code field + 6 END).
    assert out["_end_offset"] == 16


def test_query_empty_buffer() -> None:
    assert _parse_query_response(b"") == {}
    assert _parse_query_response(b"\x00") == {}


def test_query_unknown_flag_falls_through_to_raw() -> None:
    """Non-recognised flags surface as ``flag_<hex>`` with the raw payload."""
    # 0x04000000 is reserved / unassigned in the header.
    unk = 0x04000000
    buf = _card([_field(unk, b"\xaa\xbb\xcc")])
    out = _parse_query_response(buf)
    assert out[f"flag_{unk:x}"] == b"\xaa\xbb\xcc"


# ---------------------------------------------------------------------------
# _parse_query_location_response — wraps per-card blocks with total-size u32
# ---------------------------------------------------------------------------
def test_query_location_empty_slots() -> None:
    """Locations with sparse occupancy emit u16(0) for empty slots."""
    # 3 slots: empty, card, empty.
    card = _card([_field(engine.QUERY_CODE, u32(0x1234))])
    inner = u16(0) + card + u16(0)
    buf = u32(len(inner)) + inner
    out = _parse_query_location_response(buf)
    assert out == [None, {"code": 0x1234}, None]


def test_query_location_single_card() -> None:
    card = _card([
        _field(engine.QUERY_CODE, u32(0x4444)),
        _field(engine.QUERY_POSITION, u32(engine.POS_FACEDOWN_DEFENSE)),
    ])
    buf = u32(len(card)) + card
    out = _parse_query_location_response(buf)
    assert len(out) == 1
    assert out[0]["code"] == 0x4444
    assert out[0]["position"] == engine.POS_FACEDOWN_DEFENSE


def test_query_location_empty_buffer_returns_empty_list() -> None:
    assert _parse_query_location_response(b"") == []
    # A header saying "0 bytes follow" is also valid → empty location.
    assert _parse_query_location_response(u32(0)) == []


# ---------------------------------------------------------------------------
# _parse_query_field_response — full duel snapshot
# ---------------------------------------------------------------------------
def _empty_zone() -> bytes:
    """One zone slot: u8(0) = empty."""
    return u8(0)


def _occupied_zone(pos: int, overlay_count: int) -> bytes:
    """One zone slot: u8(1) + u8 pos + u32 overlay_count."""
    return u8(1) + u8(pos) + u32(overlay_count)


def _player_snapshot(lp: int,
                     mzone: list[bytes],
                     szone: list[bytes],
                     deck: int, hand: int, grave: int,
                     removed: int, extra: int, extra_p: int) -> bytes:
    return (u32(lp)
            + b"".join(mzone)
            + b"".join(szone)
            + u32(deck) + u32(hand) + u32(grave)
            + u32(removed) + u32(extra) + u32(extra_p))


def test_query_field_response_basic() -> None:
    mz = [_empty_zone()] * engine.MZONE_SLOTS
    sz = [_empty_zone()] * engine.SZONE_SLOTS
    mz[2] = _occupied_zone(engine.POS_FACEUP_ATTACK, 0)
    sz[4] = _occupied_zone(engine.POS_FACEDOWN_DEFENSE, 0)

    p0 = _player_snapshot(8000, mz, sz, 35, 5, 0, 0, 15, 0)
    # Reset zones for player 1 (fresh empties).
    mz2 = [_empty_zone()] * engine.MZONE_SLOTS
    sz2 = [_empty_zone()] * engine.SZONE_SLOTS
    p1 = _player_snapshot(6000, mz2, sz2, 30, 4, 2, 1, 15, 0)

    chain = u32(0)  # no chain
    buf = u32(engine.DUEL_SIMPLE_AI) + p0 + p1 + chain

    out = _parse_query_field_response(buf)
    assert out["duel_options"] == engine.DUEL_SIMPLE_AI
    assert out["players"][0]["lp"] == 8000
    assert out["players"][1]["lp"] == 6000
    assert out["players"][0]["mzone_summary"][2] == {
        "has_card": True,
        "position": engine.POS_FACEUP_ATTACK,
        "overlay_count": 0,
    }
    assert out["players"][0]["szone_summary"][4] == {
        "has_card": True,
        "position": engine.POS_FACEDOWN_DEFENSE,
        "overlay_count": 0,
    }
    assert out["players"][0]["deck_count"] == 35
    assert out["players"][0]["hand_count_raw"] == 5
    assert out["players"][1]["grave_count_raw"] == 2
    assert out["chain"] == []


def test_query_field_response_with_chain() -> None:
    mz = [_empty_zone()] * engine.MZONE_SLOTS
    sz = [_empty_zone()] * engine.SZONE_SLOTS
    p0 = _player_snapshot(8000, mz, sz, 40, 5, 0, 0, 15, 0)
    mz2 = [_empty_zone()] * engine.MZONE_SLOTS
    sz2 = [_empty_zone()] * engine.SZONE_SLOTS
    p1 = _player_snapshot(8000, mz2, sz2, 40, 5, 0, 0, 15, 0)

    # One chain link. 4 (code) + 1+1+4+4 (info loc_info) + 1+1+4 (trg) + 8 (desc) = 28 bytes.
    chain_entry = (u32(0x00004ce5)                               # code
                   + u8(0) + u8(engine.LOCATION_MZONE) + u32(3) + u32(engine.POS_FACEUP_ATTACK)
                   + u8(1) + u8(engine.LOCATION_MZONE) + u32(2)
                   + u64(0xCAFEBABE))
    chain = u32(1) + chain_entry
    buf = u32(0) + p0 + p1 + chain

    out = _parse_query_field_response(buf)
    assert len(out["chain"]) == 1
    link = out["chain"][0]
    assert link["code"] == 0x00004ce5
    assert link["effect_location"] == {
        "con": 0, "loc": engine.LOCATION_MZONE,
        "seq": 3, "pos": engine.POS_FACEUP_ATTACK,
    }
    assert link["trigger_location"] == {
        "con": 1, "loc": engine.LOCATION_MZONE, "seq": 2,
    }
    assert link["description"] == 0xCAFEBABE


def test_query_field_response_short_buffer_returns_empty() -> None:
    assert _parse_query_field_response(b"") == {}
    assert _parse_query_field_response(b"\x00") == {}
