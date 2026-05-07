"""Shared pytest fixtures — make the repo root importable.

Adds the repo root to sys.path so tests can do
``from engine.core import ...`` / ``from providers import ...`` etc
regardless of where pytest is invoked from.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "src"))
