"""In-country consent gate, de-identification and cross-border transfer gate.

Runs in the Hanoi Local Zone (EC2 or ECS; there is no Lambda in a Local Zone). Raw PII and the
tokenization key never leave Vietnam; only de-identified records are offered for transfer.

Legal note: PDPL (Law 91/2025) treats de-identified data as no longer personal data. Whether keyed
pseudonymization with the key held in Vietnam counts as de-identification is a question for the
company's legal team. The code keeps the key in country and drops or generalizes every direct and
quasi identifier so the answer can be "yes"; it does not decide it.
"""
from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable, Mapping

CONSENT_PURPOSE = "credit_risk_analytics"
DIRECT_IDENTIFIERS = ("full_name", "national_id", "phone", "email", "street_address", "account_number")
DROP_FIELDS = ("free_text_notes", "device_id", "ip_address")
ACCOUNT_KEYS = ("account_id", "account_number")   # replaced by a keyed token in the output "account_id"
REVIEW_PERIOD = timedelta(days=183)     # the TIA is reviewed every 6 months
FILING_DEADLINE = timedelta(days=60)    # the TIA is sent to MPS / A05 within 60 days of the transfer


def token(key: bytes, field_name: str, value) -> str:
    return hmac.new(key, f"{field_name}:{value}".encode("utf-8"), hashlib.sha256).hexdigest()[:32]


def has_consent(rec: Mapping, purpose: str, as_of: datetime) -> bool:
    """Explicit, per-purpose consent that has not been withdrawn. Silence is not consent."""
    for c in rec.get("consents") or ():
        if c.get("purpose") != purpose or c.get("status") != "granted":
            continue
        granted = datetime.fromisoformat(c["granted_at"])
        withdrawn = c.get("withdrawn_at")
        if granted <= as_of and (not withdrawn or datetime.fromisoformat(withdrawn) > as_of):
            return True
    return False


def age_band(dob: str, as_of: date) -> str:
    d = date.fromisoformat(dob)
    age = as_of.year - d.year - ((as_of.month, as_of.day) < (d.month, d.day))
    lo = (age // 10) * 10
    return f"{lo}-{lo + 9}"


def deidentify(rec: Mapping, key: bytes, as_of: date) -> dict:
    """Drop direct identifiers and free text, generalize date of birth to a 10-year band and the
    address to the province, and replace the account identifier with a keyed token."""
    out = {}
    for k, v in rec.items():
        if k in DIRECT_IDENTIFIERS or k in DROP_FIELDS or k in ACCOUNT_KEYS or k in ("consents", "address"):
            continue
        if k == "date_of_birth":
            out["age_band"] = age_band(v, as_of)
        else:
            out[k] = v
    account = rec.get("account_id") or rec.get("account_number")
    subject = rec.get("national_id") or account
    if subject is None:
        raise ValueError("record has no subject identifier to tokenize")
    out["subject_token"] = token(key, "subject", subject)
    out["account_id"] = "T-" + token(key, "account", account if account is not None else subject)
    return out


@dataclass
class GateStats:
    received: int = 0
    kept: int = 0
    dropped_no_consent: int = 0


def consent_and_deidentify(records: Iterable[Mapping], key: bytes, as_of: datetime,
                           purpose: str = CONSENT_PURPOSE):
    stats, out = GateStats(), []
    for r in records:
        stats.received += 1
        if not has_consent(r, purpose, as_of):
            stats.dropped_no_consent += 1
            continue
        out.append(deidentify(r, key, as_of.date()))
        stats.kept += 1
    return out, stats


def leaked_identifiers(records: Iterable[Mapping]) -> list[str]:
    """Fields that must never appear in an outbound record."""
    bad = set(DIRECT_IDENTIFIERS) | set(DROP_FIELDS) | {"date_of_birth", "address", "consents"}
    return sorted({k for r in records for k in r if k in bad})


def transfer_allowed(tia: Mapping | None, now: datetime, policy: str = "strict") -> tuple[bool, str]:
    """Cross-border gate. "strict" (default, internal policy): the TIA must be appraised and approved.
    "statutory": a prepared TIA is enough, as long as it is filed within 60 days of the first transfer.
    Both block when the 6-month review is overdue."""
    if not tia:
        return False, "no TIA on record"
    last_review = datetime.fromisoformat(tia.get("last_reviewed_at") or tia["prepared_at"])
    if now - last_review > REVIEW_PERIOD:
        return False, "TIA 6-month review is overdue"
    status = tia.get("status")
    if policy == "strict":
        return (status == "approved", "TIA approved" if status == "approved" else f"TIA status is {status}, strict policy needs approved")
    if policy == "statutory":
        if status not in ("prepared", "submitted", "approved"):
            return False, f"TIA status is {status}"
        first = tia.get("first_transfer_at")
        if status == "prepared" and first and now - datetime.fromisoformat(first) > FILING_DEADLINE:
            return False, "TIA not filed within 60 days of the first transfer"
        return True, f"TIA {status}"
    raise ValueError("policy must be strict or statutory")
