"""Data-quality gate, step 1 of the workflow.

Fail closed: a blocking check stops the run. Exceptions are surfaced with the record, never
corrected silently. Two rules come from the solution design:

- Balance identity: Closing = Opening + New disbursement - Prepayment - Period repayment - Write-off.
  Period repayment is the balancing item; a recorded payment line larger than the balancing
  repayment overstates principal reduction and is surfaced as an exception.
- A loan moves forward one bucket at a time; a multi-bucket forward jump is an exception.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Iterable, Mapping

from .states import DELINQUENCY, STATES, is_multi_bucket_jump

REQUIRED = ("account_id", "from_state", "to_state", "mob")
BALANCE_FIELDS = ("opening", "new_disbursement", "prepayment", "write_off", "closing")


@dataclass
class Check:
    name: str
    passed: bool
    severity: str  # "block" stops the run, "warn" is surfaced only
    detail: str


@dataclass
class DQResult:
    passed: bool
    snapshot_month: str
    records: int
    checks: list[Check] = field(default_factory=list)
    exceptions: list[dict] = field(default_factory=list)

    @property
    def exception_rate(self) -> float:
        return len(self.exceptions) / self.records if self.records else 0.0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["exception_rate"] = self.exception_rate
        return d


def balancing_repayment(r: Mapping) -> float:
    """Period repayment implied by the balance identity."""
    return (float(r["opening"]) + float(r["new_disbursement"]) - float(r["prepayment"])
            - float(r["write_off"]) - float(r["closing"]))


def record_exceptions(r: Mapping, tolerance: float) -> list[dict]:
    out = []
    if is_multi_bucket_jump(r["from_state"], r["to_state"]):
        out.append({"account_id": r["account_id"], "rule": "multi_bucket_forward_jump",
                    "detail": f"{r['from_state']} -> {r['to_state']}"})
    if all(k in r for k in BALANCE_FIELDS):
        implied = balancing_repayment(r)
        if implied < -tolerance:
            out.append({"account_id": r["account_id"], "rule": "balance_identity",
                        "detail": f"implied repayment {implied:.2f} is negative"})
        rec = r.get("recorded_payment")
        if rec is not None and float(rec) > implied + tolerance:
            out.append({"account_id": r["account_id"], "rule": "payment_overstates_principal",
                        "detail": f"recorded {float(rec):.2f} > balancing {implied:.2f}"})
    return out


def run_gate(records: Iterable[Mapping], snapshot_month: str, expected_month: str,
             tolerance: float = 0.01, max_exception_rate: float = 0.02) -> DQResult:
    rows = list(records)
    res = DQResult(passed=True, snapshot_month=snapshot_month, records=len(rows))

    def add(name, ok, severity, detail):
        res.checks.append(Check(name, bool(ok), severity, detail))

    add("freshness", snapshot_month == expected_month, "block",
        f"snapshot {snapshot_month}, expected {expected_month}")
    add("not_empty", len(rows) > 0, "block", f"{len(rows)} records")

    missing = [i for i, r in enumerate(rows) if any(r.get(k) in (None, "") for k in REQUIRED)]
    add("required_fields", not missing, "block", f"{len(missing)} records missing {REQUIRED}")

    ids = [r.get("account_id") for r in rows]
    dupes = len(ids) - len(set(ids))
    add("unique_accounts", dupes == 0, "block", f"{dupes} duplicate account ids")

    bad_states = [r.get("account_id") for r in rows
                  if r.get("from_state") not in DELINQUENCY or r.get("to_state") not in STATES]
    add("valid_states", not bad_states, "block", f"{len(bad_states)} records with unknown states")

    if not missing and not bad_states:
        for r in rows:
            res.exceptions.extend(record_exceptions(r, tolerance))
    rate = res.exception_rate
    add("exception_rate", rate <= max_exception_rate, "block",
        f"{len(res.exceptions)} exceptions ({rate:.2%}), limit {max_exception_rate:.2%}")

    res.passed = all(c.passed for c in res.checks if c.severity == "block")
    return res
