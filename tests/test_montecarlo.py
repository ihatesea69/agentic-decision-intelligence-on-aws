import numpy as np
import pytest

from adi_engines import montecarlo as mc
from adi_engines.reference import DEMO_COUNTS
from adi_engines.synthetic import opening_counts


def test_deterministic_replay_independent_of_chunking(econ):
    cfg = mc.MCConfig(paths=1_500, horizon_months=3, seed=11)
    one = mc.run(DEMO_COUNTS, opening_counts(), econ, cfg, chunks=1)
    four = mc.run(DEMO_COUNTS, opening_counts(), econ, cfg, chunks=4)
    for k in one:
        assert np.array_equal(one[k], four[k]), k
    assert one["revenue"].size == cfg.paths


def test_a_lost_chunk_is_detected_and_can_be_rerun(econ):
    cfg = mc.MCConfig(paths=1_200, horizon_months=2, seed=5)
    parts = [mc.run_chunk(i, 3, DEMO_COUNTS, opening_counts(), econ, cfg) for i in range(3)]
    with pytest.raises(ValueError, match="missing blocks"):
        mc.merge(parts[:2], cfg)
    rerun = mc.run_chunk(2, 3, DEMO_COUNTS, opening_counts(), econ, cfg)
    full = mc.merge(parts[:2] + [rerun], cfg)
    assert np.array_equal(full["revenue"], mc.merge(parts, cfg)["revenue"])


def test_summary_is_ordered_and_carries_its_horizon(econ):
    cfg = mc.MCConfig(paths=2_000, horizon_months=12, seed=3)
    paths = mc.run(DEMO_COUNTS, opening_counts(), econ, cfg)
    s = mc.summarize(paths, cfg, revenue_budget=26.0, budget_horizon_months=12)
    assert s["horizon_months"] == 12
    assert s["revenue_p5"] <= s["revenue_p50"] <= s["revenue_p95"]
    assert s["revenue_at_risk_vs_p5"] == pytest.approx(s["revenue_p50"] - s["revenue_p5"])
    assert 0.0 <= s["p_miss_revenue_budget"] <= 1.0


def test_budget_on_another_horizon_is_refused(econ):
    cfg = mc.MCConfig(paths=500, horizon_months=12)
    paths = mc.run(DEMO_COUNTS, opening_counts(), econ, cfg)
    with pytest.raises(ValueError, match="does not match"):
        mc.summarize(paths, cfg, revenue_budget=60.0, budget_horizon_months=24)


def test_collections_policy_lowers_expected_loss(econ, collections):
    cfg = mc.MCConfig(paths=2_000, horizon_months=12, seed=21)
    base = mc.run(DEMO_COUNTS, opening_counts(), econ, cfg)
    policy = {"B1": {"intensify_collections": 1.0}, "B2": {"intensify_collections": 1.0}}
    acted = mc.run(DEMO_COUNTS, opening_counts(), econ, cfg, policy=policy, actions=[collections])
    assert acted["loss"].mean() < base["loss"].mean()


def test_structural_mask_blocks_write_off_before_b4():
    m = mc.allowed_mask()
    wo = mc.STATES.index("WO")
    assert m[:, wo].tolist() == [False, False, False, False, True]
    assert np.allclose(mc.default_prior().sum(axis=1), 1.0)
