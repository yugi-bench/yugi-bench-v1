"""Card glossary rendering for the universal prompt.

Two flavours:

- ``render_full_glossary(instance, card_db)``: every card_id that
  appears anywhere in the puzzle's setup (hand, field, deck, extra,
  graveyard, set spells/traps).  Uses the dataset's pre-computed
  ``instance['card_details']`` when present, else falls back to the
  live ``CardDB`` lookup.  This is what n-attempts bulk mode and fully
  interactive mode with ``--forage`` see — full transparency, every
  card knowable from the start.

- ``render_seen_glossary(card_codes, card_db)``: glossary restricted
  to a set of card codes the player has actually seen during play.
  Fully interactive mode without ``--forage`` builds this
  incrementally as the engine reveals cards (cards in your hand from
  the start, on your field, in either graveyard, or revealed via
  effects).

Both produce a single Markdown ``## Card Glossary`` section, one
entry per unique code, alphabetical by name.
"""

from __future__ import annotations

import json
from typing import Iterable

from engine.core import CardDB


def _render_one_card(code: int, info: dict) -> list[str]:
    """Render a single card as a few markdown lines."""
    out: list[str] = []
    name = info.get("name") or f"Card #{code}"
    types = info.get("card_types") or info.get("types") or []
    type_str = ", ".join(types) if types else _type_str_from_engine(info)
    header = f"### `{code}` — {name}"
    if type_str:
        header += f"  *({type_str})*"
    out.append(header)
    # Stat block for monsters
    bits: list[str] = []
    for k, label in (("atk", "ATK"), ("attack", "ATK"), ("def", "DEF"),
                     ("defense", "DEF"), ("level", "Level"),
                     ("attribute", "Attribute"), ("race", "Race")):
        v = info.get(k)
        if v is not None and v != "" and (k, label) not in (("atk", "ATK") if "attack" in info else ()):
            bits.append(f"{label} {v}")
    # Dedupe by label so we don't print "ATK 1500 ATK 1500" when both
    # `atk` and `attack` are present.
    seen_labels: set[str] = set()
    deduped: list[str] = []
    for b in bits:
        lbl = b.split()[0]
        if lbl in seen_labels:
            continue
        seen_labels.add(lbl)
        deduped.append(b)
    if deduped:
        out.append("- " + "  ".join(deduped))
    desc = info.get("description") or info.get("desc") or ""
    if desc:
        out.append("")
        out.append(desc.strip())
    out.append("")
    return out


def _type_str_from_engine(info: dict) -> str:
    """Best-effort 'Spell' / 'Monster, Effect' style label from CardDB.

    The dataset's ``card_details`` carries a clean ``card_types`` list;
    the engine's CardDB stores a numeric ``type`` bitmask we'd need to
    decode.  For now, show the bitmask as a fallback only.
    """
    t = info.get("type")
    if t is None:
        return ""
    return f"type bitmask 0x{t:x}"


def render_full_glossary(instance: dict, card_db: CardDB) -> str:
    """Render every card_id mentioned in the puzzle as a glossary entry.

    Prefers ``instance['card_details']`` (pre-rendered, includes
    `card_types` + clean stat fields).  Falls back to ``card_db.get``
    for any code that's missing from the dataset entry.
    """
    codes = list(instance.get("card_ids") or [])
    details = instance.get("card_details") or {}

    pairs: list[tuple[str, int, dict]] = []
    for code in codes:
        info = details.get(str(code)) or details.get(code)
        if not info:
            info = card_db.get(int(code)) or {"name": f"Card #{code}"}
        pairs.append((info.get("name") or f"Card #{code}", int(code), info))
    pairs.sort(key=lambda p: (p[0].lower(), p[1]))

    if not pairs:
        return "## Card Glossary\n\n_(no cards in this puzzle)_\n"

    lines: list[str] = ["## Card Glossary", ""]
    lines.append(
        f"Full effect text and stats for every card that appears in this "
        f"puzzle ({len(pairs)} unique cards).  Look these up rather than "
        f"reasoning from card names alone — many cards share names with "
        f"older / errata'd versions and the printed text governs."
    )
    lines.append("")
    for _name, code, info in pairs:
        lines.extend(_render_one_card(code, info))
    return "\n".join(lines).rstrip() + "\n"


def render_seen_glossary(
    seen_codes: Iterable[int],
    card_db: CardDB,
    card_details: dict | None = None,
) -> str:
    """Render glossary restricted to cards the player has actually seen.

    Used by fully interactive no-forage mode.  ``seen_codes`` is the running set
    of card_ids the player has had visibility into (hand contents,
    field face-up, opp face-up field, graveyards, cards revealed via
    effects).  ``card_details`` is the dataset's pre-rendered details
    (for richer stat/text data); ``card_db`` is the fallback.
    """
    seen_set: set[int] = {int(c) for c in seen_codes}
    if not seen_set:
        return "## Card Glossary (seen so far)\n\n_(no cards seen yet)_\n"

    details = card_details or {}
    pairs: list[tuple[str, int, dict]] = []
    for code in seen_set:
        info = details.get(str(code)) or details.get(code)
        if not info:
            info = card_db.get(code) or {"name": f"Card #{code}"}
        pairs.append((info.get("name") or f"Card #{code}", code, info))
    pairs.sort(key=lambda p: (p[0].lower(), p[1]))

    lines: list[str] = ["## Card Glossary (seen so far)", ""]
    lines.append(
        f"Effect text and stats for the {len(pairs)} unique cards you've "
        f"actually seen so far in this puzzle.  As you reveal more cards "
        f"(via card effects, attacks resolving, etc.), this list grows."
    )
    lines.append("")
    for _name, code, info in pairs:
        lines.extend(_render_one_card(code, info))
    return "\n".join(lines).rstrip() + "\n"


def collect_visible_codes_from_state(state: dict) -> set[int]:
    """Pull out every card code visible to the player from a state dict.

    A pure helper that doesn't touch the engine — operates on whatever
    ``engine.state.build_state`` produced.  Used to seed / grow the
    seen-glossary for fully interactive no-forage mode.
    """
    out: set[int] = set()

    def _walk(obj: object) -> None:
        if isinstance(obj, dict):
            # A card entry typically carries `code` (int) plus optional
            # `position` etc.  Skip face-down opponent cards (they're
            # serialised with code=None or omitted).
            code = obj.get("code")
            pos = obj.get("position")
            controller = obj.get("controller")
            face_down = isinstance(pos, str) and "face_down" in pos
            is_opp = controller in ("opponent", 1, "1")
            if isinstance(code, int) and not (face_down and is_opp):
                out.add(code)
            for v in obj.values():
                _walk(v)
        elif isinstance(obj, list):
            for v in obj:
                _walk(v)

    _walk(state)
    return out
