"""SYNTHETIC snapshot generator for local end-to-end runs and tests. Not real customer data.

Builds one record per account so that the transition counts equal a given matrix exactly
(by default the invented demo sample in reference.py). Balances, MOB and payments are synthetic but
satisfy the balance identity, so the DQ gate sees only the exceptions that are in the counts.
"""
from __future__ import annotations

import numpy as np

from .reference import DEMO_COUNTS, SAMPLE_MONTH
from .states import DELINQUENCY, STATES

AVG_BALANCE_BN = {"B0": 0.120, "B1": 0.125, "B2": 0.130, "B3": 0.135, "B4": 0.140}


def snapshot(counts=DEMO_COUNTS, month: str = SAMPLE_MONTH, seed: int = 7) -> list[dict]:
    rng = np.random.default_rng(seed)
    rows, n = [], 0
    for i, s in enumerate(DELINQUENCY):
        for j, t in enumerate(STATES):
            for _ in range(int(counts[i][j])):
                n += 1
                opening = round(AVG_BALANCE_BN[s] * float(rng.uniform(0.8, 1.2)), 6)
                prepay = opening if t == "PP" else 0.0
                write_off = opening if t == "WO" else 0.0
                repay = 0.0 if t in ("PP", "WO") else round(opening * 0.02, 6)
                closing = round(opening - prepay - write_off - repay, 6)
                rows.append({
                    "account_id": f"SYN{n:06d}",
                    "snapshot_month": month,
                    "from_state": s,
                    "to_state": t,
                    "mob": int(rng.integers(5, 23)),   # no new loans in the demo book
                    "product": "credit_card" if n % 3 else "personal_loan",
                    "segment": "synthetic",
                    "opening": opening,
                    "new_disbursement": 0.0,
                    "prepayment": prepay,
                    "write_off": write_off,
                    "closing": max(closing, 0.0),
                    "recorded_payment": repay,
                })
    return rows


def opening_counts(counts=DEMO_COUNTS) -> dict:
    return {s: int(sum(counts[i])) for i, s in enumerate(DELINQUENCY)}


def ending_counts(counts=DEMO_COUNTS) -> dict:
    tot = np.asarray(counts).sum(axis=0)
    return {s: int(tot[j]) for j, s in enumerate(STATES)}
