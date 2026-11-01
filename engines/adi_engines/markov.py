"""Markov engine.

Estimates, for each starting bucket, the probability of each next-month state. Rows are the
delinquency buckets a loan can start the month in; columns add the two exits.

Sparse deep buckets (B3 and B4 often hold a few dozen accounts) are pooled over months or smoothed toward a
prior before they may enter the MDP. `prepare_for_mdp` enforces that rule.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Iterable, Mapping, Sequence

import numpy as np

from .states import DELINQUENCY, STATES, is_forward, is_recovery

FROM_STATES = DELINQUENCY
TO_STATES = STATES
Z95 = 1.959963984540054

# MOB bands: segment by loan age so seasoning is not read as a quality change.
MOB_BANDS = ((0, 6, "MOB 0-6"), (7, 12, "MOB 7-12"), (13, 24, "MOB 13-24"), (25, 10**9, "MOB 25+"))


def count_transitions(pairs: Iterable[tuple[str, str]]) -> np.ndarray:
    """Count (start state, end state) pairs into a 5 x 7 matrix."""
    counts = np.zeros((len(FROM_STATES), len(TO_STATES)), dtype=np.int64)
    fi = {s: i for i, s in enumerate(FROM_STATES)}
    ti = {s: i for i, s in enumerate(TO_STATES)}
    for start, end in pairs:
        if start not in fi:
            raise ValueError(f"start state {start!r} is not a delinquency bucket")
        if end not in ti:
            raise ValueError(f"unknown end state {end!r}")
        counts[fi[start], ti[end]] += 1
    return counts


def transition_matrix(counts) -> np.ndarray:
    """Row-normalize counts. A row with no accounts stays all zero (it is reported as sparse)."""
    c = np.asarray(counts, dtype=float)
    n = c.sum(axis=1, keepdims=True)
    with np.errstate(invalid="ignore", divide="ignore"):
        return np.where(n > 0, c / n, 0.0)


@dataclass(frozen=True)
class RollSummary:
    accounts: int
    rolled_forward: int
    recovered: int
    forward_rate: float
    recovery_rate: float
    net_deterioration: float


def roll_summary(counts) -> RollSummary:
    """Forward rolls, recoveries and net migration across the book."""
    c = np.asarray(counts)
    fwd = rec = 0
    for i, start in enumerate(FROM_STATES):
        for j, end in enumerate(TO_STATES):
            if is_forward(start, end):
                fwd += int(c[i, j])
            elif is_recovery(start, end):
                rec += int(c[i, j])
    total = int(c.sum())
    if total == 0:
        raise ValueError("no accounts")
    return RollSummary(total, fwd, rec, fwd / total, rec / total, (fwd - rec) / total)


def wilson_interval(k: float, n: float, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for a binomial rate k/n."""
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def interval_matrix(counts, z: float = Z95) -> tuple[np.ndarray, np.ndarray]:
    """Lower and upper Wilson bounds for every cell."""
    c = np.asarray(counts, dtype=float)
    n = c.sum(axis=1)
    lo = np.zeros_like(c)
    hi = np.zeros_like(c)
    for i in range(c.shape[0]):
        for j in range(c.shape[1]):
            lo[i, j], hi[i, j] = wilson_interval(c[i, j], n[i], z)
    return lo, hi


def pool(monthly_counts: Sequence, decay: float = 1.0) -> np.ndarray:
    """Pool several months of counts, oldest first. decay < 1 down-weights older months."""
    if not monthly_counts:
        raise ValueError("no months to pool")
    k = len(monthly_counts)
    return sum((decay ** (k - 1 - i)) * np.asarray(c, dtype=float) for i, c in enumerate(monthly_counts))


def sparse_rows(counts, min_n: int) -> list[str]:
    n = np.asarray(counts).sum(axis=1)
    return [FROM_STATES[i] for i in range(len(n)) if n[i] < min_n]


def smooth(counts, prior_matrix, min_n: int = 30, strength: float = 10.0) -> np.ndarray:
    """Shrink rows with fewer than min_n accounts toward a prior matrix (Dirichlet posterior mean).
    Rows with enough data stay as observed."""
    c = np.asarray(counts, dtype=float)
    prior = np.asarray(prior_matrix, dtype=float)
    out = transition_matrix(c)
    n = c.sum(axis=1)
    for i in range(len(n)):
        if n[i] < min_n:
            out[i] = (c[i] + strength * prior[i]) / (n[i] + strength)
    return out


@dataclass
class MarkovResult:
    counts: np.ndarray
    matrix: np.ndarray
    summary: RollSummary
    sparse_rows: list[str]
    smoothed_rows: list[str] = field(default_factory=list)
    mdp_ready: bool = False

    def to_dict(self) -> dict:
        return {
            "from_states": list(FROM_STATES),
            "to_states": list(TO_STATES),
            "counts": self.counts.astype(float).tolist(),
            "matrix": self.matrix.tolist(),
            "summary": self.summary.__dict__,
            "sparse_rows": self.sparse_rows,
            "smoothed_rows": self.smoothed_rows,
            "mdp_ready": self.mdp_ready,
        }


def estimate(counts, min_n: int = 30) -> MarkovResult:
    """Raw estimate. mdp_ready is False whenever a row is sparse."""
    c = np.asarray(counts)
    sp = sparse_rows(c, min_n)
    return MarkovResult(c, transition_matrix(c), roll_summary(c), sp, [], mdp_ready=not sp)


def prepare_for_mdp(counts, prior_matrix=None, min_n: int = 30, strength: float = 10.0) -> MarkovResult:
    """Sparse-row rule: sparse rows are smoothed toward a prior before they enter the MDP.
    Without a prior, a sparse matrix is refused rather than passed through."""
    c = np.asarray(counts)
    sp = sparse_rows(c, min_n)
    if not sp:
        return MarkovResult(c, transition_matrix(c), roll_summary(c), [], [], mdp_ready=True)
    if prior_matrix is None:
        raise ValueError(f"sparse rows {sp} need pooling or a smoothing prior before the MDP")
    m = smooth(c, prior_matrix, min_n=min_n, strength=strength)
    return MarkovResult(c, m, roll_summary(c), sp, sp, mdp_ready=True)


def mob_band(mob: int) -> str:
    for lo, hi, label in MOB_BANDS:
        if lo <= mob <= hi:
            return label
    raise ValueError(f"MOB {mob} outside every band")


def segment_counts(records: Iterable[Mapping], key=lambda r: mob_band(int(r["mob"]))) -> dict[str, np.ndarray]:
    """Count transitions per segment; the default segments by MOB band."""
    buckets: dict[str, list[tuple[str, str]]] = {}
    for r in records:
        buckets.setdefault(key(r), []).append((r["from_state"], r["to_state"]))
    return {k: count_transitions(v) for k, v in sorted(buckets.items())}
