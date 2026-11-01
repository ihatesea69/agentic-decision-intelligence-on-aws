"""Loan states.

This version keeps B0-B4 plus the two exits. A finer bucket scale (B0-B12, B12+, ET, Att, WO)
changes only this module.
"""
from __future__ import annotations

DELINQUENCY = ("B0", "B1", "B2", "B3", "B4")
EXITS = ("PP", "WO")
STATES = DELINQUENCY + EXITS

# Income is recognized only in B0, so state mix drives the P&L.
INCOME_STATES = ("B0",)

# WO is absorbing. PP is an exit with no further transitions in this model.
ABSORBING = ("PP", "WO")

# Severity order used to classify a move as forward (worse) or recovery (better).
# PP has no rank: a prepayment is neither a roll forward nor a cure.
_RANK = {"B0": 0, "B1": 1, "B2": 2, "B3": 3, "B4": 4, "WO": 5}


def rank(state: str) -> int | None:
    """Severity rank of a state, or None for PP."""
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}")
    return _RANK.get(state)


def is_forward(start: str, end: str) -> bool:
    a, b = rank(start), rank(end)
    return a is not None and b is not None and b > a


def is_recovery(start: str, end: str) -> bool:
    a, b = rank(start), rank(end)
    return a is not None and b is not None and b < a


def is_multi_bucket_jump(start: str, end: str) -> bool:
    """A loan moves forward one bucket at a time. A larger forward step is an exception
    to surface, never to correct silently."""
    a, b = rank(start), rank(end)
    return a is not None and b is not None and b - a > 1
