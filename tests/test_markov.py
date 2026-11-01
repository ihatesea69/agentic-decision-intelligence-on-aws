import numpy as np
import pytest

from adi_engines import markov
from adi_engines.reference import (DEMO_B4_WO_INTERVAL, DEMO_COUNTS, DEMO_CURE, DEMO_ENDING, DEMO_FORWARD_ROLL,
                                   DEMO_NET_DETERIORATION, DEMO_RECOVERED, DEMO_ROLLED_FORWARD, DEMO_ROW_TOTALS)
from adi_engines.synthetic import snapshot


def _counts():
    rows = snapshot()
    return markov.count_transitions((r["from_state"], r["to_state"]) for r in rows)


def test_counts_reproduce_the_demo_rows_and_columns():
    c = _counts()
    assert c.tolist() == DEMO_COUNTS
    assert c.sum(axis=1).tolist() == DEMO_ROW_TOTALS
    assert c.sum(axis=0).tolist() == DEMO_ENDING
    assert int(c.sum()) == 1000


def test_roll_summary_matches_the_hand_computed_values():
    s = markov.roll_summary(_counts())
    assert s.rolled_forward == DEMO_ROLLED_FORWARD
    assert s.recovered == DEMO_RECOVERED
    assert round(s.net_deterioration, 3) == DEMO_NET_DETERIORATION


def test_forward_roll_and_cure_rates_match_the_hand_computed_values():
    c = _counts()
    m = markov.transition_matrix(c)
    for state, rate in DEMO_FORWARD_ROLL.items():
        i = markov.FROM_STATES.index(state)
        fwd = sum(m[i, j] for j, s in enumerate(markov.TO_STATES) if s != "PP" and j > i)
        assert round(fwd, 3) == rate, state
    assert round(m[1, 0], 3) == DEMO_CURE["B1"]
    assert round(m[4, 3], 3) == DEMO_CURE["B4"]
    assert np.allclose(m.sum(axis=1), 1.0)


def test_wilson_interval_for_b4_write_off():
    lo, hi = markov.wilson_interval(14, 19)
    assert (round(lo, 3), round(hi, 3)) == DEMO_B4_WO_INTERVAL


def test_sparse_rows_are_refused_without_a_prior():
    c = _counts()
    assert markov.sparse_rows(c, 30) == ["B3", "B4"]
    assert markov.estimate(c).mdp_ready is False
    with pytest.raises(ValueError, match="sparse rows"):
        markov.prepare_for_mdp(c)


def test_smoothing_makes_sparse_rows_mdp_ready():
    c = _counts()
    prior = markov.transition_matrix(markov.pool([c, c, c]))
    r = markov.prepare_for_mdp(c, prior_matrix=prior)
    assert r.mdp_ready and r.smoothed_rows == ["B3", "B4"]
    assert np.allclose(r.matrix.sum(axis=1), 1.0)
    # rows with enough data stay exactly as observed
    assert np.allclose(r.matrix[0], markov.transition_matrix(c)[0])


def test_pool_with_decay_weights_recent_months_more():
    a = np.array(DEMO_COUNTS, dtype=float)
    pooled = markov.pool([a, 2 * a], decay=0.5)
    assert np.allclose(pooled, 0.5 * a + 2 * a)


def test_mob_band_segmentation():
    rows = snapshot()
    seg = markov.segment_counts(rows)
    assert sum(int(v.sum()) for v in seg.values()) == 1000
    assert set(seg) <= {"MOB 0-6", "MOB 7-12", "MOB 13-24", "MOB 25+"}
    with pytest.raises(ValueError):
        markov.mob_band(-1)
