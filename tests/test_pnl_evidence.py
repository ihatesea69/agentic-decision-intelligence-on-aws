import pytest

from adi_engines import evidence as ev
from adi_engines import pnl
from adi_engines.reference import DEMO_KPIS


def test_demo_headline_does_not_reconcile():
    r = pnl.reconcile(DEMO_KPIS)
    assert r["status"] == "UNRECONCILED"
    assert r["closest"]["computed"] == pytest.approx(0.17)
    assert r["closest"]["residual"] == pytest.approx(0.08)


def test_a_headline_that_adds_up_reconciles():
    r = pnl.reconcile(dict(DEMO_KPIS, risk_adjusted_net_profit=0.17))
    assert r["status"] == "RECONCILED"


def test_annual_pnl_is_x12_upper_bound_and_runoff_is_lower():
    month = pnl.PnL(1, 1.90, 0.10, 0.48, 0.10, 0.15, 1.10, write_off_through_allowance=False)
    year = pnl.annualize(month)
    assert year.horizon_months == 12
    assert year.total_revenue == pytest.approx(24.0)
    assert year.net_profit == pytest.approx(2.04)
    assert pnl.annualize(month, runoff_monthly=0.014).total_revenue < year.total_revenue
    with pytest.raises(ValueError):
        pnl.annualize(year)


def _package(complete=True):
    figs = ev.figures_from("markov", "model_calculated",
                           {"net_deterioration": 0.051, "rolled_forward": 96, "recovered": 45}, "ratio", 1)
    figs += [ev.Figure("risk_adjusted_net_profit", "fact", 0.25, "VND bn", "finance KPI", 1, "UNRECONCILED"),
             ev.Figure("revenue_p50", "model_calculated", 21.6, "VND bn", "monte carlo", 12)]
    return ev.EvidencePackage("D-2030-01", "2030-01", figs, complete=complete, allowed_literals=[2030, 12])


def test_grounded_narrative_passes():
    text = ("Net migration 5.1% in 2030: 96 accounts rolled forward and 45 recovered. "
            "The 12-month median revenue is VND 21.6 bn.")
    r = ev.verify_narrative(text, _package())
    assert r.passed, r


def test_invented_number_fails():
    r = ev.verify_narrative("Net migration 6.3% this month.", _package())
    assert not r.passed and r.unmatched_numbers == ["6.3"]


def test_unreconciled_headline_cannot_be_stated():
    r = ev.verify_narrative("Risk-adjusted net profit is 0.25 VND B.", _package())
    assert not r.passed and r.unreconciled_used == ["risk_adjusted_net_profit"]


def test_pii_in_narrative_fails():
    r = ev.verify_narrative("Call 0912345678 about account AC000001.", _package())
    assert not r.passed and {"vn_phone", "account_id"} <= set(r.pii_hits)


def test_incomplete_package_forces_abstention():
    r = ev.verify_narrative("Anything.", _package(complete=False))
    assert r.must_abstain and not r.passed


def test_bucket_names_are_not_read_as_numbers():
    r = ev.verify_narrative("B0, B1 and P50 moved; net migration 5.1%.", _package())
    assert r.passed, r
