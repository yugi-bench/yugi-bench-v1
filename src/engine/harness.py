"""One-to-one response harness over ``libocgcore``.

Each ``Harness.respond_*`` method maps to exactly one
``field::process(Processors::X&)`` in ``ocgcore/playerop.cpp`` — same name,
same wire format, no DSL layer in between.  Wire formats are taken
verbatim from playerop.cpp; the docstring on each method gives the line
reference so a future reader can confirm.

Lifecycle:
  ``harness.start()`` → drives OCG_DuelProcess until a SELECT message
  appears, returning a ``PendingDecision``.  The caller picks a
  ``respond_*`` method matching ``decision.msg_type`` and calls it; the
  harness writes the bytes back to the engine and drives forward to the
  next SELECT.  Non-SELECT messages (MSG_NEW_TURN, MSG_DAMAGE, etc.) are
  collected as ``events`` on the ``Step`` result so the caller can render
  them but does not have to respond to them.

The harness tracks the current turn_player / phase / LP locally based on
MSG_NEW_TURN / MSG_NEW_PHASE / MSG_LPUPDATE so observation builders can
surface this without re-querying the engine.

Harness is stateless w.r.t. the model: it does not care whether the
responder is an LLM, a random agent, or a scripted test fixture.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

from .core import (
    AnnounceAttrib,
    AnnounceCard,
    AnnounceNumber,
    AnnounceRace,
    BattleCmd,
    IdleCmd,
    MSG_DAMAGE,
    MSG_LPUPDATE,
    MSG_NAME,
    MSG_NEW_PHASE,
    MSG_NEW_TURN,
    MSG_PAY_LPCOST,
    MSG_RECOVER,
    MSG_RETRY,
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
    MSG_ANNOUNCE_RACE,
    MSG_ANNOUNCE_ATTRIB,
    MSG_ANNOUNCE_CARD,
    MSG_ANNOUNCE_NUMBER,
    MSG_ROCK_PAPER_SCISSORS,
    MSG_START,
    MSG_WIN,
    OCG_DUEL_STATUS_END,
    OCGEngine,
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
    extract_lp_from_lua,
    is_select_message,
)


class HarnessError(RuntimeError):
    pass


class InvalidResponseError(HarnessError):
    """The responder chose something inconsistent with the current prompt."""


@dataclass
class PendingDecision:
    msg_type: int
    msg_name: str
    parsed: Any            # one of the Select*/Announce* dataclasses / dicts
    player: int            # 0 or 1 — whose turn it is to decide
    raw_events: list[dict] = field(default_factory=list)


@dataclass
class StepResult:
    """Result of one advance() call."""
    events: list[dict] = field(default_factory=list)
    pending: PendingDecision | None = None
    game_over: bool = False
    winner: int | None = None
    duel_ended: bool = False


@dataclass
class DuelState:
    """Tracked per-duel facts the engine does not expose as a single query."""
    turn_player: int = 0
    turn_count: int = 0
    phase: int = 0
    lp: list[int] = field(default_factory=lambda: [8000, 8000])
    winner: int | None = None
    win_reason: int | None = None
    game_over: bool = False


class Harness:
    """One-to-one libocgcore responder."""

    MAX_ITERATIONS = 10000
    MAX_RETRIES = 5

    def __init__(self, engine: OCGEngine):
        self.engine = engine
        self.state = DuelState()
        self._pending: PendingDecision | None = None
        self._retry_count = 0
        self._started = False

    # ------------------------------------------------------------------
    # Setup
    # ------------------------------------------------------------------
    def start(self, lua_setup: str) -> StepResult:
        """Create the duel, load the puzzle, and run to the first decision."""
        lp0, lp1 = extract_lp_from_lua(lua_setup)
        self.state.lp = [lp0, lp1]

        self.engine.create_duel(flags=0, lp0=lp0, lp1=lp1)
        self.engine.load_puzzle_script(lua_setup)
        # Drain any bootstrap messages emitted during script-load (hints,
        # AI names, etc).  Treat them as events.
        bootstrap = self.engine.get_message()
        bootstrap_events: list[dict] = []
        if bootstrap:
            for msg_type, parsed in self.engine.parse_messages(bootstrap):
                bootstrap_events.append(self._event(msg_type, parsed))

        self.engine.start_duel()
        self._started = True

        step = self.advance()
        # Prepend bootstrap events so callers see them in the first observation.
        step.events = bootstrap_events + step.events
        return step

    # ------------------------------------------------------------------
    # Core loop
    # ------------------------------------------------------------------
    def advance(self) -> StepResult:
        """Drive OCG_DuelProcess until the next SELECT or the duel ends."""
        events: list[dict] = []
        for _ in range(self.MAX_ITERATIONS):
            status = self.engine.process()
            raw = self.engine.get_message()

            if raw:
                for msg_type, parsed in self.engine.parse_messages(raw):
                    self._track(msg_type, parsed)
                    if msg_type == MSG_WIN:
                        self.state.game_over = True
                        self.state.winner = parsed["winner"]
                        self.state.win_reason = parsed.get("reason")
                        events.append(self._event(msg_type, parsed))
                        return StepResult(
                            events=events,
                            pending=None,
                            game_over=True,
                            winner=self.state.winner,
                            duel_ended=True,
                        )
                    if msg_type == MSG_RETRY:
                        self._retry_count += 1
                        if self._retry_count > self.MAX_RETRIES:
                            pending_name = (
                                self._pending.msg_name
                                if self._pending is not None
                                else "<cleared>"
                            )
                            raise HarnessError(
                                f"Engine rejected response (MSG_RETRY {self._retry_count} "
                                f"times). The pending decision was {pending_name}; the "
                                f"value or selection you sent is not valid for that prompt. "
                                f"Re-read the observation and pick a different valid action."
                            )
                        events.append({"type": "retry"})
                        continue
                    if is_select_message(msg_type):
                        self._retry_count = 0
                        self._pending = PendingDecision(
                            msg_type=msg_type,
                            msg_name=MSG_NAME.get(msg_type, f"MSG_{msg_type}"),
                            parsed=parsed,
                            player=self._decision_player(parsed),
                            raw_events=events,
                        )
                        return StepResult(events=events, pending=self._pending,
                                          game_over=False)
                    events.append(self._event(msg_type, parsed))

            if status == OCG_DUEL_STATUS_END:
                return StepResult(events=events, pending=None, game_over=True,
                                  winner=self.state.winner, duel_ended=True)

        raise HarnessError(
            f"Duel did not produce a decision within {self.MAX_ITERATIONS} iterations"
        )

    @property
    def pending(self) -> PendingDecision | None:
        return self._pending

    # ------------------------------------------------------------------
    # Response shims (one-to-one with field::process(Processors::X&))
    # ------------------------------------------------------------------
    def respond_select_battlecmd(self, command: str, index: int = 0) -> StepResult:
        """playerop.cpp:18 — response is ``(s << 16) | t`` where:
          t=0 activate, t=1 attack, t=2 to_m2, t=3 to_ep.
        """
        self._require(MSG_SELECT_BATTLECMD)
        cmd: BattleCmd = self._pending.parsed
        t, s = self._battlecmd_encoding(command, index, cmd)
        return self._respond_int((s << 16) | t)

    def respond_select_idlecmd(self, command: str, index: int = 0) -> StepResult:
        """playerop.cpp:69 — response is ``(s << 16) | t``:
          t=0 summon, 1 sp_summon, 2 repos, 3 mset, 4 sset, 5 activate,
          6 to_bp, 7 to_ep, 8 shuffle.
        """
        self._require(MSG_SELECT_IDLECMD)
        cmd: IdleCmd = self._pending.parsed
        t, s = self._idlecmd_encoding(command, index, cmd)
        return self._respond_int((s << 16) | t)

    def respond_select_effectyn(self, accept: bool) -> StepResult:
        """playerop.cpp:160 — response is 0 (no) or 1 (yes)."""
        self._require(MSG_SELECT_EFFECTYN)
        return self._respond_int(1 if accept else 0)

    def respond_select_yesno(self, accept: bool) -> StepResult:
        """playerop.cpp:184 — response is 0 (no) or 1 (yes)."""
        self._require(MSG_SELECT_YESNO)
        return self._respond_int(1 if accept else 0)

    def respond_select_option(self, index: int) -> StepResult:
        """playerop.cpp:205 — response is int index into options list."""
        self._require(MSG_SELECT_OPTION)
        opt: SelectOption = self._pending.parsed
        if index < 0 or index >= len(opt.options):
            raise InvalidResponseError(
                f"SelectOption index {index} out of range [0, {len(opt.options)})"
            )
        return self._respond_int(index)

    def respond_select_card(self, indices: list[int], *, cancel: bool = False) -> StepResult:
        """playerop.cpp:279 — response format is documented by
        parse_response_cards at playerop.cpp:237.  We always use type=0
        (u32 indices) for simplicity; type=-1 means cancel.
        """
        self._require(MSG_SELECT_CARD)
        sc: SelectCard = self._pending.parsed
        return self._respond_card_indices(indices, len(sc.cards), cancel,
                                          min_=sc.min_, max_=sc.max_,
                                          cancelable=sc.cancelable)

    def respond_select_card_codes(self, indices: list[int], *, cancel: bool = False) -> StepResult:
        """playerop.cpp:334 — same wire format as SelectCard but the
        engine matches indices against ``core.select_cards_codes``
        instead of ``core.select_cards``.  The message type on the wire
        is still MSG_SELECT_CARD.  Distinguish from respond_select_card
        purely by which processor sent it (the harness cannot tell; the
        caller must pick the right shim).
        """
        self._require(MSG_SELECT_CARD)
        sc: SelectCard = self._pending.parsed
        return self._respond_card_indices(indices, len(sc.cards), cancel,
                                          min_=sc.min_, max_=sc.max_,
                                          cancelable=sc.cancelable)

    def respond_select_unselect_card(self, index: int | None) -> StepResult:
        """playerop.cpp:388 — one pick at a time:
          int32_t[0] = -1 → cancel/finish (if cancelable/finishable)
          int32_t[0] =  1 then int32_t[1] = index into
              selectable_cards ∥ selected_cards.

        Index is into the combined array (``selectable`` first, then
        ``selected`` appended).  A None means cancel/finish.
        """
        self._require(MSG_SELECT_UNSELECT_CARD)
        su: SelectUnselectCard = self._pending.parsed
        if index is None:
            if not (su.cancelable or su.finishable):
                raise InvalidResponseError(
                    "SelectUnselectCard: cannot cancel/finish — prompt is not cancelable/finishable"
                )
            return self._respond_bytes(struct.pack("<i", -1))
        total = len(su.selectable_cards) + len(su.selected_cards)
        if index < 0 or index >= total:
            raise InvalidResponseError(
                f"SelectUnselectCard index {index} out of range [0, {total})"
            )
        return self._respond_bytes(struct.pack("<ii", 1, index))

    def respond_select_chain(self, index: int | None) -> StepResult:
        """playerop.cpp:454 — response is index (or -1 to decline, if
        not forced)."""
        self._require(MSG_SELECT_CHAIN)
        sc: SelectChain = self._pending.parsed
        if index is None:
            if sc.forced:
                raise InvalidResponseError("SelectChain is forced — cannot decline")
            return self._respond_int(-1)
        if index < 0 or index >= len(sc.cards):
            raise InvalidResponseError(
                f"SelectChain index {index} out of range [0, {len(sc.cards)})"
            )
        return self._respond_int(index)

    def respond_select_place(
        self, places: list[tuple[int, int, int]],
    ) -> StepResult:
        """playerop.cpp:504 — response is ``count × 3 bytes``, each
        place written as (player u8, location u8, sequence u8).

        For MSG_SELECT_DISFIELD the message type differs but the
        response format is identical.
        """
        self._require((MSG_SELECT_PLACE, MSG_SELECT_DISFIELD))
        sp: SelectPlace = self._pending.parsed
        if len(places) != sp.min_:
            raise InvalidResponseError(
                f"SelectPlace expects exactly {sp.min_} picks, got {len(places)}"
            )
        buf = bytearray()
        for player, loc, seq in places:
            buf.extend(struct.pack("<BBB", player, loc, seq))
        return self._respond_bytes(bytes(buf))

    def respond_select_position(self, position: int) -> StepResult:
        """playerop.cpp:599 — response is the position bitmask (one of
        0x1, 0x2, 0x4, 0x8)."""
        self._require(MSG_SELECT_POSITION)
        sp: SelectPosition = self._pending.parsed
        if position not in (0x1, 0x2, 0x4, 0x8):
            raise InvalidResponseError(
                f"SelectPosition: invalid position 0x{position:x}"
            )
        if not (position & sp.positions):
            raise InvalidResponseError(
                f"SelectPosition: 0x{position:x} not in available mask 0x{sp.positions:x}"
            )
        return self._respond_int(position)

    def respond_select_tribute(self, indices: list[int], *, cancel: bool = False) -> StepResult:
        """playerop.cpp:639 — same wire format as SelectCard."""
        self._require(MSG_SELECT_TRIBUTE)
        sc: SelectCard = self._pending.parsed
        return self._respond_card_indices(indices, len(sc.cards), cancel,
                                          min_=sc.min_, max_=sc.max_,
                                          cancelable=sc.cancelable)

    def respond_select_counter(self, counts: list[int]) -> StepResult:
        """playerop.cpp:704 — response is ``len(cards) × int16`` —
        the count taken from each card, in the same order the engine
        emitted them (cards are ``core.select_cards`` sorted)."""
        self._require(MSG_SELECT_COUNTER)
        sc: SelectCounter = self._pending.parsed
        if len(counts) != len(sc.cards):
            raise InvalidResponseError(
                f"SelectCounter expects {len(sc.cards)} counts (one per card), "
                f"got {len(counts)}"
            )
        total = sum(counts)
        if total != sc.count:
            raise InvalidResponseError(
                f"SelectCounter: counts must sum to {sc.count}, got {total}"
            )
        for i, c in enumerate(counts):
            if c < 0 or c > sc.cards[i]["counter"]:
                raise InvalidResponseError(
                    f"SelectCounter[{i}]: {c} exceeds the card's counter of "
                    f"{sc.cards[i]['counter']}"
                )
        buf = bytearray()
        for c in counts:
            buf.extend(struct.pack("<h", c))
        return self._respond_bytes(bytes(buf))

    def respond_select_sum(self, indices: list[int]) -> StepResult:
        """playerop.cpp:781 — same response format as SelectCard; the
        engine validates that the chosen subset sums appropriately
        against the ``acc`` target."""
        self._require(MSG_SELECT_SUM)
        ss: SelectSum = self._pending.parsed
        return self._respond_card_indices(
            indices, len(ss.optional_cards), cancel=False,
            min_=ss.min_, max_=max(ss.min_, ss.max_), cancelable=False,
        )

    def respond_sort_card(self, ordering: list[int] | None) -> StepResult:
        """playerop.cpp:871 — response is ``n × int8`` where entry i
        holds the NEW position of the card that was originally at
        position i.  None/empty = skip (writes -1).

        ``ordering`` is a permutation of 0..n-1 with no duplicates.
        """
        self._require((MSG_SORT_CARD, MSG_SORT_CHAIN))
        sc: SortCard = self._pending.parsed
        if not ordering:
            return self._respond_bytes(struct.pack("<b", -1))
        n = len(sc.cards)
        if len(ordering) != n:
            raise InvalidResponseError(
                f"SortCard expects a permutation of length {n}, got {len(ordering)}"
            )
        seen = set()
        for v in ordering:
            if v < 0 or v >= n or v in seen:
                raise InvalidResponseError(
                    f"SortCard ordering must be a permutation of 0..{n-1}"
                )
            seen.add(v)
        buf = bytearray()
        for v in ordering:
            buf.extend(struct.pack("<b", v))
        return self._respond_bytes(bytes(buf))

    def respond_announce_race(self, races_mask: int) -> StepResult:
        """playerop.cpp:914 — response is ``uint64_t`` bitmask of races.
        popcount(mask) must equal ``count`` and all bits must be in
        ``available``.
        """
        self._require(MSG_ANNOUNCE_RACE)
        ar: AnnounceRace = self._pending.parsed
        if races_mask & ~ar.available:
            raise InvalidResponseError(
                f"AnnounceRace: mask 0x{races_mask:x} has bits outside "
                f"available mask 0x{ar.available:x}"
            )
        if bin(races_mask).count("1") != ar.count:
            raise InvalidResponseError(
                f"AnnounceRace: mask 0x{races_mask:x} has "
                f"{bin(races_mask).count('1')} bits but count={ar.count}"
            )
        return self._respond_bytes(struct.pack("<Q", races_mask))

    def respond_announce_attribute(self, attribs_mask: int) -> StepResult:
        """playerop.cpp:949 — response is ``uint32_t`` bitmask of
        attributes.  Same validity check as AnnounceRace but 32-bit."""
        self._require(MSG_ANNOUNCE_ATTRIB)
        aa: AnnounceAttrib = self._pending.parsed
        if attribs_mask & ~aa.available:
            raise InvalidResponseError(
                f"AnnounceAttribute: mask 0x{attribs_mask:x} has bits outside "
                f"available mask 0x{aa.available:x}"
            )
        if bin(attribs_mask).count("1") != aa.count:
            raise InvalidResponseError(
                f"AnnounceAttribute: mask 0x{attribs_mask:x} has "
                f"{bin(attribs_mask).count('1')} bits but count={aa.count}"
            )
        return self._respond_bytes(struct.pack("<I", attribs_mask))

    def respond_announce_card(self, card_code: int) -> StepResult:
        """playerop.cpp:1075 — response is int32 card code.  The engine
        validates the code against the opcode filter."""
        self._require(MSG_ANNOUNCE_CARD)
        if card_code < 0:
            raise InvalidResponseError("AnnounceCard: card_code must be non-negative")
        return self._respond_int(card_code)

    def respond_announce_number(self, index: int) -> StepResult:
        """playerop.cpp:1099 — response is index into the number options
        list (the engine looks up the actual number via this index)."""
        self._require(MSG_ANNOUNCE_NUMBER)
        an: AnnounceNumber = self._pending.parsed
        if index < 0 or index >= len(an.numbers):
            raise InvalidResponseError(
                f"AnnounceNumber index {index} out of range [0, {len(an.numbers)})"
            )
        return self._respond_int(index)

    def respond_rock_paper_scissors(self, hand: int) -> StepResult:
        """playerop.cpp:1121 — response is 1 (rock), 2 (scissors), or
        3 (paper).  Called twice per duel (once per player) when the
        puzzle opts in."""
        self._require(MSG_ROCK_PAPER_SCISSORS)
        if hand not in (1, 2, 3):
            raise InvalidResponseError(
                "RockPaperScissors: hand must be 1 (rock), 2 (scissors), or 3 (paper)"
            )
        return self._respond_int(hand)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def _require(self, msg_types) -> None:
        if self._pending is None:
            raise HarnessError("No pending decision — did you forget to call advance()?")
        want = msg_types if isinstance(msg_types, tuple) else (msg_types,)
        if self._pending.msg_type not in want:
            names = ", ".join(MSG_NAME.get(m, str(m)) for m in want)
            raise HarnessError(
                f"Pending decision is {self._pending.msg_name}, "
                f"but responder expects one of [{names}]"
            )

    def _respond_int(self, value: int) -> StepResult:
        # Snapshot _pending so we can restore it if advance() raises (e.g.
        # MSG_RETRY exhaustion when the engine repeatedly rejects this
        # response). Without restore, the harness ends up with pending=None
        # and the episode terminates as no_pending_decision_unexpected even
        # though the model could re-plan against the same decision.
        saved_pending = self._pending
        self.engine.set_response_int(value)
        self._pending = None
        try:
            return self.advance()
        except Exception:
            self._pending = saved_pending
            raise

    def _respond_bytes(self, payload: bytes) -> StepResult:
        saved_pending = self._pending
        self.engine.set_response_bytes(payload)
        self._pending = None
        try:
            return self.advance()
        except Exception:
            self._pending = saved_pending
            raise

    def _respond_card_indices(
        self, indices: list[int], total_cards: int, cancel: bool,
        *, min_: int, max_: int, cancelable: bool,
    ) -> StepResult:
        """SelectCard-family wire format (type=0 form).

        Layout (all little-endian):
          int32_t type;          // 0 = u32 indices, -1 = cancel
          uint32_t count;        // if type = 0
          uint32_t indices[count];
        """
        if cancel:
            if not cancelable and min_ > 0:
                raise InvalidResponseError(
                    "SelectCard: cannot cancel — prompt is not cancelable"
                )
            return self._respond_bytes(struct.pack("<i", -1))
        if len(set(indices)) != len(indices):
            raise InvalidResponseError("SelectCard: duplicate index in selection")
        if len(indices) < min_ or len(indices) > max_:
            raise InvalidResponseError(
                f"SelectCard: selection of {len(indices)} outside allowed range "
                f"[{min_}, {max_}]"
            )
        for idx in indices:
            if idx < 0 or idx >= total_cards:
                raise InvalidResponseError(
                    f"SelectCard: index {idx} out of range [0, {total_cards})"
                )
        buf = bytearray()
        buf.extend(struct.pack("<iI", 0, len(indices)))
        for idx in indices:
            buf.extend(struct.pack("<I", idx))
        return self._respond_bytes(bytes(buf))

    # --- State tracking ------------------------------------------------
    def _track(self, msg_type: int, parsed) -> None:
        if msg_type == MSG_NEW_TURN:
            self.state.turn_count += 1
            self.state.turn_player = parsed["player"]
        elif msg_type == MSG_NEW_PHASE:
            self.state.phase = parsed["phase"]
        elif msg_type == MSG_LPUPDATE:
            self.state.lp[parsed["player"]] = parsed["lp"]
        elif msg_type == MSG_DAMAGE or msg_type == MSG_PAY_LPCOST:
            p = parsed["player"]
            self.state.lp[p] = max(0, self.state.lp[p] - parsed["amount"])
        elif msg_type == MSG_RECOVER:
            p = parsed["player"]
            self.state.lp[p] += parsed["amount"]
        elif msg_type == MSG_START:
            pass

    @staticmethod
    def _decision_player(parsed) -> int:
        if isinstance(parsed, dict):
            p = parsed.get("player")
            return int(p) if p is not None else 0
        return int(getattr(parsed, "player", 0))

    @staticmethod
    def _event(msg_type: int, parsed) -> dict:
        """Flatten a non-SELECT message for external consumers.

        Decodes any ``reason`` bitmask fields and the MSG_WIN ``reason``
        enum so raw integers don't leak to the caller.
        """
        from .core import render_reason, render_win_reason
        base = {"msg_type": msg_type, "msg_name": MSG_NAME.get(msg_type, f"MSG_{msg_type}")}
        if isinstance(parsed, dict):
            base.update(parsed)
            if "reason" in parsed and msg_type != MSG_WIN:
                flags = render_reason(parsed["reason"])
                if flags:
                    base["reason_flags"] = flags
            if msg_type == MSG_WIN and "reason" in parsed:
                base["win_reason"] = render_win_reason(parsed["reason"])
        else:
            # Dataclass — unlikely here since non-SELECT messages come back
            # as dicts, but fall through gracefully.
            base["parsed"] = getattr(parsed, "__dict__", {})
        return base

    # --- IdleCmd / BattleCmd encoding ----------------------------------
    # Separated so tools.py can also call these for validation without
    # submitting.
    @staticmethod
    def _idlecmd_encoding(command: str, index: int, cmd: IdleCmd) -> tuple[int, int]:
        simple = {
            "to_battle_phase": (6, cmd.can_battle_phase, "battle phase unavailable"),
            "to_end_phase":    (7, cmd.can_end_phase,    "end phase unavailable"),
            "shuffle_hand":    (8, cmd.can_shuffle,      "hand shuffle unavailable"),
        }
        if command in simple:
            t, allowed, msg = simple[command]
            if not allowed:
                raise InvalidResponseError(f"SelectIdleCmd: {msg}")
            return t, 0

        cat_map = {
            "summon":     0, "sp_summon": 1, "repos":    2,
            "set_monster":3, "set_spell": 4, "activate": 5,
        }
        if command not in cat_map:
            raise InvalidResponseError(
                f"SelectIdleCmd: unknown command {command!r}. "
                "Expected one of: summon, sp_summon, repos, set_monster, "
                "set_spell, activate, to_battle_phase, to_end_phase, shuffle_hand."
            )
        t = cat_map[command]
        matches = [o for o in cmd.options if o.category == t]
        if index < 0 or index >= len(matches):
            raise InvalidResponseError(
                f"SelectIdleCmd: {command} index {index} out of range (have "
                f"{len(matches)} option(s))"
            )
        return t, matches[index].index

    @staticmethod
    def _battlecmd_encoding(command: str, index: int, cmd: BattleCmd) -> tuple[int, int]:
        if command == "to_main_phase_2":
            if not cmd.can_main2:
                raise InvalidResponseError("SelectBattleCmd: main phase 2 unavailable")
            return 2, 0
        if command == "to_end_phase":
            if not cmd.can_end_phase:
                raise InvalidResponseError("SelectBattleCmd: end phase unavailable")
            return 3, 0
        cat_map = {"activate": 0, "attack": 1}
        if command not in cat_map:
            raise InvalidResponseError(
                f"SelectBattleCmd: unknown command {command!r}. "
                "Expected one of: activate, attack, to_main_phase_2, to_end_phase."
            )
        t = cat_map[command]
        matches = [o for o in cmd.options if o.category == t]
        if index < 0 or index >= len(matches):
            raise InvalidResponseError(
                f"SelectBattleCmd: {command} index {index} out of range (have "
                f"{len(matches)} option(s))"
            )
        return t, matches[index].index
