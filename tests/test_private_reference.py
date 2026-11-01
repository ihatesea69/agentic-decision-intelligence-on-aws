"""Optional check against a private published sample. Skipped unless ADI_PRIVATE_REFERENCE points to a
JSON file kept outside the repository (keys: counts, rolled_forward, recovered, net_deterioration,
forward_roll, cure, b4_wo_interval, kpis, headline_status, headline_closest)."""
import json
import os
from pathlib import Path

import numpy as np
import pytest

from adi_engines import markov, pnl

PATH = os.environ.get("ADI_PRIVATE_REFERENCE")
pytestmark = pytest.mark.skipif(not PATH or not Path(PATH).is_file(),
                                reason="set ADI_PRIVATE_REFERENCE to the private reference JSON")


@pytest.fixture(scope="module")
def ref():
    return json.loads(Path(PATH).read_text(encoding="utf-8"))


def test_roll_summary(ref):
    s = markov.roll_summary(np.array(ref["counts"]))
    assert (s.rolled_forward, s.recovered) == (ref["rolled_forward"], ref["recovered"])
    assert round(s.net_deterioration, 3) == ref["net_deterioration"]


def test_rates_and_interval(ref):
    c = np.array(ref["counts"])
    m = markov.transition_matrix(c)
    for state, rate in ref["forward_roll"].items():
        i = markov.FROM_STATES.index(state)
        fwd = sum(m[i, j] for j, s in enumerate(markov.TO_STATES) if s != "PP" and j > i)
        assert round(fwd, 3) == rate, state
    assert round(m[1, 0], 3) == ref["cure"]["B1"]
    assert round(m[4, 3], 3) == ref["cure"]["B4"]
    lo, hi = markov.wilson_interval(c[4, 6], c[4].sum())
    assert [round(lo, 3), round(hi, 3)] == ref["b4_wo_interval"]


def test_headline_reconciliation(ref):
    r = pnl.reconcile(ref["kpis"])
    assert r["status"] == ref["headline_status"]
    assert r["closest"]["computed"] == pytest.approx(ref["headline_closest"])
