import numpy as np
import pytest

from adi_engines import markov, mdp
from adi_engines.reference import DEMO_COUNTS
from adi_engines.synthetic import opening_counts


def _matrix():
    c = np.array(DEMO_COUNTS)
    prior = markov.transition_matrix(markov.pool([c, c, c]))
    return markov.prepare_for_mdp(c, prior_matrix=prior).matrix


def test_new_vintage_actions_are_rejected_while_origination_is_frozen():
    allowed, rejected = mdp.check_actions(
        ["tighten_approval", "reprice", "intensify_collections", "restructure"], origination_frozen=True)
    assert allowed == ["intensify_collections", "restructure"]
    assert set(rejected) == {"tighten_approval", "reprice"}
    allowed, rejected = mdp.check_actions(["tighten_approval"], origination_frozen=False)
    assert allowed == ["tighten_approval"] and not rejected


def test_without_actions_the_optimum_is_the_baseline(econ):
    r = mdp.solve(_matrix(), opening_counts(), econ, horizon=6)
    assert r.uplift == pytest.approx(0.0, abs=1e-7)
    assert all(p == {"baseline": 1.0} for p in r.first_month_policy.values())


def test_collections_action_creates_value_and_binds_capacity(econ, collections):
    r = mdp.solve(_matrix(), opening_counts(), econ, actions=[collections], horizon=12)
    assert r.uplift > 0
    assert any(b.startswith("capacity:intensify_collections") for b in r.binding_constraints)
    used = sum(v.get("intensify_collections", 0.0) for v in r.first_month_accounts.values())
    assert used <= collections.capacity_per_month + 1e-6
    for shares in r.first_month_policy.values():
        assert sum(shares.values()) == pytest.approx(1.0)
    assert r.effects_are_assumptions is True


def test_action_reduces_expected_write_offs(econ, collections):
    r = mdp.solve(_matrix(), opening_counts(), econ, actions=[collections], horizon=12)
    assert sum(r.expected_write_offs) < sum(r.baseline_write_offs)


def test_risk_appetite_limit_that_cannot_be_met_fails_closed(econ, collections):
    with pytest.raises(mdp.MDPInfeasible):
        mdp.solve(_matrix(), opening_counts(), econ, actions=[collections], horizon=6,
                  max_wo_per_month=0.5)


def test_apply_shift_preserves_probability_mass():
    m = _matrix()
    row = mdp.apply_shift(m[1], "B1", 0.15)
    assert row.sum() == pytest.approx(1.0)
    assert row[0] > m[1][0]
    with pytest.raises(ValueError):
        mdp.apply_shift(m[1], "B1", 1.5)
