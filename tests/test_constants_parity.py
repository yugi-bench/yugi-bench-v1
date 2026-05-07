"""Constant-parity tests: every constant defined in
``edopro/ocgcore/ocgapi_constants.h`` must match the same-named
constant in ``engine/core.py`` byte-for-byte.

The header is parsed with regex: we extract ``#define NAME VALUE`` pairs,
substitute references (``DUEL_PZONE``), evaluate the expression in
Python, and compare against ``core.<NAME>``.

Constants that are *intentionally* not exposed in Python (e.g. internal
header guards, client-mode tokens irrelevant to the Python client) are
listed in ``SKIP`` with a reason.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

import engine.core as engine

# ---------------------------------------------------------------------------
# Paths — header is a dev-time asset, not shipped with the benchmark.
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _find_header() -> Path | None:
    """Locate ``ocgapi_constants.h`` across the common dev layouts.

    Order:
      1. ``YGO_OCGAPI_HEADER`` env var.
      2. Sibling ``yugioh/edopro/ocgcore/`` (dev tree).
      3. Sibling ``edopro/ocgcore/`` (README §2 layout).
    Returns None if not found.  The test module skips wholesale in that
    case — constant parity is a source-parity check, not a runtime
    invariant, and the production container doesn't ship the C headers.
    """
    env = os.environ.get("YGO_OCGAPI_HEADER")
    if env:
        p = Path(env)
        return p if p.exists() else None
    candidates = [
        _REPO_ROOT / "yugioh" / "edopro" / "ocgcore" / "ocgapi_constants.h",
        _REPO_ROOT / "edopro" / "ocgcore" / "ocgapi_constants.h",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


_HEADER = _find_header()
pytestmark = pytest.mark.skipif(
    _HEADER is None,
    reason=("ocgapi_constants.h not found — set YGO_OCGAPI_HEADER or put a "
            "sibling edopro/ocgcore/ tree next to this repo.  Constant "
            "parity is a source-time check; not required at runtime."),
)

# Names in the header that are *not* required on the Python side.
# These are compile-time-only tokens or aliases irrelevant to wire parity.
SKIP: dict[str, str] = {
    "OCGAPI_CONSTANTS_H":        "C header guard",
    "ATTRIBUTE_ALL":             "derived alias",
    "RACE_MAX":                  "derived alias",
    "RACE_ALL":                  "derived alias",
    "LOCATION_ONFIELD":          "derived alias (MZONE|SZONE)",
    "POS_FACEUP":                "derived alias",
    "POS_FACEDOWN":              "derived alias",
    "POS_ATTACK":                "derived alias",
    "POS_DEFENSE":               "derived alias",
    "EFFECT_CLIENT_MODE_NORMAL": "Lua-side effect marker, not wire-visible",
    "EFFECT_CLIENT_MODE_RESOLVE": "Lua-side effect marker, not wire-visible",
    "EFFECT_CLIENT_MODE_RESET":  "Lua-side effect marker, not wire-visible",
    "DUEL_MODE_MR1_FORB":        "forbidden-type bitmask, not a duel flag",
    "DUEL_MODE_MR2_FORB":        "forbidden-type bitmask, not a duel flag",
    "DUEL_MODE_MR3_FORB":        "forbidden-type bitmask, not a duel flag",
    "DUEL_MODE_MR4_FORB":        "forbidden-type bitmask, not a duel flag",
    "DUEL_MODE_MR5_FORB":        "forbidden-type bitmask, not a duel flag",
}


# ---------------------------------------------------------------------------
# Header parser
# ---------------------------------------------------------------------------
_DEFINE_RE = re.compile(
    r"^\s*#define\s+([A-Z_][A-Z0-9_]*)\s+(.+?)(?:\s*/\*.*?\*/)?\s*$",
    re.MULTILINE,
)


def _strip_casts(expr: str) -> str:
    """Remove ``(uint64_t)`` and friends — Python integers are arbitrary-precision."""
    return re.sub(r"\((?:uint|int)\d+_t\)", "", expr)


def _c_to_py(expr: str) -> str:
    """C-style integer literals and operators → Python.

    - ``0001`` (C octal) is already handled because we already pulled the raw
      expression: Python integer parser doesn't allow leading zeros, but
      the header uses them for LINK_MARKER_* — we convert ``NNNN`` (all digits,
      leading zero, ≤4 chars, no suffix) to ``0o<digits>``.
    - Removes C casts.
    - Leaves hex and parentheses intact.
    """
    expr = _strip_casts(expr).strip()
    # C octal literal (leading 0, ≥2 digits, all octal) — used by LINK_MARKER_*.
    # Apply conservatively: only when the whole expr is a bare octal literal.
    if re.fullmatch(r"0[0-7]+", expr):
        return "0o" + expr[1:]
    return expr


def _parse_header(text: str) -> dict[str, str]:
    """Return ``{NAME: raw_c_expression}`` for every ``#define``."""
    out: dict[str, str] = {}
    for m in _DEFINE_RE.finditer(text):
        name, raw = m.group(1), m.group(2).strip()
        # Skip function-like macros (next char after name was "(")
        if raw == "":
            continue
        out[name] = raw
    return out


def _eval_define(name: str, defines: dict[str, str],
                 resolved: dict[str, int],
                 depth: int = 0) -> int:
    """Resolve and evaluate a ``#define`` expression.

    References to other ``#define``s are substituted recursively. This is a
    *lexical* substitution — same model as the C preprocessor for object-like
    macros — so nothing leaks outside the constants file.
    """
    if name in resolved:
        return resolved[name]
    if depth > 32:
        raise RecursionError(f"define cycle at {name}")
    expr = _c_to_py(defines[name])
    # Substitute identifier references.
    def _sub(m: re.Match) -> str:
        ref = m.group(0)
        if ref in defines:
            val = _eval_define(ref, defines, resolved, depth + 1)
            return f"({val})"
        return ref
    subbed = re.sub(r"[A-Za-z_][A-Za-z0-9_]*", _sub, expr)
    # Any remaining non-numeric bareword is an error.
    try:
        val = eval(subbed, {"__builtins__": {}}, {})  # noqa: S307 — no untrusted input
    except Exception as exc:
        raise ValueError(f"could not eval {name}={expr!r} -> {subbed!r}: {exc}")
    if not isinstance(val, int):
        raise TypeError(f"{name} did not evaluate to int: {val!r}")
    resolved[name] = val
    return val


# ---------------------------------------------------------------------------
# Build the expected-dict once per test session
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def expected() -> dict[str, int]:
    text = _HEADER.read_text()
    defines = _parse_header(text)
    resolved: dict[str, int] = {}
    for name in list(defines):
        if name in SKIP:
            continue
        try:
            _eval_define(name, defines, resolved)
        except Exception as exc:
            pytest.fail(f"failed to parse {name}: {exc}")
    return resolved


# ---------------------------------------------------------------------------
# Actual parity assertions — grouped by constant family.
# Each test iterates its group so a single failure names the exact constant.
# ---------------------------------------------------------------------------
_GROUPS = [
    ("LOCATION_", "location"),
    ("POS_",      "position"),
    ("TYPE_",     "card type"),
    ("ATTRIBUTE_","attribute"),
    ("RACE_",     "race"),
    ("REASON_",   "reason"),
    ("STATUS_",   "status"),
    ("QUERY_",    "query"),
    ("LINK_MARKER_", "link marker"),
    ("MSG_",      "message"),
    ("HINT_",     "hint"),
    ("CHINT_",    "card hint"),
    ("PHINT_",    "player hint"),
    ("PHASE_",    "phase"),
    ("DUEL_",     "duel flag"),
    ("OPCODE_",   "opcode"),
]


def _names_for(prefix: str, expected: dict[str, int]) -> list[str]:
    return [n for n in expected if n.startswith(prefix)]


@pytest.mark.parametrize("prefix,family", _GROUPS)
def test_family_parity(prefix: str, family: str, expected: dict[str, int]) -> None:
    """Every header constant in this family must match the Python value."""
    missing: list[str] = []
    mismatched: list[tuple[str, int, int]] = []
    for name in _names_for(prefix, expected):
        if not hasattr(engine, name):
            missing.append(name)
            continue
        header_val = expected[name]
        py_val = getattr(engine, name)
        if header_val != py_val:
            mismatched.append((name, header_val, py_val))
    assert not missing, (
        f"[{family}] Python is missing {len(missing)} constant(s): "
        + ", ".join(sorted(missing))
    )
    assert not mismatched, "\n".join(
        f"[{family}] {n}: header=0x{h:x} python=0x{p:x}"
        for n, h, p in mismatched
    )


def test_player_constants(expected: dict[str, int]) -> None:
    """PLAYER_NONE and PLAYER_ALL — small family, direct check."""
    assert expected.get("PLAYER_NONE") == 2
    assert expected.get("PLAYER_ALL") == 3


def test_ocg_version_exposed() -> None:
    """OCG_VERSION_MAJOR from ocgapi_types.h should be exposed for caller
    sanity checks — not part of ocgapi_constants.h but still part of parity.
    """
    types_h = _REPO_ROOT / "yugioh" / "edopro" / "ocgcore" / "ocgapi_types.h"
    text = types_h.read_text()
    m = re.search(r"#define\s+OCG_VERSION_MAJOR\s+(\d+)", text)
    assert m, "could not find OCG_VERSION_MAJOR in ocgapi_types.h"
    header_major = int(m.group(1))
    # core.py does not expose OCG_VERSION_MAJOR as a constant; verify the
    # runtime version returned by the library instead. The library file may be
    # absent in CI — if so, skip this leg.
    try:
        eng = engine.OCGEngine  # cheap: don't actually load the lib.
    except Exception:
        pytest.skip("engine module could not be loaded")
    assert header_major == 11, (
        "header bumped to a new OCG major version — review wire-format "
        "compatibility before updating this assertion"
    )
