import numpy as np

from adi_engines import drift, markov
from adi_engines.reference import DEMO_COUNTS


def _baseline():
    c = np.array(DEMO_COUNTS)
    return markov.transition_matrix(c), c.sum(axis=1) / c.sum()


def test_same_month_shows_no_drift():
    m, mix = _baseline()
    r = drift.check(m, mix, DEMO_COUNTS)
    assert r["level"] == "ok" and r["psi"] < 1e-9 and not r["drifted_cells"]


def test_worse_b1_cure_is_flagged_for_retraining():
    m, mix = _baseline()
    c = np.array(DEMO_COUNTS)
    c[1] = [8, 22, 36, 5, 0, 0, 0]           # B1 cures collapse from 29 to 8
    r = drift.check(m, mix, c)
    assert r["level"] == "alert" and r["retrain_recommended"]
    assert any(d["from"] == "B1" and d["to"] == "B0" for d in r["drifted_cells"])


def test_mix_shift_raises_psi():
    m, mix = _baseline()
    c = np.array(DEMO_COUNTS)
    c[0] = (c[0] * 0.5).astype(int)
    c[2] = c[2] * 4
    assert drift.check(m, mix, c, min_n=10_000)["psi"] > drift.PSI_WARN
