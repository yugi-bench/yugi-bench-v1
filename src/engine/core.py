"""OCG engine wrapper — libocgcore FFI, message parsers, state rendering.

Thin ctypes binding over EDOPro's ``libocgcore``. Owns nothing above the
engine boundary: no action DSL, no responder logic, no LLM protocol. Callers
drive the duel via ``OCGEngine.process()`` / ``.get_message()`` / parsed
``Select*`` dataclasses, then write raw bytes back with
``set_response_*``. See ``harness.py`` for the one-to-one responder layer
over ``field::process(Processors::X&)``.

Requires:
  - ``libocgcore.so`` / ``.dylib`` from EDOPro
  - ``.cdb`` card databases (``YGO_DB_DIR``)
  - Card scripts (``YGO_SCRIPT_DIR`` + ``YGO_CARD_SCRIPT_DIR``)
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import re
import sqlite3
import struct
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Default paths (override via env vars or constructor args)
# ---------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _first_existing(*candidates: Path) -> Path:
    """Return the first candidate path that exists, else the first one.

    Used to pick between the modern in-repo ``vendor/`` layout (populated
    by ``setup.sh``) and the legacy sibling layout (``../edopro``,
    ``../distribution``) the README used to document.  Env vars
    (``YGO_DYLIB``, ``YGO_DB_DIR``, ``YGO_SCRIPT_DIR``,
    ``YGO_CARD_SCRIPT_DIR``) still override everything.
    """
    for c in candidates:
        if c.exists():
            return c
    return candidates[0]


def _default_dylib() -> Path:
    env = os.environ.get("YGO_DYLIB")
    if env:
        return Path(env)
    suffix = "libocgcore.dylib" if sys.platform == "darwin" else "libocgcore.so"
    return _first_existing(
        REPO_ROOT / "vendor" / "ygopro-core" / "bin" / "release" / suffix,
        REPO_ROOT.parent / "edopro" / "ocgcore" / "bin" / "release" / suffix,
    )


DYLIB_PATH = _default_dylib()
SCRIPT_DIR = Path(
    os.environ.get(
        "YGO_SCRIPT_DIR",
        str(
            _first_existing(
                REPO_ROOT / "vendor" / "distribution" / "script",
                REPO_ROOT.parent / "distribution" / "script",
            )
        ),
    )
)
CARD_SCRIPT_DIR = Path(os.environ.get("YGO_CARD_SCRIPT_DIR", str(SCRIPT_DIR / "official")))
DB_DIR = Path(
    os.environ.get(
        "YGO_DB_DIR",
        str(
            _first_existing(
                REPO_ROOT / "vendor" / "distribution" / "expansions",
                REPO_ROOT.parent / "distribution" / "expansions",
            )
        ),
    )
)

# ---------------------------------------------------------------------------
# OCG constants
# ---------------------------------------------------------------------------
DUEL_TEST_MODE = 0x01
DUEL_ATTACK_FIRST_TURN = 0x02
DUEL_USE_TRAPS_IN_NEW_CHAIN = 0x04
DUEL_6_STEP_BATLLE_STEP = 0x08
DUEL_PSEUDO_SHUFFLE = 0x10
DUEL_TRIGGER_WHEN_PRIVATE_KNOWLEDGE = 0x20
DUEL_SIMPLE_AI = 0x40
DUEL_RELAY = 0x80
DUEL_OCG_OBSOLETE_IGNITION = 0x100
DUEL_1ST_TURN_DRAW = 0x200
DUEL_1_FACEUP_FIELD = 0x400
DUEL_PZONE = 0x800
DUEL_SEPARATE_PZONE = 0x1000
DUEL_EMZONE = 0x2000
DUEL_FSX_MMZONE = 0x4000
DUEL_TRAP_MONSTERS_NOT_USE_ZONE = 0x8000
DUEL_RETURN_TO_DECK_TRIGGERS = 0x10000
DUEL_TRIGGER_ONLY_IN_LOCATION = 0x20000
DUEL_SPSUMMON_ONCE_OLD_NEGATE = 0x40000
DUEL_CANNOT_SUMMON_OATH_OLD = 0x80000
DUEL_NO_STANDBY_PHASE = 0x100000
DUEL_NO_MAIN_PHASE_2 = 0x200000
DUEL_3_COLUMNS_FIELD = 0x400000
DUEL_DRAW_UNTIL_5 = 0x800000
DUEL_NO_HAND_LIMIT = 0x1000000
DUEL_UNLIMITED_SUMMONS = 0x2000000
DUEL_INVERTED_QUICK_PRIORITY = 0x4000000
DUEL_EQUIP_NOT_SENT_IF_MISSING_TARGET = 0x8000000
DUEL_0_ATK_DESTROYED = 0x10000000
DUEL_STORE_ATTACK_REPLAYS = 0x20000000
DUEL_SINGLE_CHAIN_IN_DAMAGE_SUBSTEP = 0x40000000
DUEL_CAN_REPOS_IF_NON_SUMPLAYER = 0x80000000
DUEL_TCG_SEGOC_NONPUBLIC = 0x100000000
DUEL_TCG_SEGOC_FIRSTTRIGGER = 0x200000000
DUEL_TCG_FAST_EFFECT_IGNITION = 0x400000000
DUEL_EXTRA_DECK_RITUAL = 0x800000000
DUEL_NORMAL_SUMMON_FACEUP_DEF = 0x1000000000

DUEL_MODE_SPEED = (
    DUEL_3_COLUMNS_FIELD
    | DUEL_NO_MAIN_PHASE_2
    | DUEL_TRAP_MONSTERS_NOT_USE_ZONE
    | DUEL_TRIGGER_ONLY_IN_LOCATION
)
DUEL_MODE_RUSH = (
    DUEL_3_COLUMNS_FIELD
    | DUEL_NO_MAIN_PHASE_2
    | DUEL_NO_STANDBY_PHASE
    | DUEL_1ST_TURN_DRAW
    | DUEL_INVERTED_QUICK_PRIORITY
    | DUEL_DRAW_UNTIL_5
    | DUEL_NO_HAND_LIMIT
    | DUEL_UNLIMITED_SUMMONS
    | DUEL_TRAP_MONSTERS_NOT_USE_ZONE
    | DUEL_TRIGGER_ONLY_IN_LOCATION
    | DUEL_EXTRA_DECK_RITUAL
)
DUEL_MODE_MR1 = (
    DUEL_OCG_OBSOLETE_IGNITION
    | DUEL_1ST_TURN_DRAW
    | DUEL_1_FACEUP_FIELD
    | DUEL_SPSUMMON_ONCE_OLD_NEGATE
    | DUEL_RETURN_TO_DECK_TRIGGERS
    | DUEL_CANNOT_SUMMON_OATH_OLD
)
DUEL_MODE_GOAT = (
    DUEL_MODE_MR1
    | DUEL_TCG_FAST_EFFECT_IGNITION
    | DUEL_USE_TRAPS_IN_NEW_CHAIN
    | DUEL_6_STEP_BATLLE_STEP
    | DUEL_TRIGGER_WHEN_PRIVATE_KNOWLEDGE
    | DUEL_EQUIP_NOT_SENT_IF_MISSING_TARGET
    | DUEL_0_ATK_DESTROYED
    | DUEL_STORE_ATTACK_REPLAYS
    | DUEL_SINGLE_CHAIN_IN_DAMAGE_SUBSTEP
    | DUEL_CAN_REPOS_IF_NON_SUMPLAYER
    | DUEL_TCG_SEGOC_NONPUBLIC
    | DUEL_TCG_SEGOC_FIRSTTRIGGER
)
DUEL_MODE_MR2 = (
    DUEL_1ST_TURN_DRAW
    | DUEL_1_FACEUP_FIELD
    | DUEL_SPSUMMON_ONCE_OLD_NEGATE
    | DUEL_RETURN_TO_DECK_TRIGGERS
    | DUEL_CANNOT_SUMMON_OATH_OLD
)
DUEL_MODE_MR3 = (
    DUEL_PZONE
    | DUEL_SEPARATE_PZONE
    | DUEL_SPSUMMON_ONCE_OLD_NEGATE
    | DUEL_RETURN_TO_DECK_TRIGGERS
    | DUEL_CANNOT_SUMMON_OATH_OLD
)
DUEL_MODE_MR4 = (
    DUEL_PZONE
    | DUEL_EMZONE
    | DUEL_SPSUMMON_ONCE_OLD_NEGATE
    | DUEL_RETURN_TO_DECK_TRIGGERS
    | DUEL_CANNOT_SUMMON_OATH_OLD
)
DUEL_MODE_MR5 = (
    DUEL_PZONE
    | DUEL_EMZONE
    | DUEL_FSX_MMZONE
    | DUEL_TRAP_MONSTERS_NOT_USE_ZONE
    | DUEL_TRIGGER_ONLY_IN_LOCATION
)

# ---------------------------------------------------------------------------
# Card types / attributes / races / reasons / hints / link markers / opcodes
# (ocgapi_constants.h §Card Types, §Attributes, §Monster Races, §Event
# Reasons, §Duel Hints, §Card Hints, §Player Hints, §Link Markers,
# §Announce Card Opcodes) — kept 1:1 so wire-format consumers can reference
# them by name.
# ---------------------------------------------------------------------------
TYPE_MONSTER = 0x1
TYPE_SPELL = 0x2
TYPE_TRAP = 0x4
TYPE_NORMAL = 0x10
TYPE_EFFECT = 0x20
TYPE_FUSION = 0x40
TYPE_RITUAL = 0x80
TYPE_TRAPMONSTER = 0x100
TYPE_SPIRIT = 0x200
TYPE_UNION = 0x400
TYPE_GEMINI = 0x800
TYPE_TUNER = 0x1000
TYPE_SYNCHRO = 0x2000
TYPE_TOKEN = 0x4000
TYPE_MAXIMUM = 0x8000
TYPE_QUICKPLAY = 0x10000
TYPE_CONTINUOUS = 0x20000
TYPE_EQUIP = 0x40000
TYPE_FIELD = 0x80000
TYPE_COUNTER = 0x100000
TYPE_FLIP = 0x200000
TYPE_TOON = 0x400000
TYPE_XYZ = 0x800000
TYPE_PENDULUM = 0x1000000
TYPE_SPSUMMON = 0x2000000
TYPE_LINK = 0x4000000

ATTRIBUTE_EARTH = 0x01
ATTRIBUTE_WATER = 0x02
ATTRIBUTE_FIRE = 0x04
ATTRIBUTE_WIND = 0x08
ATTRIBUTE_LIGHT = 0x10
ATTRIBUTE_DARK = 0x20
ATTRIBUTE_DIVINE = 0x40

RACE_WARRIOR = 0x1
RACE_SPELLCASTER = 0x2
RACE_FAIRY = 0x4
RACE_FIEND = 0x8
RACE_ZOMBIE = 0x10
RACE_MACHINE = 0x20
RACE_AQUA = 0x40
RACE_PYRO = 0x80
RACE_ROCK = 0x100
RACE_WINGEDBEAST = 0x200
RACE_PLANT = 0x400
RACE_INSECT = 0x800
RACE_THUNDER = 0x1000
RACE_DRAGON = 0x2000
RACE_BEAST = 0x4000
RACE_BEASTWARRIOR = 0x8000
RACE_DINOSAUR = 0x10000
RACE_FISH = 0x20000
RACE_SEASERPENT = 0x40000
RACE_REPTILE = 0x80000
RACE_PSYCHIC = 0x100000
RACE_DIVINE = 0x200000
RACE_CREATORGOD = 0x400000
RACE_WYRM = 0x800000
RACE_CYBERSE = 0x1000000
RACE_ILLUSION = 0x2000000
RACE_CYBORG = 0x4000000
RACE_MAGICALKNIGHT = 0x8000000
RACE_HIGHDRAGON = 0x10000000
RACE_OMEGAPSYCHIC = 0x20000000
RACE_CELESTIALWARRIOR = 0x40000000
RACE_GALAXY = 0x80000000
RACE_YOKAI = 0x4000000000000000

REASON_DESTROY = 0x1
REASON_RELEASE = 0x2
REASON_TEMPORARY = 0x4
REASON_MATERIAL = 0x8
REASON_SUMMON = 0x10
REASON_BATTLE = 0x20
REASON_EFFECT = 0x40
REASON_COST = 0x80
REASON_ADJUST = 0x100
REASON_LOST_TARGET = 0x200
REASON_RULE = 0x400
REASON_SPSUMMON = 0x800
REASON_DISSUMMON = 0x1000
REASON_FLIP = 0x2000
REASON_DISCARD = 0x4000
REASON_RDAMAGE = 0x8000
REASON_RRECOVER = 0x10000
REASON_RETURN = 0x20000
REASON_FUSION = 0x40000
REASON_SYNCHRO = 0x80000
REASON_RITUAL = 0x100000
REASON_XYZ = 0x200000
REASON_REPLACE = 0x1000000
REASON_DRAW = 0x2000000
REASON_REDIRECT = 0x4000000
REASON_LINK = 0x10000000

LINK_MARKER_BOTTOM_LEFT = 0o001
LINK_MARKER_BOTTOM = 0o002
LINK_MARKER_BOTTOM_RIGHT = 0o004
LINK_MARKER_LEFT = 0o010
LINK_MARKER_RIGHT = 0o040
LINK_MARKER_TOP_LEFT = 0o100
LINK_MARKER_TOP = 0o200
LINK_MARKER_TOP_RIGHT = 0o400

HINT_EVENT = 1
HINT_MESSAGE = 2
HINT_SELECTMSG = 3
HINT_OPSELECTED = 4
HINT_EFFECT = 5
HINT_RACE = 6
HINT_ATTRIB = 7
HINT_CODE = 8
HINT_NUMBER = 9
HINT_CARD = 10
HINT_ZONE = 11

CHINT_TURN = 1
CHINT_CARD = 2
CHINT_RACE = 3
CHINT_ATTRIBUTE = 4
CHINT_NUMBER = 5
CHINT_DESC_ADD = 6
CHINT_DESC_REMOVE = 7

PHINT_DESC_ADD = 6
PHINT_DESC_REMOVE = 7

OPCODE_ADD = 0x4000000000000000
OPCODE_SUB = 0x4000000100000000
OPCODE_MUL = 0x4000000200000000
OPCODE_DIV = 0x4000000300000000
OPCODE_AND = 0x4000000400000000
OPCODE_OR = 0x4000000500000000
OPCODE_NEG = 0x4000000600000000
OPCODE_NOT = 0x4000000700000000
OPCODE_BAND = 0x4000000800000000
OPCODE_BOR = 0x4000000900000000
OPCODE_BNOT = 0x4000001000000000
OPCODE_BXOR = 0x4000001100000000
OPCODE_LSHIFT = 0x4000001200000000
OPCODE_RSHIFT = 0x4000001300000000
OPCODE_ALLOW_ALIASES = 0x4000001400000000
OPCODE_ALLOW_TOKENS = 0x4000001500000000
OPCODE_ISCODE = 0x4000010000000000
OPCODE_ISSETCARD = 0x4000010100000000
OPCODE_ISTYPE = 0x4000010200000000
OPCODE_ISRACE = 0x4000010300000000
OPCODE_ISATTRIBUTE = 0x4000010400000000
OPCODE_GETCODE = 0x4000010500000000
OPCODE_GETSETCARD = 0x4000010600000000
OPCODE_GETTYPE = 0x4000010700000000
OPCODE_GETRACE = 0x4000010800000000
OPCODE_GETATTRIBUTE = 0x4000010900000000

LOCATION_DECK = 0x01
LOCATION_HAND = 0x02
LOCATION_MZONE = 0x04
LOCATION_SZONE = 0x08
LOCATION_GRAVE = 0x10
LOCATION_REMOVED = 0x20
LOCATION_EXTRA = 0x40
LOCATION_FZONE = 0x100
LOCATION_PZONE = 0x200
LOCATION_OVERLAY = 0x80

POS_FACEUP_ATTACK = 0x1
POS_FACEDOWN_ATTACK = 0x2
POS_FACEUP_DEFENSE = 0x4
POS_FACEDOWN_DEFENSE = 0x8

MSG_RETRY = 1
MSG_HINT = 2
MSG_WAITING = 3
MSG_START = 4
MSG_WIN = 5
MSG_UPDATE_DATA = 6
MSG_UPDATE_CARD = 7
MSG_REQUEST_DECK = 8
MSG_SELECT_BATTLECMD = 10
MSG_SELECT_IDLECMD = 11
MSG_SELECT_EFFECTYN = 12
MSG_SELECT_YESNO = 13
MSG_SELECT_OPTION = 14
MSG_SELECT_CARD = 15
MSG_SELECT_CHAIN = 16
MSG_SELECT_PLACE = 18
MSG_SELECT_POSITION = 19
MSG_SELECT_TRIBUTE = 20
MSG_SORT_CHAIN = 21
MSG_SELECT_COUNTER = 22
MSG_SELECT_SUM = 23
MSG_SELECT_DISFIELD = 24
MSG_SORT_CARD = 25
MSG_SELECT_UNSELECT_CARD = 26
MSG_CONFIRM_DECKTOP = 30
MSG_CONFIRM_CARDS = 31
MSG_SHUFFLE_DECK = 32
MSG_SHUFFLE_HAND = 33
MSG_REFRESH_DECK = 34
MSG_SWAP_GRAVE_DECK = 35
MSG_SHUFFLE_SET_CARD = 36
MSG_REVERSE_DECK = 37
MSG_DECK_TOP = 38
MSG_SHUFFLE_EXTRA = 39
MSG_NEW_TURN = 40
MSG_NEW_PHASE = 41
MSG_CONFIRM_EXTRATOP = 42
MSG_MOVE = 50
MSG_POS_CHANGE = 53
MSG_SET = 54
MSG_SWAP = 55
MSG_FIELD_DISABLED = 56
MSG_SUMMONING = 60
MSG_SUMMONED = 61
MSG_SPSUMMONING = 62
MSG_SPSUMMONED = 63
MSG_FLIPSUMMONING = 64
MSG_FLIPSUMMONED = 65
MSG_CHAINING = 70
MSG_CHAINED = 71
MSG_CHAIN_SOLVING = 72
MSG_CHAIN_SOLVED = 73
MSG_CHAIN_END = 74
MSG_CHAIN_NEGATED = 75
MSG_CHAIN_DISABLED = 76
MSG_CARD_SELECTED = 80
MSG_RANDOM_SELECTED = 81
MSG_BECOME_TARGET = 83
MSG_DRAW = 90
MSG_DAMAGE = 91
MSG_RECOVER = 92
MSG_EQUIP = 93
MSG_LPUPDATE = 94
MSG_UNEQUIP = 95
MSG_CARD_TARGET = 96
MSG_CANCEL_TARGET = 97
MSG_PAY_LPCOST = 100
MSG_ADD_COUNTER = 101
MSG_REMOVE_COUNTER = 102
MSG_ATTACK = 110
MSG_BATTLE = 111
MSG_ATTACK_DISABLED = 112
MSG_DAMAGE_STEP_START = 113
MSG_DAMAGE_STEP_END = 114
MSG_MISSED_EFFECT = 120
MSG_BE_CHAIN_TARGET = 121
MSG_CREATE_RELATION = 122
MSG_RELEASE_RELATION = 123
MSG_TOSS_COIN = 130
MSG_TOSS_DICE = 131
MSG_ROCK_PAPER_SCISSORS = 132
MSG_HAND_RES = 133
MSG_ANNOUNCE_RACE = 140
MSG_ANNOUNCE_ATTRIB = 141
MSG_ANNOUNCE_CARD = 142
MSG_ANNOUNCE_NUMBER = 143
MSG_CARD_HINT = 160
MSG_TAG_SWAP = 161
MSG_RELOAD_FIELD = 162
MSG_AI_NAME = 163
MSG_SHOW_HINT = 164
MSG_PLAYER_HINT = 165
MSG_MATCH_KILL = 170
MSG_CUSTOM_MSG = 180
MSG_REMOVE_CARDS = 190

# IdleCmd action types: t = action & 0xffff, s = action >> 16
IDLE_SUMMON = 0
IDLE_SPSUMMON = 1
IDLE_REPOS = 2
IDLE_MSET = 3
IDLE_SSET = 4
IDLE_ACTIVATE = 5
IDLE_TO_BP = 6
IDLE_TO_EP = 7
IDLE_SHUFFLE = 8

# BattleCmd action types
BATTLE_ACTIVATE = 0
BATTLE_ATTACK = 1
BATTLE_TO_M2 = 2
BATTLE_TO_EP = 3

OCG_DUEL_STATUS_END = 0
OCG_DUEL_STATUS_AWAITING = 1
OCG_DUEL_STATUS_CONTINUE = 2

PHASE_DRAW = 0x01
PHASE_STANDBY = 0x02
PHASE_MAIN1 = 0x04
PHASE_BATTLE_START = 0x08
PHASE_BATTLE_STEP = 0x10
PHASE_DAMAGE = 0x20
PHASE_DAMAGE_CAL = 0x40
PHASE_BATTLE = 0x80
PHASE_MAIN2 = 0x100
PHASE_END = 0x200

QUERY_CODE = 0x1
QUERY_POSITION = 0x2
QUERY_ALIAS = 0x4
QUERY_TYPE = 0x8
QUERY_LEVEL = 0x10
QUERY_RANK = 0x20
QUERY_ATTRIBUTE = 0x40
QUERY_RACE = 0x80
QUERY_ATTACK = 0x100
QUERY_DEFENSE = 0x200
QUERY_BASE_ATTACK = 0x400
QUERY_BASE_DEFENSE = 0x800
QUERY_REASON = 0x1000
QUERY_REASON_CARD = 0x2000
QUERY_EQUIP_CARD = 0x4000
QUERY_TARGET_CARD = 0x8000
QUERY_OVERLAY_CARD = 0x10000
QUERY_COUNTERS = 0x20000
QUERY_OWNER = 0x40000
QUERY_STATUS = 0x80000
QUERY_IS_PUBLIC = 0x100000
QUERY_LSCALE = 0x200000
QUERY_RSCALE = 0x400000
QUERY_LINK = 0x800000
QUERY_IS_HIDDEN = 0x1000000
QUERY_COVER = 0x2000000
QUERY_END = 0x80000000

QUERY_FULL_CARD = (
    QUERY_CODE
    | QUERY_POSITION
    | QUERY_ALIAS
    | QUERY_TYPE
    | QUERY_LEVEL
    | QUERY_RANK
    | QUERY_ATTRIBUTE
    | QUERY_RACE
    | QUERY_ATTACK
    | QUERY_DEFENSE
    | QUERY_BASE_ATTACK
    | QUERY_BASE_DEFENSE
    | QUERY_OVERLAY_CARD
    | QUERY_COUNTERS
    | QUERY_OWNER
    | QUERY_STATUS
    | QUERY_IS_PUBLIC
    | QUERY_LSCALE
    | QUERY_RSCALE
    | QUERY_LINK
)

STATUS_DISABLED = 0x1
STATUS_TO_ENABLE = 0x2
STATUS_TO_DISABLE = 0x4
STATUS_PROC_COMPLETE = 0x8
STATUS_SET_TURN = 0x10
STATUS_NO_LEVEL = 0x20
STATUS_BATTLE_RESULT = 0x40
STATUS_SPSUMMON_STEP = 0x80
STATUS_FORM_CHANGED = 0x100
STATUS_SUMMONING = 0x200
STATUS_EFFECT_ENABLED = 0x400
STATUS_SUMMON_TURN = 0x800
STATUS_DESTROY_CONFIRMED = 0x1000
STATUS_LEAVE_CONFIRMED = 0x2000
STATUS_BATTLE_DESTROYED = 0x4000
STATUS_COPYING_EFFECT = 0x8000
STATUS_CHAINING = 0x10000
STATUS_SUMMON_DISABLED = 0x20000
STATUS_ACTIVATE_DISABLED = 0x40000
STATUS_EFFECT_REPLACED = 0x80000
STATUS_FUTURE_FUSION = 0x100000
STATUS_ATTACK_CANCELED = 0x200000
STATUS_INITIALIZING = 0x400000
STATUS_JUST_POS = 0x1000000
STATUS_CONTINUOUS_POS = 0x2000000
STATUS_FORBIDDEN = 0x4000000
STATUS_ACT_FROM_HAND = 0x8000000
STATUS_OPPO_BATTLE = 0x10000000
STATUS_FLIP_SUMMON_TURN = 0x20000000
STATUS_SPSUMMON_TURN = 0x40000000

# Zone counts (MR5)
MZONE_SLOTS = 7
SZONE_SLOTS = 8

MSG_NAME = {v: k for k, v in globals().items() if k.startswith("MSG_")}

SELECT_MSG_TYPES = {
    MSG_SELECT_IDLECMD,
    MSG_SELECT_BATTLECMD,
    MSG_SELECT_CARD,
    MSG_SELECT_TRIBUTE,
    MSG_SELECT_CHAIN,
    MSG_SELECT_EFFECTYN,
    MSG_SELECT_YESNO,
    MSG_SELECT_OPTION,
    MSG_SELECT_POSITION,
    MSG_SELECT_PLACE,
    MSG_SELECT_DISFIELD,
    MSG_SELECT_SUM,
    MSG_SELECT_UNSELECT_CARD,
    MSG_SELECT_COUNTER,
    MSG_SORT_CHAIN,
    MSG_SORT_CARD,
    MSG_ROCK_PAPER_SCISSORS,
    MSG_ANNOUNCE_RACE,
    MSG_ANNOUNCE_ATTRIB,
    MSG_ANNOUNCE_CARD,
    MSG_ANNOUNCE_NUMBER,
}


def is_select_message(msg_type: int) -> bool:
    return msg_type in SELECT_MSG_TYPES


# ---------------------------------------------------------------------------
# ctypes structures
# ---------------------------------------------------------------------------
class OCG_CardData(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_uint32),
        ("alias", ctypes.c_uint32),
        ("setcodes", ctypes.POINTER(ctypes.c_uint16)),
        ("type", ctypes.c_uint32),
        ("level", ctypes.c_uint32),
        ("attribute", ctypes.c_uint32),
        ("race", ctypes.c_uint64),
        ("attack", ctypes.c_int32),
        ("defense", ctypes.c_int32),
        ("lscale", ctypes.c_uint32),
        ("rscale", ctypes.c_uint32),
        ("link_marker", ctypes.c_uint32),
    ]


class OCG_Player(ctypes.Structure):
    _fields_ = [
        ("startingLP", ctypes.c_uint32),
        ("startingDrawCount", ctypes.c_uint32),
        ("drawCountPerTurn", ctypes.c_uint32),
    ]


OCG_DataReader = ctypes.CFUNCTYPE(
    None, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(OCG_CardData)
)
OCG_DataReaderDone = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.POINTER(OCG_CardData))
OCG_ScriptReader = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p)
OCG_LogHandler = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int)


class OCG_DuelOptions(ctypes.Structure):
    _fields_ = [
        ("seed0", ctypes.c_uint64),
        ("seed1", ctypes.c_uint64),
        ("seed2", ctypes.c_uint64),
        ("seed3", ctypes.c_uint64),
        ("flags", ctypes.c_uint64),
        ("team1", OCG_Player),
        ("team2", OCG_Player),
        ("cardReader", OCG_DataReader),
        ("payload1", ctypes.c_void_p),
        ("scriptReader", OCG_ScriptReader),
        ("payload2", ctypes.c_void_p),
        ("logHandler", OCG_LogHandler),
        ("payload3", ctypes.c_void_p),
        ("cardReaderDone", OCG_DataReaderDone),
        ("payload4", ctypes.c_void_p),
        ("enableUnsafeLibraries", ctypes.c_uint8),
    ]


class OCG_NewCardInfo(ctypes.Structure):
    _fields_ = [
        ("team", ctypes.c_uint8),
        ("duelist", ctypes.c_uint8),
        ("code", ctypes.c_uint32),
        ("con", ctypes.c_uint8),
        ("loc", ctypes.c_uint32),
        ("seq", ctypes.c_uint32),
        ("pos", ctypes.c_uint32),
    ]


class OCG_QueryInfo(ctypes.Structure):
    # u32 flags, u8 con, u32 loc, u32 seq, u32 overlay_seq
    _fields_ = [
        ("flags", ctypes.c_uint32),
        ("con", ctypes.c_uint8),
        ("loc", ctypes.c_uint32),
        ("seq", ctypes.c_uint32),
        ("overlay_seq", ctypes.c_uint32),
    ]


# ---------------------------------------------------------------------------
# Card database
# ---------------------------------------------------------------------------
class CardDB:
    """Reads card data from SQLite .cdb files."""

    def __init__(self, db_dir: Path):
        self._cache: dict[int, dict] = {}
        self._setcode_cache: dict[int, list[int]] = {}
        self._dbs: list[Path] = sorted(db_dir.glob("*.cdb"))
        for db_path in self._dbs:
            try:
                conn = sqlite3.connect(str(db_path))
                rows = conn.execute(
                    "SELECT id, alias, type, level, attribute, race, atk, def, setcode FROM datas"
                ).fetchall()
                for row in rows:
                    card_id = row[0]
                    setcode_raw = row[8]
                    setcodes = []
                    sc = setcode_raw
                    for _ in range(4):
                        s = sc & 0xFFFF
                        if s:
                            setcodes.append(s)
                        sc >>= 16
                    self._cache[card_id] = {
                        "code": card_id,
                        "alias": row[1],
                        "type": row[2],
                        "level": row[3],
                        "attribute": row[4],
                        "race": row[5],
                        "attack": row[6],
                        "defense": row[7],
                    }
                    self._setcode_cache[card_id] = setcodes
                try:
                    rows2 = conn.execute(
                        "SELECT id, lscale, rscale FROM datas WHERE lscale > 0 OR rscale > 0"
                    ).fetchall()
                    for row in rows2:
                        if row[0] in self._cache:
                            self._cache[row[0]]["lscale"] = row[1]
                            self._cache[row[0]]["rscale"] = row[2]
                except Exception:
                    pass
                try:
                    cols = "id,name,desc," + ",".join(f"str{i}" for i in range(1, 17))
                    name_rows = conn.execute(f"SELECT {cols} FROM texts").fetchall()
                    for row in name_rows:
                        if row[0] not in self._cache:
                            continue
                        entry = self._cache[row[0]]
                        if row[1]:
                            entry["name"] = row[1]
                        if row[2]:
                            entry["desc"] = row[2]
                        strs = [row[3 + i] or "" for i in range(16)]
                        if any(strs):
                            entry["strings"] = strs
                except Exception:
                    pass
                conn.close()
            except Exception as e:
                print(f"Warning: could not load {db_path}: {e}", file=sys.stderr)

    def get(self, code: int) -> dict | None:
        return self._cache.get(code)

    def get_setcodes(self, code: int) -> list[int]:
        return self._setcode_cache.get(code, [])


# ---------------------------------------------------------------------------
# Query response parsing
# ---------------------------------------------------------------------------
_QUERY_FIXED_SIZES = {
    QUERY_CODE: 4,
    QUERY_POSITION: 4,
    QUERY_ALIAS: 4,
    QUERY_TYPE: 4,
    QUERY_LEVEL: 4,
    QUERY_RANK: 4,
    QUERY_ATTRIBUTE: 4,
    QUERY_RACE: 8,
    QUERY_ATTACK: 4,
    QUERY_DEFENSE: 4,
    QUERY_BASE_ATTACK: 4,
    QUERY_BASE_DEFENSE: 4,
    QUERY_REASON: 4,
    QUERY_OWNER: 1,
    QUERY_STATUS: 4,
    QUERY_IS_PUBLIC: 1,
    QUERY_LSCALE: 4,
    QUERY_RSCALE: 4,
    QUERY_IS_HIDDEN: 1,
    QUERY_COVER: 4,
}
_QUERY_FIELD_NAMES = {
    QUERY_CODE: "code",
    QUERY_POSITION: "position",
    QUERY_ALIAS: "alias",
    QUERY_TYPE: "type",
    QUERY_LEVEL: "level",
    QUERY_RANK: "rank",
    QUERY_ATTRIBUTE: "attribute",
    QUERY_RACE: "race",
    QUERY_ATTACK: "attack",
    QUERY_DEFENSE: "defense",
    QUERY_BASE_ATTACK: "base_attack",
    QUERY_BASE_DEFENSE: "base_defense",
    QUERY_REASON: "reason",
    QUERY_OWNER: "owner",
    QUERY_STATUS: "status",
    QUERY_IS_PUBLIC: "is_public",
    QUERY_LSCALE: "lscale",
    QUERY_RSCALE: "rscale",
    QUERY_IS_HIDDEN: "is_hidden",
    QUERY_COVER: "cover",
}


def _parse_query_response(buf: bytes, offset: int = 0) -> dict:
    """Parse one card's QUERY response. Returns dict of fields."""
    result: dict = {}
    end = len(buf)
    i = offset
    if end - i < 2:
        return result
    while i + 6 <= end:
        size = int.from_bytes(buf[i : i + 2], "little", signed=False)
        flag = int.from_bytes(buf[i + 2 : i + 6], "little", signed=False)
        if flag == QUERY_END:
            result["_end_offset"] = i + 6
            return result
        payload = buf[i + 6 : i + 2 + size]
        name = _QUERY_FIELD_NAMES.get(flag)
        if flag in _QUERY_FIXED_SIZES:
            sz = _QUERY_FIXED_SIZES[flag]
            signed = flag in (QUERY_ATTACK, QUERY_DEFENSE, QUERY_BASE_ATTACK, QUERY_BASE_DEFENSE)
            val = int.from_bytes(payload[:sz], "little", signed=signed)
            if name:
                result[name] = val
            else:
                result[f"flag_{flag:x}"] = val
        elif flag == QUERY_REASON_CARD or flag == QUERY_EQUIP_CARD:
            con = payload[0]
            loc = payload[1]
            seq = int.from_bytes(payload[2:6], "little")
            pos = int.from_bytes(payload[6:10], "little")
            key = "reason_card" if flag == QUERY_REASON_CARD else "equip_card"
            result[key] = {"con": con, "loc": loc, "seq": seq, "pos": pos}
        elif flag == QUERY_TARGET_CARD:
            count = int.from_bytes(payload[:4], "little")
            targets = []
            off = 4
            for _ in range(count):
                t = payload[off : off + 10]
                targets.append(
                    {
                        "con": t[0],
                        "loc": t[1],
                        "seq": int.from_bytes(t[2:6], "little"),
                        "pos": int.from_bytes(t[6:10], "little"),
                    }
                )
                off += 10
            result["targets"] = targets
        elif flag == QUERY_OVERLAY_CARD:
            count = int.from_bytes(payload[:4], "little")
            codes = [int.from_bytes(payload[4 + 4 * j : 8 + 4 * j], "little") for j in range(count)]
            result["overlay"] = codes
        elif flag == QUERY_COUNTERS:
            count = int.from_bytes(payload[:4], "little")
            counters = {}
            for j in range(count):
                packed = int.from_bytes(payload[4 + 4 * j : 8 + 4 * j], "little")
                ctype = packed & 0xFFFF
                ccount = (packed >> 16) & 0xFFFF
                counters[ctype] = ccount
            result["counters"] = counters
        elif flag == QUERY_LINK:
            link = int.from_bytes(payload[:4], "little")
            markers = int.from_bytes(payload[4:8], "little")
            result["link"] = link
            result["link_markers"] = markers
        else:
            result[f"flag_{flag:x}"] = bytes(payload[: size - 4])
        i += 2 + size
    return result


def _parse_query_location_response(buf: bytes) -> list[dict | None]:
    if len(buf) < 4:
        return []
    total_size = int.from_bytes(buf[:4], "little")
    end = min(4 + total_size, len(buf))
    out: list[dict | None] = []
    i = 4
    while i < end:
        if end - i < 2:
            break
        peek = int.from_bytes(buf[i : i + 2], "little", signed=True)
        if peek == 0:
            out.append(None)
            i += 2
            continue
        card = _parse_query_response(buf, offset=i)
        next_i = card.pop("_end_offset", None)
        if next_i is None or next_i <= i:
            break
        out.append(card)
        i = next_i
    return out


def _parse_query_field_response(buf: bytes) -> dict:
    if len(buf) < 4:
        return {}
    r = MessageReader(buf)
    duel_options = r.read_u32()
    players = []
    for _ in range(2):
        lp = r.read_u32()
        mzone = []
        for _ in range(MZONE_SLOTS):
            has = r.read_u8()
            if has:
                pos = r.read_u8()
                overlay_count = r.read_u32()
                mzone.append({"has_card": True, "position": pos, "overlay_count": overlay_count})
            else:
                mzone.append(None)
        szone = []
        for _ in range(SZONE_SLOTS):
            has = r.read_u8()
            if has:
                pos = r.read_u8()
                overlay_count = r.read_u32()
                szone.append({"has_card": True, "position": pos, "overlay_count": overlay_count})
            else:
                szone.append(None)
        deck_count = r.read_u32()
        hand_count = r.read_u32()
        grave_count = r.read_u32()
        removed_count = r.read_u32()
        extra_count = r.read_u32()
        extra_p_count = r.read_u32()
        players.append(
            {
                "lp": lp,
                "mzone_summary": mzone,
                "szone_summary": szone,
                "deck_count": deck_count,
                "hand_count_raw": hand_count,
                "grave_count_raw": grave_count,
                "removed_count_raw": removed_count,
                "extra_count_raw": extra_count,
                "extra_p_count": extra_p_count,
            }
        )
    chain_count = r.read_u32() if r.remaining() >= 4 else 0
    chain = []
    for _ in range(chain_count):
        if r.remaining() < 26:
            break
        code = r.read_u32()
        info_con = r.read_u8()
        info_loc = r.read_u8()
        info_seq = r.read_u32()
        info_pos = r.read_u32()
        trg_con = r.read_u8()
        trg_loc = r.read_u8()
        trg_seq = r.read_u32()
        desc = r.read_u64()
        chain.append(
            {
                "code": code,
                "effect_location": {
                    "con": info_con,
                    "loc": info_loc,
                    "seq": info_seq,
                    "pos": info_pos,
                },
                "trigger_location": {"con": trg_con, "loc": trg_loc, "seq": trg_seq},
                "description": desc,
            }
        )
    return {"duel_options": duel_options, "players": players, "chain": chain}


# ---------------------------------------------------------------------------
# Observation rendering
# ---------------------------------------------------------------------------
_POSITION_STRS = {
    POS_FACEUP_ATTACK: "face_up_attack",
    POS_FACEDOWN_ATTACK: "face_down_attack",
    POS_FACEUP_DEFENSE: "face_up_defense",
    POS_FACEDOWN_DEFENSE: "face_down_defense",
}

_PHASE_STRS = {
    PHASE_DRAW: "draw",
    PHASE_STANDBY: "standby",
    PHASE_MAIN1: "main1",
    PHASE_BATTLE_START: "battle_start",
    PHASE_BATTLE_STEP: "battle_step",
    PHASE_DAMAGE: "damage",
    PHASE_DAMAGE_CAL: "damage_calculation",
    PHASE_BATTLE: "battle",
    PHASE_MAIN2: "main2",
    PHASE_END: "end",
}

_LOCATION_STRS = {
    LOCATION_DECK: "deck",
    LOCATION_HAND: "hand",
    LOCATION_MZONE: "monster_zone",
    LOCATION_SZONE: "spell_zone",
    LOCATION_GRAVE: "graveyard",
    LOCATION_REMOVED: "banished",
    LOCATION_EXTRA: "extra_deck",
    LOCATION_FZONE: "field_zone",
    LOCATION_PZONE: "pendulum_zone",
}

_STATUS_BITS = [
    (STATUS_DISABLED, "disabled"),
    (STATUS_EFFECT_ENABLED, "effect_enabled"),
    (STATUS_SUMMON_TURN, "summoned_this_turn"),
    (STATUS_SPSUMMON_TURN, "spsummoned_this_turn"),
    (STATUS_FLIP_SUMMON_TURN, "flip_summoned_this_turn"),
    (STATUS_CHAINING, "chaining"),
    (STATUS_ACTIVATE_DISABLED, "activate_disabled"),
    (STATUS_ATTACK_CANCELED, "attack_canceled"),
    (STATUS_JUST_POS, "just_changed_position"),
    (STATUS_BATTLE_RESULT, "battle_result"),
]

_TYPE_BITS = [
    (0x1, "monster"),
    (0x2, "spell"),
    (0x4, "trap"),
    (0x10, "normal"),
    (0x20, "effect"),
    (0x40, "fusion"),
    (0x80, "ritual"),
    (0x100, "trap_monster"),
    (0x200, "spirit"),
    (0x400, "union"),
    (0x800, "gemini"),
    (0x1000, "tuner"),
    (0x2000, "synchro"),
    (0x4000, "token"),
    (0x8000, "maximum"),
    (0x10000, "quick_play"),
    (0x20000, "continuous"),
    (0x40000, "equip"),
    (0x80000, "field"),
    (0x100000, "counter"),
    (0x200000, "flip"),
    (0x400000, "toon"),
    (0x800000, "xyz"),
    (0x1000000, "pendulum"),
    (0x2000000, "spsummon"),
    (0x4000000, "link"),
]

# ocgapi_constants.h:61-67
_ATTRIBUTE_STRS = {
    0x01: "EARTH",
    0x02: "WATER",
    0x04: "FIRE",
    0x08: "WIND",
    0x10: "LIGHT",
    0x20: "DARK",
    0x40: "DIVINE",
}

# ocgapi_constants.h:71-104 — full OCG race list incl. newer races.
_RACE_STRS = {
    0x1: "WARRIOR",
    0x2: "SPELLCASTER",
    0x4: "FAIRY",
    0x8: "FIEND",
    0x10: "ZOMBIE",
    0x20: "MACHINE",
    0x40: "AQUA",
    0x80: "PYRO",
    0x100: "ROCK",
    0x200: "WINGED_BEAST",
    0x400: "PLANT",
    0x800: "INSECT",
    0x1000: "THUNDER",
    0x2000: "DRAGON",
    0x4000: "BEAST",
    0x8000: "BEAST_WARRIOR",
    0x10000: "DINOSAUR",
    0x20000: "FISH",
    0x40000: "SEA_SERPENT",
    0x80000: "REPTILE",
    0x100000: "PSYCHIC",
    0x200000: "DIVINE_BEAST",
    0x400000: "CREATOR_GOD",
    0x800000: "WYRM",
    0x1000000: "CYBERSE",
    0x2000000: "ILLUSION",
    0x4000000: "CYBORG",
    0x8000000: "MAGICAL_KNIGHT",
    0x10000000: "HIGH_DRAGON",
    0x20000000: "OMEGA_PSYCHIC",
    0x40000000: "CELESTIAL_WARRIOR",
    0x80000000: "GALAXY",
    0x4000000000000000: "YOKAI",
}

# ocgapi_constants.h:108-133
_REASON_BITS = [
    (0x1, "destroy"),
    (0x2, "release"),
    (0x4, "temporary"),
    (0x8, "material"),
    (0x10, "summon"),
    (0x20, "battle"),
    (0x40, "effect"),
    (0x80, "cost"),
    (0x100, "adjust"),
    (0x200, "lost_target"),
    (0x400, "rule"),
    (0x800, "spsummon"),
    (0x1000, "dissummon"),
    (0x2000, "flip"),
    (0x4000, "discard"),
    (0x8000, "reflected_damage"),
    (0x10000, "reflected_recover"),
    (0x20000, "return"),
    (0x40000, "fusion"),
    (0x80000, "synchro"),
    (0x100000, "ritual"),
    (0x200000, "xyz"),
    (0x1000000, "replace"),
    (0x2000000, "draw"),
    (0x4000000, "redirect"),
    (0x10000000, "link"),
]

# ocgapi_constants.h:197-204 (octal literals in C).
_LINK_MARKER_BITS = [
    (0o001, "bottom_left"),
    (0o002, "bottom"),
    (0o004, "bottom_right"),
    (0o010, "left"),
    (0o040, "right"),
    (0o100, "top_left"),
    (0o200, "top"),
    (0o400, "top_right"),
]

# processor.cpp:4656-4692 — the engine's built-in win-check codes.
# Additional codes (3+) can be set by card effects via libduel.cpp:1123
# (e.g. Exodia, Final Countdown) and are script-specific.
_WIN_REASON_STRS = {
    0: "effect_win",  # set by a card effect (code > 2 also routes here)
    1: "lp_zero",  # a player reached 0 LP
    2: "deck_out",  # a player tried to draw from an empty deck
}


def render_position(pos: int) -> str:
    return _POSITION_STRS.get(pos, f"pos_0x{pos:x}")


def render_attribute(mask: int) -> list[str]:
    """One card can only have one attribute but AnnounceAttribute uses a
    mask of several — return a list in both cases."""
    return [name for bit, name in _ATTRIBUTE_STRS.items() if mask & bit]


def render_race(mask: int) -> list[str]:
    return [name for bit, name in _RACE_STRS.items() if mask & bit]


def render_reason(mask: int) -> list[str]:
    return [name for bit, name in _REASON_BITS if mask & bit]


def render_link_markers(mask: int) -> list[str]:
    return [name for bit, name in _LINK_MARKER_BITS if mask & bit]


def render_win_reason(reason: int) -> str:
    # Codes > 2 are script-set effect-win reasons (see libduel.cpp:1123).
    if reason in _WIN_REASON_STRS:
        return _WIN_REASON_STRS[reason]
    return f"effect_win(code={reason})"


def glossary() -> dict:
    """Return every enum the LLM might see on the wire, as {code → name}.

    Includes hex form and (for masks) a "how to read" note so the LLM has
    zero reason to consult the C++ source.  Expose via the ``get_glossary``
    inspection tool.
    """
    return {
        "note": (
            "All game-state integers you see in observations decode via "
            "one of the tables below.  Single-bit codes (position, "
            "attribute, race, location, phase, win_reason) map one code "
            "→ one string.  Multi-bit masks (type, status, reason, "
            "link_markers) may have several bits set; the renderer "
            "gives you a list of names in that case."
        ),
        "position": {f"0x{k:x}": v for k, v in _POSITION_STRS.items()},
        "location": {f"0x{k:x}": v for k, v in _LOCATION_STRS.items()},
        "phase": {f"0x{k:x}": v for k, v in _PHASE_STRS.items()},
        "attribute": {f"0x{k:x}": v for k, v in _ATTRIBUTE_STRS.items()},
        "race": {f"0x{k:x}": v for k, v in _RACE_STRS.items()},
        "type_flags": [{"bit": f"0x{b:x}", "name": n} for b, n in _TYPE_BITS],
        "status_flags": [{"bit": f"0x{b:x}", "name": n} for b, n in _STATUS_BITS],
        "reason_flags": [{"bit": f"0x{b:x}", "name": n} for b, n in _REASON_BITS],
        "link_markers": [{"bit": f"0x{b:x}", "name": n} for b, n in _LINK_MARKER_BITS],
        "win_reason": {f"0x{k:x}": v for k, v in _WIN_REASON_STRS.items()},
    }


def render_phase(phase: int) -> str:
    return _PHASE_STRS.get(phase, f"phase_0x{phase:x}")


def render_location(loc: int) -> str:
    return _LOCATION_STRS.get(loc, f"loc_0x{loc:x}")


def render_status(status: int) -> list[str]:
    return [name for bit, name in _STATUS_BITS if status & bit]


def render_type(type_flags: int) -> list[str]:
    return [name for bit, name in _TYPE_BITS if type_flags & bit]


def resolve_desc_id(strcode: int, card_db: CardDB, compat: bool = True) -> dict:
    """Resolve a description ID into a human-readable entry.

    compat=True matches the legacy EDOPro encoding:
      - strcode < 10000 ⇒ system string
      - otherwise: code = strcode >> 4, stringid = strcode & 0xf
    """
    if compat:
        if strcode < 10000:
            return {
                "strcode": strcode,
                "code": 0,
                "stringid": strcode,
                "text": None,
                "kind": "system",
            }
        code = strcode >> 4
        stringid = strcode & 0xF
    else:
        code = strcode >> 20
        stringid = strcode & 0xFFFFF
    if code == 0:
        return {"strcode": strcode, "code": 0, "stringid": stringid, "text": None, "kind": "system"}
    info = card_db.get(code) or {}
    strs = info.get("strings") or []
    text = strs[stringid] if 0 <= stringid < len(strs) and strs[stringid] else None
    return {
        "strcode": strcode,
        "code": code,
        "stringid": stringid,
        "text": text,
        "card_name": info.get("name"),
        "kind": "card",
    }


def _is_face_up(pos: int) -> bool:
    return bool(pos & (POS_FACEUP_ATTACK | POS_FACEUP_DEFENSE))


def _code_visible(raw: dict, zone: str, owner: int, perspective: int) -> bool:
    if raw.get("is_public"):
        return True
    if owner == perspective:
        return True
    if zone in ("hand", "deck", "extra_deck"):
        return False
    if zone in ("monster_zone", "spell_zone", "field_zone", "pendulum_zone", "banished"):
        return _is_face_up(raw.get("position", 0))
    if zone == "graveyard":
        return True
    return True


def render_card(
    raw: dict,
    zone: str,
    owner: int,
    perspective: int,
    card_db: CardDB,
) -> dict:
    visible = _code_visible(raw, zone, owner, perspective)
    pos = raw.get("position", 0)
    out: dict = {"owner": "you" if owner == perspective else "opponent"}
    if pos:
        out["position"] = render_position(pos)
    if visible:
        code = raw.get("code", 0)
        if code:
            out["code"] = code
            info = card_db.get(code) or {}
            if info.get("name"):
                out["name"] = info["name"]
            else:
                out["name"] = f"Card#{code}"
            if info.get("desc"):
                out["desc"] = info["desc"]
        for k in (
            "attack",
            "defense",
            "base_attack",
            "base_defense",
            "level",
            "rank",
            "lscale",
            "rscale",
            "link",
        ):
            if k in raw:
                out[k] = raw[k]
        if "attribute" in raw:
            attrs = render_attribute(raw["attribute"])
            out["attribute"] = attrs[0] if len(attrs) == 1 else attrs
        if "race" in raw:
            races = render_race(raw["race"])
            out["race"] = races[0] if len(races) == 1 else races
        if "link_markers" in raw:
            out["link_markers"] = render_link_markers(raw["link_markers"])
        if "type" in raw:
            out["type_flags"] = render_type(raw["type"])
        if "status" in raw:
            flags = render_status(raw["status"])
            if flags:
                out["status_flags"] = flags
        if "reason" in raw:
            reasons = render_reason(raw["reason"])
            if reasons:
                out["reason_flags"] = reasons
        if raw.get("counters"):
            out["counters"] = raw["counters"]
        if raw.get("overlay"):
            out["overlay"] = [
                {"code": c, "name": (card_db.get(c) or {}).get("name", f"Card#{c}")}
                for c in raw["overlay"]
            ]
    else:
        out["face_down"] = True
    return out


def build_observation(
    snapshot: dict,
    perspective: int,
    card_db: CardDB,
    *,
    phase: int | str | None = None,
    turn_player: int | None = None,
    turn_count: int | None = None,
    prompt: dict | None = None,
) -> dict:
    """Render a snapshot into a perspective-filtered observation.

    SZONE is split into spell_trap_zone (0-4), field_zone (5), and
    pendulum_zone (6-7) to match the mental model of the game.
    """
    if perspective not in (0, 1):
        raise ValueError(f"perspective must be 0 or 1, got {perspective}")

    players = snapshot["players"]

    def render_side(owner: int) -> dict:
        p = players[owner]
        side: dict = {
            "lp": p.get("lp", 0),
            "deck_count": p.get("deck_count", 0),
            "hand_count": p.get("hand_count", 0),
            "grave_count": p.get("grave_count", 0),
            "banished_count": p.get("removed_count", 0),
            "extra_deck_count": p.get("extra_count", 0),
            "extra_pendulum_count": p.get("extra_p_count", 0),
        }

        mzone: list = []
        for i, card in enumerate(p.get("mzone", [])):
            if card is None:
                mzone.append({"zone_index": i, "empty": True})
            else:
                mzone.append(
                    {
                        "zone_index": i,
                        **render_card(card, "monster_zone", owner, perspective, card_db),
                    }
                )
        side["monster_zone"] = mzone

        szone_raw = p.get("szone", [])
        st_zone: list = []
        for i in range(5):
            card = szone_raw[i] if i < len(szone_raw) else None
            if card is None:
                st_zone.append({"zone_index": i, "empty": True})
            else:
                st_zone.append(
                    {
                        "zone_index": i,
                        **render_card(card, "spell_zone", owner, perspective, card_db),
                    }
                )
        side["spell_trap_zone"] = st_zone

        field_raw = szone_raw[5] if len(szone_raw) > 5 else None
        side["field_zone"] = (
            None
            if field_raw is None
            else render_card(field_raw, "field_zone", owner, perspective, card_db)
        )

        pend = []
        for i in (6, 7):
            card = szone_raw[i] if i < len(szone_raw) else None
            pend.append(
                {"zone_index": i, "empty": True}
                if card is None
                else {
                    "zone_index": i,
                    **render_card(card, "pendulum_zone", owner, perspective, card_db),
                }
            )
        side["pendulum_zone"] = pend

        side["hand"] = [
            render_card(c, "hand", owner, perspective, card_db) for c in p.get("hand", [])
        ]
        side["graveyard"] = [
            render_card(c, "graveyard", owner, perspective, card_db) for c in p.get("grave", [])
        ]
        side["banished"] = [
            render_card(c, "banished", owner, perspective, card_db) for c in p.get("removed", [])
        ]
        side["extra_deck"] = [
            render_card(c, "extra_deck", owner, perspective, card_db) for c in p.get("extra", [])
        ]
        return side

    chain = []
    for link in snapshot.get("chain", []):
        code = link.get("code", 0)
        info = (card_db.get(code) or {}) if code else {}
        entry = {
            "code": code,
            "name": info.get("name") or (f"Card#{code}" if code else None),
            "effect_location": link.get("effect_location"),
            "trigger_location": link.get("trigger_location"),
        }
        if info.get("desc"):
            entry["desc"] = info["desc"]
        chain.append(entry)

    obs: dict = {
        "perspective_player": perspective,
        "you": render_side(perspective),
        "opponent": render_side(1 - perspective),
        "chain": chain,
    }
    if phase is not None:
        obs["phase"] = render_phase(phase) if isinstance(phase, int) else phase
    if turn_player is not None:
        obs["turn_player"] = "you" if turn_player == perspective else "opponent"
    if turn_count is not None:
        obs["turn"] = turn_count
    if prompt is not None:
        obs["prompt"] = prompt
    return obs


# ---------------------------------------------------------------------------
# Binary message reader
# ---------------------------------------------------------------------------
class MessageReader:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0

    def remaining(self) -> int:
        return len(self.data) - self.pos

    def read_u8(self) -> int:
        val = self.data[self.pos]
        self.pos += 1
        return val

    def read_i8(self) -> int:
        val = struct.unpack_from("<b", self.data, self.pos)[0]
        self.pos += 1
        return val

    def read_u16(self) -> int:
        val = struct.unpack_from("<H", self.data, self.pos)[0]
        self.pos += 2
        return val

    def read_i16(self) -> int:
        val = struct.unpack_from("<h", self.data, self.pos)[0]
        self.pos += 2
        return val

    def read_u32(self) -> int:
        val = struct.unpack_from("<I", self.data, self.pos)[0]
        self.pos += 4
        return val

    def read_i32(self) -> int:
        val = struct.unpack_from("<i", self.data, self.pos)[0]
        self.pos += 4
        return val

    def read_u64(self) -> int:
        val = struct.unpack_from("<Q", self.data, self.pos)[0]
        self.pos += 8
        return val

    def read_i64(self) -> int:
        val = struct.unpack_from("<q", self.data, self.pos)[0]
        self.pos += 8
        return val

    def read_bytes(self, n: int) -> bytes:
        val = self.data[self.pos : self.pos + n]
        self.pos += n
        return val

    def skip(self, n: int):
        self.pos += n


# ---------------------------------------------------------------------------
# SELECT message payloads
# ---------------------------------------------------------------------------
@dataclass
class IdleCmdOption:
    category: int  # 0=summon,1=spsummon,2=repos,3=mset,4=sset,5=activate
    index: int
    code: int
    con: int
    loc: int
    seq: int
    desc: int = 0

    CATEGORY_NAMES = {
        0: "summon",
        1: "sp_summon",
        2: "repos",
        3: "set_monster",
        4: "set_spell",
        5: "activate",
    }


@dataclass
class BattleCmdOption:
    category: int  # 0=activate, 1=attack
    index: int
    code: int
    con: int
    loc: int
    seq: int
    desc: int = 0
    direct_attackable: int = 0


@dataclass
class IdleCmd:
    player: int
    options: list[IdleCmdOption]
    can_battle_phase: bool
    can_end_phase: bool
    can_shuffle: bool


@dataclass
class BattleCmd:
    player: int
    options: list[BattleCmdOption]
    can_main2: bool
    can_end_phase: bool


@dataclass
class SelectCard:
    player: int
    cancelable: bool
    min_: int
    max_: int
    cards: list[dict]
    is_tribute: bool = False


@dataclass
class SelectChain:
    player: int
    forced: bool
    cards: list[dict]


@dataclass
class SelectEffectYn:
    player: int
    code: int
    con: int
    loc: int
    seq: int
    pos: int
    desc: int


@dataclass
class SelectYesNo:
    player: int
    desc: int


@dataclass
class SelectOption:
    player: int
    options: list[int]


@dataclass
class SelectPosition:
    player: int
    code: int
    positions: int  # bitmask


@dataclass
class SelectPlace:
    player: int
    min_: int
    field_mask: int  # raw (inverted) field value


@dataclass
class SelectSum:
    player: int
    mode: int
    sumval: int
    min_: int
    max_: int
    mandatory_cards: list[dict]
    optional_cards: list[dict]


@dataclass
class SelectUnselectCard:
    player: int
    finishable: bool
    cancelable: bool
    min_: int
    max_: int
    selectable_cards: list[dict]
    selected_cards: list[dict]


@dataclass
class SelectCounter:
    player: int
    counter_type: int
    count: int
    cards: list[dict]


@dataclass
class SortCard:
    player: int
    cards: list[dict]


@dataclass
class AnnounceRace:
    player: int
    count: int
    available: int  # bitmask of races


@dataclass
class AnnounceAttrib:
    player: int
    count: int
    available: int  # bitmask of attributes


@dataclass
class AnnounceCard:
    player: int
    opcodes: list[int]


@dataclass
class AnnounceNumber:
    player: int
    numbers: list[int]


# ---------------------------------------------------------------------------
# OCG engine wrapper
# ---------------------------------------------------------------------------
class OCGEngine:
    """Wraps libocgcore for headless duel simulation."""

    def __init__(
        self,
        dylib_path: Path,
        card_db: CardDB,
        script_dir: Path,
        card_script_dir: Path,
        verbose: bool = False,
    ):
        self.lib = ctypes.CDLL(str(dylib_path))
        self.card_db = card_db
        self.script_dir = script_dir
        self.card_script_dir = card_script_dir
        self.verbose = verbose
        self.duel = None
        self._log_messages: list[str] = []

        self._card_reader_cb = None
        self._card_reader_done_cb = None
        self._script_reader_cb = None
        self._log_handler_cb = None
        self._setcode_arrays: list = []

        self._setup_api()

    def _setup_api(self):
        lib = self.lib

        lib.OCG_GetVersion.argtypes = [ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int)]
        lib.OCG_GetVersion.restype = None

        lib.OCG_CreateDuel.argtypes = [
            ctypes.POINTER(ctypes.c_void_p),
            ctypes.POINTER(OCG_DuelOptions),
        ]
        lib.OCG_CreateDuel.restype = ctypes.c_int

        lib.OCG_DestroyDuel.argtypes = [ctypes.c_void_p]
        lib.OCG_DestroyDuel.restype = None

        lib.OCG_DuelNewCard.argtypes = [ctypes.c_void_p, ctypes.POINTER(OCG_NewCardInfo)]
        lib.OCG_DuelNewCard.restype = None

        lib.OCG_StartDuel.argtypes = [ctypes.c_void_p]
        lib.OCG_StartDuel.restype = None

        lib.OCG_DuelProcess.argtypes = [ctypes.c_void_p]
        lib.OCG_DuelProcess.restype = ctypes.c_int

        lib.OCG_DuelGetMessage.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        lib.OCG_DuelGetMessage.restype = ctypes.c_void_p

        lib.OCG_DuelSetResponse.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32]
        lib.OCG_DuelSetResponse.restype = None

        lib.OCG_LoadScript.argtypes = [
            ctypes.c_void_p,
            ctypes.c_char_p,
            ctypes.c_uint32,
            ctypes.c_char_p,
        ]
        lib.OCG_LoadScript.restype = ctypes.c_int

        lib.OCG_DuelQueryCount.argtypes = [ctypes.c_void_p, ctypes.c_uint8, ctypes.c_uint32]
        lib.OCG_DuelQueryCount.restype = ctypes.c_uint32

        lib.OCG_DuelQuery.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(OCG_QueryInfo),
        ]
        lib.OCG_DuelQuery.restype = ctypes.c_void_p

        lib.OCG_DuelQueryLocation.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_uint32),
            ctypes.POINTER(OCG_QueryInfo),
        ]
        lib.OCG_DuelQueryLocation.restype = ctypes.c_void_p

        lib.OCG_DuelQueryField.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
        lib.OCG_DuelQueryField.restype = ctypes.c_void_p

    def _make_card_reader(self):
        db = self.card_db

        @OCG_DataReader
        def card_reader(payload, code, data_ptr):
            info = db.get(code)
            data = data_ptr[0]
            if info is None:
                data.code = code
                data.alias = 0
                sc_arr = (ctypes.c_uint16 * 1)(0)
                self._setcode_arrays.append(sc_arr)
                data.setcodes = ctypes.cast(sc_arr, ctypes.POINTER(ctypes.c_uint16))
                data.type = 0
                data.level = 0
                data.attribute = 0
                data.race = 0
                data.attack = 0
                data.defense = 0
                data.lscale = 0
                data.rscale = 0
                data.link_marker = 0
                return
            data.code = code
            data.alias = info["alias"]
            setcodes = db.get_setcodes(code)
            sc_arr = (ctypes.c_uint16 * (len(setcodes) + 1))()
            for i, sc in enumerate(setcodes):
                sc_arr[i] = sc
            sc_arr[len(setcodes)] = 0
            self._setcode_arrays.append(sc_arr)
            data.setcodes = ctypes.cast(sc_arr, ctypes.POINTER(ctypes.c_uint16))
            data.type = info["type"]
            data.level = info["level"]
            data.attribute = info["attribute"]
            data.race = info["race"]
            data.attack = info["attack"]
            data.defense = info["defense"]
            data.lscale = info.get("lscale", 0)
            data.rscale = info.get("rscale", 0)
            data.link_marker = 0

        self._card_reader_cb = card_reader
        return card_reader

    def _make_card_reader_done(self):
        @OCG_DataReaderDone
        def card_reader_done(payload, data_ptr):
            pass

        self._card_reader_done_cb = card_reader_done
        return card_reader_done

    def _make_script_reader(self):
        engine = self
        script_subdirs = [d.name for d in engine.script_dir.iterdir() if d.is_dir()]

        @OCG_ScriptReader
        def script_reader(payload, duel, name_bytes):
            name = name_bytes.decode("utf-8") if isinstance(name_bytes, bytes) else name_bytes
            script_path = engine.script_dir / name
            if script_path.exists():
                content = script_path.read_bytes()
                engine.lib.OCG_LoadScript(duel, content, len(content), name_bytes)
                return 1
            for subdir in script_subdirs:
                candidate = engine.script_dir / subdir / name
                if candidate.exists():
                    content = candidate.read_bytes()
                    engine.lib.OCG_LoadScript(duel, content, len(content), name_bytes)
                    return 1
            return 0

        self._script_reader_cb = script_reader
        return script_reader

    def _make_log_handler(self):
        engine = self

        @OCG_LogHandler
        def log_handler(payload, msg_bytes, log_type):
            if msg_bytes:
                msg = msg_bytes.decode("utf-8", errors="replace")
                engine._log_messages.append(msg)
                if engine.verbose:
                    print(f"  [engine log] {msg}", file=sys.stderr)

        self._log_handler_cb = log_handler
        return log_handler

    def create_duel(self, flags: int = 0, lp0: int = 8000, lp1: int = 8000):
        opts = OCG_DuelOptions()
        opts.seed0 = 1
        opts.seed1 = 1
        opts.seed2 = 1
        opts.seed3 = 1
        opts.flags = flags
        opts.team1 = OCG_Player(startingLP=lp0, startingDrawCount=0, drawCountPerTurn=0)
        opts.team2 = OCG_Player(startingLP=lp1, startingDrawCount=0, drawCountPerTurn=0)
        opts.cardReader = self._make_card_reader()
        opts.payload1 = None
        opts.scriptReader = self._make_script_reader()
        opts.payload2 = None
        opts.logHandler = self._make_log_handler()
        opts.payload3 = None
        opts.cardReaderDone = self._make_card_reader_done()
        opts.payload4 = None
        opts.enableUnsafeLibraries = 0

        duel_ptr = ctypes.c_void_p()
        result = self.lib.OCG_CreateDuel(ctypes.byref(duel_ptr), ctypes.byref(opts))
        if result != 0:
            raise RuntimeError(f"OCG_CreateDuel failed with status {result}")
        self.duel = duel_ptr
        self._log_messages = []

        self._load_script("constant.lua")
        self._load_script("utility.lua")

    def _load_script(self, name: str):
        path = self.script_dir / name
        if not path.exists():
            raise FileNotFoundError(f"Script not found: {path}")
        content = path.read_bytes()
        name_bytes = name.encode("utf-8")
        result = self.lib.OCG_LoadScript(self.duel, content, len(content), name_bytes)
        if not result:
            raise RuntimeError(f"Failed to load script: {name}")

    def load_puzzle_script(self, lua_text: str, name: str = "puzzle.lua"):
        content = lua_text.encode("utf-8")
        name_bytes = name.encode("utf-8")
        result = self.lib.OCG_LoadScript(self.duel, content, len(content), name_bytes)
        if not result:
            raise RuntimeError("Failed to load puzzle script")

    def start_duel(self):
        self.lib.OCG_StartDuel(self.duel)

    def process(self, timeout: float = 10.0) -> int:
        """Runs ``OCG_DuelProcess`` on a background thread so a hung Lua
        script surfaces as ``TimeoutError`` rather than freezing the worker."""
        result = [None]
        exc = [None]

        def _run():
            try:
                result[0] = self.lib.OCG_DuelProcess(self.duel)
            except Exception as e:
                exc[0] = e

        t = threading.Thread(target=_run, daemon=True)
        t.start()
        t.join(timeout=timeout)
        if t.is_alive():
            raise TimeoutError(f"OCG_DuelProcess hung (>{timeout}s)")
        if exc[0]:
            raise exc[0]
        return result[0]

    def get_message(self) -> bytes:
        length = ctypes.c_uint32()
        ptr = self.lib.OCG_DuelGetMessage(self.duel, ctypes.byref(length))
        if length.value == 0 or not ptr:
            return b""
        return ctypes.string_at(ptr, length.value)

    def set_response_int(self, value: int):
        buf = struct.pack("<i", value)
        self.lib.OCG_DuelSetResponse(self.duel, buf, len(buf))

    def set_response_long(self, value: int):
        buf = struct.pack("<q", value)
        self.lib.OCG_DuelSetResponse(self.duel, buf, len(buf))

    def set_response_bytes(self, data: bytes):
        self.lib.OCG_DuelSetResponse(self.duel, data, len(data))

    # --- Query API ---
    # The core returns a pointer to an internal buffer that is cleared on
    # the next query/message call, so each method copies into Python bytes
    # before returning.

    def query_count(self, team: int, loc: int) -> int:
        return int(self.lib.OCG_DuelQueryCount(self.duel, team, loc))

    def query_card_raw(
        self, con: int, loc: int, seq: int, flags: int, overlay_seq: int = 0
    ) -> bytes:
        info = OCG_QueryInfo(flags=flags, con=con, loc=loc, seq=seq, overlay_seq=overlay_seq)
        length = ctypes.c_uint32(0)
        ptr = self.lib.OCG_DuelQuery(self.duel, ctypes.byref(length), ctypes.byref(info))
        if not ptr or length.value == 0:
            return b""
        return ctypes.string_at(ptr, length.value)

    def query_card(
        self, con: int, loc: int, seq: int, flags: int = QUERY_FULL_CARD, overlay_seq: int = 0
    ) -> dict | None:
        raw = self.query_card_raw(con, loc, seq, flags, overlay_seq)
        if not raw:
            return None
        parsed = _parse_query_response(raw)
        return parsed or None

    def query_location_raw(self, con: int, loc: int, flags: int) -> bytes:
        info = OCG_QueryInfo(flags=flags, con=con, loc=loc, seq=0, overlay_seq=0)
        length = ctypes.c_uint32(0)
        ptr = self.lib.OCG_DuelQueryLocation(self.duel, ctypes.byref(length), ctypes.byref(info))
        if not ptr or length.value == 0:
            return b""
        return ctypes.string_at(ptr, length.value)

    def query_location(self, con: int, loc: int, flags: int = QUERY_FULL_CARD) -> list[dict | None]:
        raw = self.query_location_raw(con, loc, flags)
        if not raw:
            return []
        return _parse_query_location_response(raw)

    def query_field_raw(self) -> bytes:
        length = ctypes.c_uint32(0)
        ptr = self.lib.OCG_DuelQueryField(self.duel, ctypes.byref(length))
        if not ptr or length.value == 0:
            return b""
        return ctypes.string_at(ptr, length.value)

    def query_field(self) -> dict:
        raw = self.query_field_raw()
        return _parse_query_field_response(raw) if raw else {}

    def get_snapshot(self, flags: int = QUERY_FULL_CARD, include_decks: bool = True) -> dict:
        field_snap = self.query_field()
        players = [dict(p) for p in field_snap.get("players", [])]

        for con in (0, 1):
            p = players[con]
            p["mzone"] = self.query_location(con, LOCATION_MZONE, flags)
            p["szone"] = self.query_location(con, LOCATION_SZONE, flags)
            if include_decks:
                p["hand"] = self.query_location(con, LOCATION_HAND, flags)
                p["grave"] = self.query_location(con, LOCATION_GRAVE, flags)
                p["removed"] = self.query_location(con, LOCATION_REMOVED, flags)
                p["extra"] = self.query_location(con, LOCATION_EXTRA, flags)
            p["hand_count"] = self.query_count(con, LOCATION_HAND)
            p["grave_count"] = self.query_count(con, LOCATION_GRAVE)
            p["removed_count"] = self.query_count(con, LOCATION_REMOVED)
            p["extra_count"] = self.query_count(con, LOCATION_EXTRA)

        return {
            "duel_options": field_snap.get("duel_options", 0),
            "chain": field_snap.get("chain", []),
            "players": players,
        }

    def destroy(self):
        if self.duel:
            self.lib.OCG_DestroyDuel(self.duel)
            self.duel = None

    # --- Message parsing ---

    def parse_messages(self, raw: bytes) -> list[tuple[int, object]]:
        messages: list[tuple[int, object]] = []
        reader = MessageReader(raw)
        while reader.remaining() > 0:
            msg_len = reader.read_u32()
            if msg_len == 0:
                break
            msg_data = reader.read_bytes(msg_len)
            msg_reader = MessageReader(msg_data)
            msg_type = msg_reader.read_u8()
            parsed = self._parse_single_message(msg_type, msg_reader)
            messages.append((msg_type, parsed))
        return messages

    def _parse_single_message(self, msg_type: int, r: MessageReader):
        if msg_type == MSG_SELECT_IDLECMD:
            return self._parse_idle_cmd(r)
        elif msg_type == MSG_SELECT_BATTLECMD:
            return self._parse_battle_cmd(r)
        elif msg_type == MSG_SELECT_CARD:
            return self._parse_select_card(r, is_tribute=False)
        elif msg_type == MSG_SELECT_TRIBUTE:
            return self._parse_select_card(r, is_tribute=True)
        elif msg_type == MSG_SELECT_CHAIN:
            return self._parse_select_chain(r)
        elif msg_type == MSG_SELECT_EFFECTYN:
            return self._parse_select_effect_yn(r)
        elif msg_type == MSG_SELECT_YESNO:
            return self._parse_select_yesno(r)
        elif msg_type == MSG_SELECT_OPTION:
            return self._parse_select_option(r)
        elif msg_type == MSG_SELECT_POSITION:
            return self._parse_select_position(r)
        elif msg_type == MSG_SELECT_PLACE:
            return self._parse_select_place(r)
        elif msg_type == MSG_SELECT_DISFIELD:
            return self._parse_select_place(r)
        elif msg_type == MSG_SELECT_SUM:
            return self._parse_select_sum(r)
        elif msg_type == MSG_SELECT_UNSELECT_CARD:
            return self._parse_select_unselect_card(r)
        elif msg_type == MSG_SELECT_COUNTER:
            return self._parse_select_counter(r)
        elif msg_type in (MSG_SORT_CARD, MSG_SORT_CHAIN):
            return self._parse_sort_card(r)
        elif msg_type == MSG_ROCK_PAPER_SCISSORS:
            return {"player": r.read_u8()}
        elif msg_type == MSG_ANNOUNCE_RACE:
            return self._parse_announce_race(r)
        elif msg_type == MSG_ANNOUNCE_ATTRIB:
            return self._parse_announce_attrib(r)
        elif msg_type == MSG_ANNOUNCE_CARD:
            return self._parse_announce_card(r)
        elif msg_type == MSG_ANNOUNCE_NUMBER:
            return self._parse_announce_number(r)
        elif msg_type == MSG_WIN:
            player = r.read_u8()
            reason = r.read_u8()
            return {"winner": player, "reason": reason}
        elif msg_type == MSG_NEW_TURN:
            return {"player": r.read_u8()}
        elif msg_type == MSG_NEW_PHASE:
            return {"phase": r.read_u16()}
        elif msg_type == MSG_DAMAGE:
            return {"player": r.read_u8(), "amount": r.read_u32()}
        elif msg_type == MSG_RECOVER:
            return {"player": r.read_u8(), "amount": r.read_u32()}
        elif msg_type == MSG_LPUPDATE:
            return {"player": r.read_u8(), "lp": r.read_u32()}
        elif msg_type == MSG_PAY_LPCOST:
            return {"player": r.read_u8(), "amount": r.read_u32()}
        elif msg_type == MSG_HINT:
            return {"type": r.read_u8(), "player": r.read_u8(), "data": r.read_u64()}
        elif msg_type == MSG_CARD_HINT:
            con = r.read_u8()
            loc = r.read_u8()
            seq = r.read_u32()
            pos = r.read_u32()
            htype = r.read_u8()
            value = r.read_u64()
            return {"con": con, "loc": loc, "seq": seq, "pos": pos, "type": htype, "value": value}
        elif msg_type in (MSG_CONFIRM_CARDS, MSG_CONFIRM_DECKTOP, MSG_CONFIRM_EXTRATOP):
            player = r.read_u8()
            count = r.read_u32()
            cards = []
            for _ in range(count):
                cards.append(
                    {
                        "code": r.read_u32(),
                        "con": r.read_u8(),
                        "loc": r.read_u8(),
                        "seq": r.read_u32(),
                    }
                )
            return {"player": player, "cards": cards}
        elif msg_type == MSG_SHUFFLE_DECK:
            return {"player": r.read_u8()}
        elif msg_type in (MSG_SHUFFLE_HAND, MSG_SHUFFLE_EXTRA):
            player = r.read_u8()
            count = r.read_u32()
            codes = [r.read_u32() for _ in range(count)]
            return {"player": player, "codes": codes}
        elif msg_type in (MSG_TOSS_COIN, MSG_TOSS_DICE):
            player = r.read_u8()
            count = r.read_u8()
            results = [r.read_u8() for _ in range(count)]
            return {"player": player, "results": results}
        elif msg_type == MSG_HAND_RES:
            packed = r.read_u8()
            return {"hand0": packed & 0x3, "hand1": (packed >> 2) & 0x3}
        elif msg_type == MSG_START:
            return {"type": r.read_u8()}
        elif msg_type == MSG_RETRY:
            return {}
        elif msg_type == MSG_MOVE:
            code = r.read_u32()
            prev = self._read_loc_info(r)
            curr = self._read_loc_info(r)
            reason = r.read_u32()
            return {"code": code, "previous": prev, "current": curr, "reason": reason}
        elif msg_type == MSG_POS_CHANGE:
            code = r.read_u32()
            con = r.read_u8()
            loc = r.read_u8()
            seq = r.read_u8()
            prev_pos = r.read_u8()
            cur_pos = r.read_u8()
            return {
                "code": code,
                "con": con,
                "loc": loc,
                "seq": seq,
                "prev_pos": prev_pos,
                "cur_pos": cur_pos,
            }
        elif msg_type == MSG_SET:
            code = r.read_u32()
            info = self._read_loc_info(r)
            return {"code": code, **info}
        elif msg_type == MSG_SWAP:
            code1 = r.read_u32()
            info1 = self._read_loc_info(r)
            code2 = r.read_u32()
            info2 = self._read_loc_info(r)
            return {"card1": {"code": code1, **info1}, "card2": {"code": code2, **info2}}
        elif msg_type == MSG_FIELD_DISABLED:
            return {"disabled_mask": r.read_u32()}
        elif msg_type in (MSG_SUMMONING, MSG_SPSUMMONING, MSG_FLIPSUMMONING):
            code = r.read_u32()
            info = self._read_loc_info(r)
            return {"code": code, **info}
        elif msg_type in (MSG_SUMMONED, MSG_SPSUMMONED, MSG_FLIPSUMMONED):
            return {}
        elif msg_type == MSG_CHAINING:
            code = r.read_u32()
            info = self._read_loc_info(r)
            tcon = r.read_u8()
            tloc = r.read_u8()
            tseq = r.read_u32()
            desc = r.read_u64()
            chain_count = r.read_u32()
            return {
                "code": code,
                **info,
                "triggering_con": tcon,
                "triggering_loc": tloc,
                "triggering_seq": tseq,
                "desc": desc,
                "chain_count": chain_count,
            }
        elif msg_type in (
            MSG_CHAINED,
            MSG_CHAIN_SOLVING,
            MSG_CHAIN_SOLVED,
            MSG_CHAIN_NEGATED,
            MSG_CHAIN_DISABLED,
        ):
            return {"chain_count": r.read_u8()}
        elif msg_type == MSG_CHAIN_END:
            return {}
        elif msg_type in (MSG_CARD_SELECTED, MSG_BECOME_TARGET):
            count = r.read_u32()
            cards = [self._read_loc_info(r) for _ in range(count)]
            return {"cards": cards}
        elif msg_type == MSG_RANDOM_SELECTED:
            player = r.read_u8()
            count = r.read_u32()
            cards = [self._read_loc_info(r) for _ in range(count)]
            return {"player": player, "cards": cards}
        elif msg_type == MSG_DRAW:
            player = r.read_u8()
            count = r.read_u32()
            cards = []
            for _ in range(count):
                cards.append({"code": r.read_u32(), "position": r.read_u32()})
            return {"player": player, "count": count, "cards": cards}
        elif msg_type == MSG_EQUIP:
            equip = self._read_loc_info(r)
            target = self._read_loc_info(r)
            return {"equip": equip, "target": target}
        elif msg_type == MSG_UNEQUIP:
            return {"loc_info": self._read_loc_info(r)}
        elif msg_type in (MSG_CARD_TARGET, MSG_CANCEL_TARGET):
            source = self._read_loc_info(r)
            target = self._read_loc_info(r)
            return {"source": source, "target": target}
        elif msg_type == MSG_BE_CHAIN_TARGET:
            return {"raw": r.read_bytes(r.remaining())}
        elif msg_type == MSG_ATTACK:
            attacker = self._read_loc_info(r)
            target = self._read_loc_info(r)
            return {"attacker": attacker, "target": target}
        elif msg_type == MSG_BATTLE:
            attacker = self._read_loc_info(r)
            aa = r.read_u32()
            ad = r.read_u32()
            a_destroyed = r.read_u8()
            target = self._read_loc_info(r)
            da = r.read_u32()
            dd = r.read_u32()
            d_destroyed = r.read_u8()
            return {
                "attacker": attacker,
                "attacker_attack": aa,
                "attacker_defense": ad,
                "attacker_destroyed": bool(a_destroyed),
                "target": target,
                "target_attack": da,
                "target_defense": dd,
                "target_destroyed": bool(d_destroyed),
            }
        elif msg_type == MSG_ATTACK_DISABLED:
            return {}
        elif msg_type in (MSG_DAMAGE_STEP_START, MSG_DAMAGE_STEP_END):
            return {}
        elif msg_type == MSG_MISSED_EFFECT:
            info = self._read_loc_info(r)
            code = r.read_u32()
            return {"code": code, **info}
        elif msg_type in (MSG_ADD_COUNTER, MSG_REMOVE_COUNTER):
            ctype = r.read_u16()
            con = r.read_u8()
            loc = r.read_u8()
            seq = r.read_u8()
            count = r.read_u16()
            return {"counter_type": ctype, "con": con, "loc": loc, "seq": seq, "count": count}
        else:
            return {"raw": r.read_bytes(r.remaining())}

    def _read_loc_info(self, r: MessageReader) -> dict:
        """Decode a C loc_info struct: u8 controler, u8 location, u32 sequence, u32 position."""
        return {
            "con": r.read_u8(),
            "loc": r.read_u8(),
            "seq": r.read_u32(),
            "pos": r.read_u32(),
        }

    def _parse_idle_cmd(self, r: MessageReader) -> IdleCmd:
        player = r.read_u8()
        options = []

        for cat in range(5):
            count = r.read_u32()
            for i in range(count):
                code = r.read_u32()
                con = r.read_u8()
                loc = r.read_u8()
                if cat == 2:  # repos uses u8 seq
                    seq = r.read_u8()
                else:
                    seq = r.read_u32()
                options.append(
                    IdleCmdOption(category=cat, index=i, code=code, con=con, loc=loc, seq=seq)
                )

        count = r.read_u32()
        for i in range(count):
            code = r.read_u32()
            con = r.read_u8()
            loc = r.read_u8()
            seq = r.read_u32()
            desc = r.read_u64()
            r.read_u8()  # operation type
            options.append(
                IdleCmdOption(category=5, index=i, code=code, con=con, loc=loc, seq=seq, desc=desc)
            )

        can_bp = r.read_u8() != 0
        can_ep = r.read_u8() != 0
        can_shuffle = r.read_u8() != 0

        return IdleCmd(
            player=player,
            options=options,
            can_battle_phase=can_bp,
            can_end_phase=can_ep,
            can_shuffle=can_shuffle,
        )

    def _parse_battle_cmd(self, r: MessageReader) -> BattleCmd:
        player = r.read_u8()
        options = []

        count = r.read_u32()
        for i in range(count):
            code = r.read_u32()
            con = r.read_u8()
            loc = r.read_u8()
            seq = r.read_u32()
            desc = r.read_u64()
            r.read_u8()  # operation type
            options.append(
                BattleCmdOption(
                    category=0, index=i, code=code, con=con, loc=loc, seq=seq, desc=desc
                )
            )

        count = r.read_u32()
        for i in range(count):
            code = r.read_u32()
            con = r.read_u8()
            loc = r.read_u8()
            seq = r.read_u8()
            diratt = r.read_u8()
            options.append(
                BattleCmdOption(
                    category=1,
                    index=i,
                    code=code,
                    con=con,
                    loc=loc,
                    seq=seq,
                    direct_attackable=diratt,
                )
            )

        can_m2 = r.read_u8() != 0
        can_ep = r.read_u8() != 0

        return BattleCmd(player=player, options=options, can_main2=can_m2, can_end_phase=can_ep)

    def _parse_select_card(self, r: MessageReader, is_tribute: bool) -> SelectCard:
        player = r.read_u8()
        cancelable = r.read_u8() != 0
        min_ = r.read_u32()
        max_ = r.read_u32()
        count = r.read_u32()
        cards = []
        for i in range(count):
            code = r.read_u32()
            if not is_tribute:
                con = r.read_u8()
                loc = r.read_u8()
                seq = r.read_u32()
                pos = r.read_u32()
            else:
                con = r.read_u8()
                loc = r.read_u8()
                seq = r.read_u32()
                r.read_u8()  # tribute-release-param
                pos = 0
            cards.append({"code": code, "con": con, "loc": loc, "seq": seq, "pos": pos, "index": i})
        return SelectCard(
            player=player,
            cancelable=cancelable,
            min_=min_,
            max_=max_,
            cards=cards,
            is_tribute=is_tribute,
        )

    def _parse_select_chain(self, r: MessageReader) -> SelectChain:
        player = r.read_u8()
        r.read_u8()  # specount
        forced = r.read_u8() != 0
        r.read_u32()  # hint1
        r.read_u32()  # hint2
        count = r.read_u32()
        cards = []
        for i in range(count):
            code = r.read_u32()
            con = r.read_u8()
            loc = r.read_u8()
            seq = r.read_u32()
            pos = r.read_u32()
            desc = r.read_u64()
            r.read_u8()  # operation type
            cards.append(
                {
                    "code": code,
                    "con": con,
                    "loc": loc,
                    "seq": seq,
                    "pos": pos,
                    "desc": desc,
                    "index": i,
                }
            )
        return SelectChain(player=player, forced=forced, cards=cards)

    def _parse_select_effect_yn(self, r: MessageReader) -> SelectEffectYn:
        player = r.read_u8()
        code = r.read_u32()
        con = r.read_u8()
        loc = r.read_u8()
        seq = r.read_u32()
        pos = r.read_u32()
        desc = r.read_u64()
        return SelectEffectYn(
            player=player, code=code, con=con, loc=loc, seq=seq, pos=pos, desc=desc
        )

    def _parse_select_yesno(self, r: MessageReader) -> SelectYesNo:
        player = r.read_u8()
        desc = r.read_u64()
        return SelectYesNo(player=player, desc=desc)

    def _parse_select_option(self, r: MessageReader) -> SelectOption:
        player = r.read_u8()
        count = r.read_u8()
        options = []
        for _ in range(count):
            options.append(r.read_u64())
        return SelectOption(player=player, options=options)

    def _parse_select_position(self, r: MessageReader) -> SelectPosition:
        player = r.read_u8()
        code = r.read_u32()
        positions = r.read_u8()
        return SelectPosition(player=player, code=code, positions=positions)

    def _parse_select_place(self, r: MessageReader) -> SelectPlace:
        player = r.read_u8()
        min_ = r.read_u8()
        field_mask = r.read_u32()
        return SelectPlace(player=player, min_=min_, field_mask=field_mask)

    def _parse_select_sum(self, r: MessageReader) -> SelectSum:
        player = r.read_u8()
        mode = r.read_u8()
        sumval = r.read_u32()
        min_ = r.read_u32()
        max_ = r.read_u32()

        mandatory_cards = []
        optional_cards = []
        for j in range(2):
            count = r.read_u32()
            for i in range(count):
                code = r.read_u32()
                con = r.read_u8()
                loc = r.read_u8()
                seq = r.read_u32()
                pos = r.read_u32()
                op_param = r.read_u32()
                card = {
                    "code": code,
                    "con": con,
                    "loc": loc,
                    "seq": seq,
                    "pos": pos,
                    "op_param": op_param,
                    "index": i,
                }
                if j == 0:
                    mandatory_cards.append(card)
                else:
                    optional_cards.append(card)
        return SelectSum(
            player=player,
            mode=mode,
            sumval=sumval,
            min_=min_,
            max_=max_,
            mandatory_cards=mandatory_cards,
            optional_cards=optional_cards,
        )

    def _parse_select_unselect_card(self, r: MessageReader) -> SelectUnselectCard:
        player = r.read_u8()
        finishable = r.read_u8() != 0
        cancelable = r.read_u8() != 0 or finishable
        min_ = r.read_u32()
        max_ = r.read_u32()

        selectable_cards = []
        count = r.read_u32()
        for i in range(count):
            code = r.read_u32()
            con = r.read_u8()
            loc = r.read_u8()
            seq = r.read_u32()
            pos = r.read_u32()
            selectable_cards.append(
                {"code": code, "con": con, "loc": loc, "seq": seq, "pos": pos, "index": i}
            )

        selected_cards = []
        count = r.read_u32()
        for i in range(count):
            code = r.read_u32()
            con = r.read_u8()
            loc = r.read_u8()
            seq = r.read_u32()
            pos = r.read_u32()
            selected_cards.append(
                {"code": code, "con": con, "loc": loc, "seq": seq, "pos": pos, "index": i}
            )

        return SelectUnselectCard(
            player=player,
            finishable=finishable,
            cancelable=cancelable,
            min_=min_,
            max_=max_,
            selectable_cards=selectable_cards,
            selected_cards=selected_cards,
        )

    def _parse_select_counter(self, r: MessageReader) -> SelectCounter:
        player = r.read_u8()
        counter_type = r.read_u16()
        count = r.read_u16()
        num_cards = r.read_u32()
        cards = []
        for i in range(num_cards):
            code = r.read_u32()
            con = r.read_u8()
            loc = r.read_u8()
            seq = r.read_u8()
            counter = r.read_u16()
            cards.append(
                {"code": code, "con": con, "loc": loc, "seq": seq, "counter": counter, "index": i}
            )
        return SelectCounter(player=player, counter_type=counter_type, count=count, cards=cards)

    def _parse_sort_card(self, r: MessageReader) -> SortCard:
        player = r.read_u8()
        count = r.read_u32()
        cards = []
        for i in range(count):
            code = r.read_u32()
            con = r.read_u8()
            loc = r.read_u32()
            seq = r.read_u32()
            cards.append({"code": code, "con": con, "loc": loc, "seq": seq, "index": i})
        return SortCard(player=player, cards=cards)

    def _parse_announce_race(self, r: MessageReader) -> AnnounceRace:
        player = r.read_u8()
        count = r.read_u8()
        available = r.read_u64()
        return AnnounceRace(player=player, count=count, available=available)

    def _parse_announce_attrib(self, r: MessageReader) -> AnnounceAttrib:
        player = r.read_u8()
        count = r.read_u8()
        available = r.read_u32()
        return AnnounceAttrib(player=player, count=count, available=available)

    def _parse_announce_card(self, r: MessageReader) -> AnnounceCard:
        player = r.read_u8()
        count = r.read_u8()
        opcodes = []
        for _ in range(count):
            opcodes.append(r.read_u64())
        return AnnounceCard(player=player, opcodes=opcodes)

    def _parse_announce_number(self, r: MessageReader) -> AnnounceNumber:
        player = r.read_u8()
        count = r.read_u8()
        numbers = []
        for _ in range(count):
            numbers.append(r.read_u64())
        return AnnounceNumber(player=player, numbers=numbers)


def extract_lp_from_lua(lua_text: str) -> tuple[int, int]:
    """Extract starting LP from Debug.SetPlayerInfo calls in a puzzle Lua."""
    lp0, lp1 = 8000, 8000
    for m in re.finditer(r"Debug\.SetPlayerInfo\s*\(\s*(\d+)\s*,\s*(\d+)", lua_text):
        player = int(m.group(1))
        lp = int(m.group(2))
        if player == 0:
            lp0 = lp
        elif player == 1:
            lp1 = lp
    return lp0, lp1
