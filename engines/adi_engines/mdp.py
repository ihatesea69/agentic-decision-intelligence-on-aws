"""Constrained MDP policy engine.

    pi* = argmax E[ sum_t gamma^t (R_t - C_t - ECL_t - WO_t) ]  s.t. risk appetite and capacity limits

Solved as a finite-horizon constrained MDP over the whole portfolio with the occupancy-measure
linear program: variable x[t, s, a] is the expected number of accounts in bucket s that receive
action a in month t. Capacity limits then bind at portfolio level, which a per-account Bellman
recursion cannot express. The optimum may split a bucket between actions (a randomized policy);
that is normal for a constrained MDP and is reported as shares.

Objective, provision-expense view: ECL_t is the expected change in allowance and WO_t is the
loss at write-off. The allowance on a written-off loan is released (ecl_rate["WO"] = 0), so ECL
and write-off are not double counted.

Causal caveat: the historical matrix shows where the book goes if nothing changes. The
effect of an action on a transition row is an ASSUMPTION (`ActionSpec.shift`) until an
action-conditioned estimate or a shadow-mode experiment replaces it. Results carry
`effects_are_assumptions=True` while that is the case.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Sequence

import numpy as np
from scipy.optimize import linprog

from .states import DELINQUENCY, STATES, rank

TRANSIENT = DELINQUENCY  # B0..B4 share their indices with STATES
WO = STATES.index("WO")

# Approval-threshold and pricing actions only affect new vintages.
NEW_VINTAGE_ACTIONS = frozenset({"tighten_approval", "reprice"})


class MDPError(Exception):
    pass


class MDPInfeasible(MDPError):
    """No policy meets the capacity and risk-appetite limits. The workflow fails closed."""


@dataclass(frozen=True)
class ActionSpec:
    name: str
    states: tuple[str, ...]
    shift: Mapping[str, float]          # share of forward-roll mass moved to the cure bucket (assumption)
    cost_per_account: float             # monthly cost per account under the action
    capacity_per_month: float | None = None


@dataclass(frozen=True)
class Economics:
    balance: Mapping[str, float]        # average balance per account, by bucket
    monthly_yield: float                # interest per unit balance per month, earned in B0 only
    fee_per_account: float              # monthly fee income per B0 account
    cof_rate: float                     # cost of funds per unit balance per month
    base_collection_cost: Mapping[str, float]
    ecl_rate: Mapping[str, float]       # allowance coverage by state; PP and WO are 0
    lgd: float
    discount: float = 1.0


def apply_shift(row, state: str, share: float) -> np.ndarray:
    """Move `share` of the forward-roll probability of `state` into its cure bucket."""
    r = np.array(row, dtype=float)
    k = rank(state)
    worse = [j for j, s in enumerate(STATES) if rank(s) is not None and rank(s) > k]
    if not worse or share <= 0:
        return r
    if not 0 <= share <= 1:
        raise ValueError(f"shift for {state} must be in [0, 1]")
    target = STATES.index(DELINQUENCY[k - 1]) if k > 0 else STATES.index(state)
    moved = share * r[worse].sum()
    r[worse] *= 1 - share
    r[target] += moved
    return r


def check_actions(requested: Sequence[str], origination_frozen: bool):
    """Split requested action names into allowed ones and rejected ones with a reason."""
    allowed, rejected = [], {}
    for a in requested:
        if a in NEW_VINTAGE_ACTIONS and origination_frozen:
            rejected[a] = "origination is frozen; this action only affects new vintages"
        else:
            allowed.append(a)
    return allowed, rejected


def reward(state: str, row, action_cost: float, econ: Economics) -> float:
    bal = econ.balance[state]
    income = bal * econ.monthly_yield + econ.fee_per_account if state == "B0" else 0.0
    cof = bal * econ.cof_rate
    coll = econ.base_collection_cost.get(state, 0.0) + action_cost
    ecl_next = sum(row[j] * econ.ecl_rate.get(s, 0.0) for j, s in enumerate(STATES))
    ecl_charge = bal * (ecl_next - econ.ecl_rate.get(state, 0.0))
    wo_loss = row[WO] * bal * econ.lgd
    return income - cof - coll - ecl_charge - wo_loss


def _pairs(matrix, actions: Sequence[ActionSpec]):
    base = np.asarray(matrix, dtype=float)
    if base.shape != (len(TRANSIENT), len(STATES)):
        raise ValueError(f"matrix must be {len(TRANSIENT)} x {len(STATES)}")
    pairs = []
    for i, s in enumerate(TRANSIENT):
        pairs.append((i, "baseline", base[i], 0.0))
        for a in actions:
            if s in a.states:
                pairs.append((i, a.name, apply_shift(base[i], s, a.shift.get(s, 0.0)), a.cost_per_account))
    return pairs


def baseline_value(matrix, n0: Mapping[str, float], econ: Economics, horizon: int) -> tuple[float, list[float]]:
    """Value and expected monthly write-offs if nothing changes."""
    m = np.asarray(matrix, dtype=float)
    n = np.array([float(n0.get(s, 0.0)) for s in TRANSIENT])
    value, wo = 0.0, []
    for t in range(horizon):
        value += (econ.discount ** t) * sum(n[i] * reward(s, m[i], 0.0, econ) for i, s in enumerate(TRANSIENT))
        wo.append(float(n @ m[:, WO]))
        n = n @ m[:, : len(TRANSIENT)]
    return value, wo


@dataclass
class MDPResult:
    horizon_months: int
    value: float
    baseline_value: float
    first_month_policy: dict
    first_month_accounts: dict
    expected_write_offs: list
    baseline_write_offs: list
    binding_constraints: list
    effects_are_assumptions: bool = True
    rejected_actions: dict = field(default_factory=dict)

    @property
    def uplift(self) -> float:
        return self.value - self.baseline_value

    def to_dict(self) -> dict:
        d = dict(self.__dict__)
        d["uplift"] = self.uplift
        return d


def solve(matrix, n0: Mapping[str, float], econ: Economics, actions: Sequence[ActionSpec] = (),
          horizon: int = 12, max_wo_per_month: float | None = None, effects_measured: bool = False,
          rejected_actions: Mapping[str, str] | None = None) -> MDPResult:
    pairs = _pairs(matrix, actions)
    P, T, nT = len(pairs), horizon, len(TRANSIENT)
    nv = T * P

    def idx(t, k):
        return t * P + k

    c = np.zeros(nv)
    for t in range(T):
        for k, (i, _a, row, cost) in enumerate(pairs):
            c[idx(t, k)] = -(econ.discount ** t) * reward(TRANSIENT[i], row, cost, econ)

    a_eq, b_eq = [], []
    for s in range(nT):
        r = np.zeros(nv)
        for k, (i, *_rest) in enumerate(pairs):
            if i == s:
                r[idx(0, k)] = 1.0
        a_eq.append(r)
        b_eq.append(float(n0.get(TRANSIENT[s], 0.0)))
    for t in range(1, T):
        for s in range(nT):
            r = np.zeros(nv)
            for k, (i, _a, row, _c) in enumerate(pairs):
                if i == s:
                    r[idx(t, k)] += 1.0
                r[idx(t - 1, k)] -= row[s]
            a_eq.append(r)
            b_eq.append(0.0)

    a_ub, b_ub, names = [], [], []
    for a in actions:
        if a.capacity_per_month is None:
            continue
        for t in range(T):
            r = np.zeros(nv)
            for k, (_i, an, _row, _c) in enumerate(pairs):
                if an == a.name:
                    r[idx(t, k)] = 1.0
            a_ub.append(r)
            b_ub.append(float(a.capacity_per_month))
            names.append(f"capacity:{a.name}:m{t + 1}")
    if max_wo_per_month is not None:
        for t in range(T):
            r = np.zeros(nv)
            for k, (_i, _an, row, _c) in enumerate(pairs):
                r[idx(t, k)] = row[WO]
            a_ub.append(r)
            b_ub.append(float(max_wo_per_month))
            names.append(f"risk_appetite:write_offs:m{t + 1}")

    res = linprog(c, A_ub=np.array(a_ub) if a_ub else None, b_ub=b_ub or None,
                  A_eq=np.array(a_eq), b_eq=b_eq, bounds=(0, None), method="highs")
    if res.status == 2:
        raise MDPInfeasible("no policy meets the capacity and risk-appetite limits")
    if res.status != 0:
        raise MDPError(res.message)

    x = res.x.reshape(T, P)
    policy, accounts = {}, {}
    for s in range(nT):
        ks = [k for k, (i, *_r) in enumerate(pairs) if i == s]
        tot = sum(x[0, k] for k in ks)
        accounts[TRANSIENT[s]] = {pairs[k][1]: float(x[0, k]) for k in ks}
        policy[TRANSIENT[s]] = {pairs[k][1]: (float(x[0, k] / tot) if tot > 1e-9 else (1.0 if pairs[k][1] == "baseline" else 0.0)) for k in ks}
    wo = [float(sum(x[t, k] * pairs[k][2][WO] for k in range(P))) for t in range(T)]
    binding = []
    if a_ub:
        slack = res.ineqlin.residual
        binding = [n for n, sl, b in zip(names, slack, b_ub) if sl <= 1e-6 * max(1.0, abs(b))]
    base_v, base_wo = baseline_value(matrix, n0, econ, horizon)
    return MDPResult(horizon, float(-res.fun), base_v, policy, accounts, wo, base_wo, binding,
                     effects_are_assumptions=not effects_measured,
                     rejected_actions=dict(rejected_actions or {}))
