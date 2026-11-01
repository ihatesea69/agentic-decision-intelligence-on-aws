"""Drift check for the retrain loop. Replaces SageMaker Model Monitor, which is closed to new customers.

Compares this month's transition counts with the approved baseline matrix:
- population stability index (PSI) on the starting-bucket mix;
- per-row shift: a row drifts when the observed rate of a cell falls outside the Wilson interval of
  the baseline rate by more than `min_gap`, for rows with at least `min_n` accounts.
"""
from __future__ import annotations

import numpy as np

from .markov import FROM_STATES, TO_STATES, transition_matrix, wilson_interval

PSI_WARN, PSI_ALERT = 0.10, 0.25


def psi(expected_share, actual_share, eps: float = 1e-6) -> float:
    e = np.clip(np.asarray(expected_share, dtype=float), eps, None)
    a = np.clip(np.asarray(actual_share, dtype=float), eps, None)
    return float(np.sum((a - e) * np.log(a / e)))


def check(baseline_matrix, baseline_mix, counts, min_n: int = 30, min_gap: float = 0.02) -> dict:
    c = np.asarray(counts, dtype=float)
    n = c.sum(axis=1)
    mix = n / n.sum()
    score = psi(baseline_mix, mix)
    base = np.asarray(baseline_matrix, dtype=float)
    drifted = []
    for i, s in enumerate(FROM_STATES):
        if n[i] < min_n:
            continue
        obs = transition_matrix(c)[i]
        for j, t in enumerate(TO_STATES):
            lo, hi = wilson_interval(base[i, j] * n[i], n[i])
            if obs[j] < lo - min_gap or obs[j] > hi + min_gap:
                drifted.append({"from": s, "to": t, "baseline": float(base[i, j]), "observed": float(obs[j])})
    level = "alert" if score >= PSI_ALERT or drifted else ("warn" if score >= PSI_WARN else "ok")
    return {"psi": score, "level": level, "drifted_cells": drifted, "retrain_recommended": level == "alert"}
