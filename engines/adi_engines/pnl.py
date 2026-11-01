"""P&L with an explicit horizon and a reconciliation check.

A headline such as "risk-adjusted net profit" can be published next to monthly KPI lines that do
not add up to it, and a Monte Carlo block can sit on an unstated horizon. This module guards both:

- every P&L carries `horizon_months`, and stock lines (ECL) are never multiplied by time;
- `reconcile` recomputes the headline from its lines under a documented formula and reports the
  residual. An unreconciled headline is marked UNRECONCILED in the evidence package, and the
  insight agent may not state it as a fact.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Mapping

FLOW_LINES = ("interest_revenue", "fee_income", "cost_of_funds", "collection_cost",
              "provision_expense", "write_off")


@dataclass(frozen=True)
class PnL:
    horizon_months: int
    interest_revenue: float
    fee_income: float
    cost_of_funds: float
    collection_cost: float
    provision_expense: float
    write_off: float = 0.0
    write_off_through_allowance: bool = True   # a write-off consumes the allowance; not charged twice

    @property
    def total_revenue(self) -> float:
        return self.interest_revenue + self.fee_income

    @property
    def net_profit(self) -> float:
        wo = 0.0 if self.write_off_through_allowance else self.write_off
        return self.total_revenue - self.cost_of_funds - self.collection_cost - self.provision_expense - wo

    def to_dict(self) -> dict:
        d = asdict(self)
        d["total_revenue"] = self.total_revenue
        d["net_profit"] = self.net_profit
        return d


def annualize(p: PnL, runoff_monthly: float = 0.0) -> PnL:
    """Scale a one-month P&L to 12 months. With runoff r each month earns (1 - r) of the previous
    one; r = 0 gives x12, which is an upper bound for a book that is running off."""
    if p.horizon_months != 1:
        raise ValueError("annualize expects a one-month P&L")
    factor = sum((1 - runoff_monthly) ** t for t in range(12))
    return replace(p, horizon_months=12, **{k: getattr(p, k) * factor for k in FLOW_LINES})


def reconcile(published: Mapping[str, float], headline: str = "risk_adjusted_net_profit",
              tolerance: float = 0.005) -> dict:
    """Recompute the headline from the published lines under both write-off treatments."""
    base = dict(horizon_months=1,
                interest_revenue=published["interest_revenue"],
                fee_income=published["fee_income"],
                cost_of_funds=published["cost_of_funds"],
                collection_cost=published["collection_cost"],
                provision_expense=published["provision_expense"],
                write_off=published.get("write_off", 0.0))
    target = published[headline]
    attempts = []
    for through in (True, False):
        p = PnL(**base, write_off_through_allowance=through)
        formula = ("revenue - cost of funds - collection cost - provision expense"
                   + ("" if through else " - write-off"))
        attempts.append({"formula": formula, "computed": round(p.net_profit, 6),
                         "residual": round(target - p.net_profit, 6)})
    best = min(attempts, key=lambda a: abs(a["residual"]))
    tied = abs(best["residual"]) <= tolerance
    return {"headline": headline, "published": target, "tied": tied,
            "status": "RECONCILED" if tied else "UNRECONCILED",
            "closest": best, "attempts": attempts, "tolerance": tolerance}
