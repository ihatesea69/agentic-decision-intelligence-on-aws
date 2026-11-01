"""Monte Carlo simulation, step 4 of the workflow.

10,000 simulated portfolio paths, each with its own draws for migration, prepayment and loss.

- Parameter uncertainty: every path draws its own transition matrix from a Dirichlet posterior
  (observed counts + a weak prior on the structurally allowed cells). Sparse deep buckets therefore
  widen the output range instead of hiding inside one point estimate.
- Process uncertainty: accounts migrate month by month with multinomial draws.
- Deterministic replay: paths are grouped in fixed blocks, each with its own child seed. Any split
  of the blocks into chunks (AWS Batch array children) gives the same numbers, so a Spot
  interruption re-runs one chunk and the result is unchanged.
- Every output carries its horizon in months. A revenue budget must use the same horizon, so a
  multi-month simulation is never compared with a one-month KPI.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

import numpy as np

from .mdp import ActionSpec, Economics
from .states import DELINQUENCY, STATES, rank

BLOCK = 500
NT = len(DELINQUENCY)
WO = STATES.index("WO")


@dataclass(frozen=True)
class MCConfig:
    paths: int = 10_000
    horizon_months: int = 12
    seed: int = 20300131
    prior_strength: float = 1.0
    amortization_rate: float = 0.02   # monthly principal run-down of a performing balance

    @property
    def blocks(self) -> int:
        return math.ceil(self.paths / BLOCK)

    def block_size(self, b: int) -> int:
        return min(BLOCK, self.paths - b * BLOCK)


def allowed_mask() -> np.ndarray:
    """Structural support: recoveries, stay, one bucket forward, prepayment; WO only from B4."""
    m = np.zeros((NT, len(STATES)), dtype=bool)
    for i, s in enumerate(DELINQUENCY):
        for j, t in enumerate(STATES):
            if t == "PP":
                m[i, j] = True
            elif rank(t) is not None and rank(t) <= rank(s) + 1:
                m[i, j] = True
    return m


def default_prior() -> np.ndarray:
    m = allowed_mask().astype(float)
    return m / m.sum(axis=1, keepdims=True)


def draw_matrices(rng, counts, prior_matrix, prior_strength: float, n: int) -> np.ndarray:
    c = np.asarray(counts, dtype=float)
    prior = np.asarray(prior_matrix, dtype=float)
    out = np.zeros((n, NT, len(STATES)))
    for i in range(NT):
        alpha = c[i] + prior_strength * prior[i]
        sup = alpha > 0
        out[:, i, sup] = rng.dirichlet(alpha[sup], size=n)
    return out


def _shift_batch(rows: np.ndarray, state: str, share: float) -> np.ndarray:
    k = rank(state)
    worse = [j for j, s in enumerate(STATES) if rank(s) is not None and rank(s) > k]
    if not worse or share <= 0:
        return rows
    target = STATES.index(DELINQUENCY[k - 1]) if k > 0 else STATES.index(state)
    out = rows.copy()
    moved = share * out[:, worse].sum(axis=1)
    out[:, worse] *= 1 - share
    out[:, target] += moved
    return out


def apply_policy(P: np.ndarray, policy: Mapping[str, Mapping[str, float]], actions: Sequence[ActionSpec]) -> np.ndarray:
    """Mix each bucket's row by the policy shares (state -> action -> share)."""
    spec = {a.name: a for a in actions}
    out = P.copy()
    for i, s in enumerate(DELINQUENCY):
        shares = policy.get(s, {"baseline": 1.0})
        rows = P[:, i, :]
        mixed = np.zeros_like(rows)
        for name, w in shares.items():
            if w <= 0:
                continue
            mixed += w * (rows if name == "baseline" else _shift_batch(rows, s, spec[name].shift.get(s, 0.0)))
        out[:, i, :] = mixed / mixed.sum(axis=1, keepdims=True)
    return out


def _policy_cost(policy, actions) -> np.ndarray:
    spec = {a.name: a for a in actions}
    return np.array([sum(w * (0.0 if n == "baseline" else spec[n].cost_per_account)
                         for n, w in policy.get(s, {"baseline": 1.0}).items()) for s in DELINQUENCY])


def simulate_block(rng, counts, n0, econ: Economics, cfg: MCConfig, n: int, policy=None,
                   actions: Sequence[ActionSpec] = (), prior_matrix=None) -> dict:
    prior = default_prior() if prior_matrix is None else prior_matrix
    P = draw_matrices(rng, counts, prior, cfg.prior_strength, n)
    policy = policy or {}
    if policy:
        P = apply_policy(P, policy, actions)
    P = P / P.sum(axis=2, keepdims=True)
    bal = np.array([econ.balance[s] for s in DELINQUENCY])
    base_coll = np.array([econ.base_collection_cost.get(s, 0.0) for s in DELINQUENCY])
    act_cost = _policy_cost(policy, actions)
    state = np.tile(np.array([int(n0.get(s, 0)) for s in DELINQUENCY], dtype=np.int64), (n, 1))
    revenue = np.zeros(n)
    cof = np.zeros(n)
    coll = np.zeros(n)
    wo_amount = np.zeros(n)
    for t in range(cfg.horizon_months):
        f = (1 - cfg.amortization_rate) ** t
        revenue += state[:, 0] * (bal[0] * f * econ.monthly_yield + econ.fee_per_account)
        cof += state @ (bal * f) * econ.cof_rate
        coll += state @ (base_coll + act_cost)
        new = np.zeros((n, len(STATES)), dtype=np.int64)
        for i in range(NT):
            moved = rng.multinomial(state[:, i], P[:, i, :])
            new += moved
            wo_amount += moved[:, WO] * bal[i] * f
        state = new[:, :NT]
    loss = wo_amount * econ.lgd
    return {"revenue": revenue, "net_profit": revenue - cof - coll - loss,
            "write_off": wo_amount, "loss": loss, "end_accounts": state.sum(axis=1).astype(float)}


def run_chunk(chunk_index: int, chunks: int, counts, n0, econ: Economics, cfg: MCConfig,
              policy=None, actions: Sequence[ActionSpec] = (), prior_matrix=None) -> dict:
    """One AWS Batch array child: the blocks [chunk_index::chunks] in block order."""
    if not 0 <= chunk_index < chunks:
        raise ValueError("chunk_index out of range")
    seeds = np.random.SeedSequence(cfg.seed).spawn(cfg.blocks)
    parts = {}
    for b in range(chunk_index, cfg.blocks, chunks):
        rng = np.random.default_rng(seeds[b])
        parts[b] = simulate_block(rng, counts, n0, econ, cfg, cfg.block_size(b), policy, actions, prior_matrix)
    return {"chunk_index": chunk_index, "chunks": chunks, "blocks": parts}


def merge(chunk_results: Sequence[dict], cfg: MCConfig) -> dict:
    blocks = {}
    for r in chunk_results:
        blocks.update(r["blocks"])
    missing = [b for b in range(cfg.blocks) if b not in blocks]
    if missing:
        raise ValueError(f"missing blocks {missing}: re-run their chunks")
    keys = blocks[0].keys()
    return {k: np.concatenate([blocks[b][k] for b in range(cfg.blocks)]) for k in keys}


def run(counts, n0, econ: Economics, cfg: MCConfig = MCConfig(), policy=None,
        actions: Sequence[ActionSpec] = (), prior_matrix=None, chunks: int = 1) -> dict:
    parts = [run_chunk(i, chunks, counts, n0, econ, cfg, policy, actions, prior_matrix) for i in range(chunks)]
    return merge(parts, cfg)


def summarize(paths: Mapping[str, np.ndarray], cfg: MCConfig, revenue_budget: float | None = None,
              budget_horizon_months: int | None = None) -> dict:
    """P5/P50/P95 with the horizon on every number. revenue_at_risk_vs_p5 is defined as P50 - P5."""
    if revenue_budget is not None and budget_horizon_months != cfg.horizon_months:
        raise ValueError(f"budget horizon {budget_horizon_months} months does not match the "
                         f"simulation horizon {cfg.horizon_months} months")
    rev = paths["revenue"]
    p5, p50, p95 = (float(v) for v in np.percentile(rev, [5, 50, 95]))
    out = {
        "horizon_months": cfg.horizon_months,
        "paths": int(rev.size),
        "seed": cfg.seed,
        "revenue_p5": p5,
        "revenue_p50": p50,
        "revenue_p95": p95,
        "revenue_at_risk_vs_p5": p50 - p5,
        "net_profit_p5": float(np.percentile(paths["net_profit"], 5)),
        "net_profit_p50": float(np.percentile(paths["net_profit"], 50)),
        "net_profit_p95": float(np.percentile(paths["net_profit"], 95)),
        "expected_loss": float(paths["loss"].mean()),
    }
    if revenue_budget is not None:
        out["revenue_budget"] = float(revenue_budget)
        out["p_miss_revenue_budget"] = float((rev < revenue_budget).mean())
    return out
