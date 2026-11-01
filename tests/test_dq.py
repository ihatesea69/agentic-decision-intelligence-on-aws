import copy

from adi_engines import dq
from adi_engines.reference import SAMPLE_MONTH
from adi_engines.synthetic import snapshot


def test_sample_passes_with_exactly_the_b1_to_b3_exception():
    res = dq.run_gate(snapshot(), SAMPLE_MONTH, SAMPLE_MONTH)
    assert res.passed
    assert [e["rule"] for e in res.exceptions] == ["multi_bucket_forward_jump"]
    assert res.exceptions[0]["detail"] == "B1 -> B3"


def test_stale_snapshot_blocks_the_run():
    res = dq.run_gate(snapshot(), "2026-06", SAMPLE_MONTH)
    assert not res.passed
    assert [c.name for c in res.checks if not c.passed] == ["freshness"]


def test_duplicate_accounts_block_the_run():
    rows = snapshot()
    rows.append(copy.deepcopy(rows[0]))
    res = dq.run_gate(rows, SAMPLE_MONTH, SAMPLE_MONTH)
    assert not res.passed
    assert any(c.name == "unique_accounts" and not c.passed for c in res.checks)


def test_payment_overstating_principal_is_surfaced_not_corrected():
    rows = snapshot()
    rows[5]["recorded_payment"] = rows[5]["opening"]  # far more than the balancing repayment
    res = dq.run_gate(rows, SAMPLE_MONTH, SAMPLE_MONTH)
    rules = [e["rule"] for e in res.exceptions]
    assert "payment_overstates_principal" in rules
    assert rows[5]["recorded_payment"] == rows[5]["opening"]  # the record is untouched


def test_broken_balance_identity_is_an_exception():
    rows = snapshot()
    rows[7]["closing"] = rows[7]["opening"] * 2
    res = dq.run_gate(rows, SAMPLE_MONTH, SAMPLE_MONTH)
    assert any(e["rule"] == "balance_identity" for e in res.exceptions)


def test_exception_rate_above_limit_blocks():
    rows = snapshot()
    for r in rows[:40]:
        r["recorded_payment"] = r["opening"] * 3
    res = dq.run_gate(rows, SAMPLE_MONTH, SAMPLE_MONTH, max_exception_rate=0.02)
    assert not res.passed
    assert res.to_dict()["exception_rate"] > 0.02


def test_unknown_state_blocks():
    rows = snapshot()
    rows[0]["to_state"] = "B9"
    res = dq.run_gate(rows, SAMPLE_MONTH, SAMPLE_MONTH)
    assert not res.passed
