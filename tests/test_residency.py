from datetime import datetime, timedelta

from adi_engines import residency as rz

KEY = b"test-key-held-in-vietnam"
NOW = datetime(2026, 8, 1, 9, 0, 0)


def _rec(consents):
    return {"account_number": "0001112223", "national_id": "999099012345", "full_name": "Nguyen Van A",
            "phone": "0912345678", "email": "a@example.com", "date_of_birth": "1990-05-04",
            "address": "1 Example St", "province": "Ha Noi", "free_text_notes": "called",
            "from_state": "B1", "to_state": "B0", "mob": 12, "opening": 0.1, "consents": consents}


def _granted(**kw):
    c = {"purpose": rz.CONSENT_PURPOSE, "status": "granted", "granted_at": "2026-01-02T00:00:00"}
    c.update(kw)
    return c


def test_consent_rules():
    assert rz.has_consent(_rec([_granted()]), rz.CONSENT_PURPOSE, NOW)
    assert not rz.has_consent(_rec([]), rz.CONSENT_PURPOSE, NOW)  # silence is not consent
    assert not rz.has_consent(_rec([_granted(purpose="marketing")]), rz.CONSENT_PURPOSE, NOW)
    assert not rz.has_consent(_rec([_granted(withdrawn_at="2026-07-01T00:00:00")]), rz.CONSENT_PURPOSE, NOW)
    assert rz.has_consent(_rec([_granted(withdrawn_at="2026-09-01T00:00:00")]), rz.CONSENT_PURPOSE, NOW)


def test_gate_drops_unconsented_and_removes_identifiers():
    recs = [_rec([_granted()]), _rec([]), _rec([_granted(status="denied")])]
    out, stats = rz.consent_and_deidentify(recs, KEY, NOW)
    assert (stats.received, stats.kept, stats.dropped_no_consent) == (3, 1, 2)
    assert rz.leaked_identifiers(out) == []
    assert out[0]["age_band"] == "30-39" and out[0]["province"] == "Ha Noi"
    assert out[0]["from_state"] == "B1"


def test_tokens_are_stable_and_keyed():
    a = rz.deidentify(_rec([_granted()]), KEY, NOW.date())
    b = rz.deidentify(_rec([_granted()]), KEY, NOW.date())
    c = rz.deidentify(_rec([_granted()]), b"another-key", NOW.date())
    assert a["subject_token"] == b["subject_token"] != c["subject_token"]
    assert "999099012345" not in str(a)


def test_transfer_gate():
    prepared = {"status": "prepared", "prepared_at": "2026-07-20T00:00:00",
                "first_transfer_at": "2026-07-25T00:00:00"}
    approved = dict(prepared, status="approved")
    assert rz.transfer_allowed(None, NOW) == (False, "no TIA on record")
    assert rz.transfer_allowed(approved, NOW)[0]
    assert not rz.transfer_allowed(prepared, NOW)[0]                         # strict policy
    assert rz.transfer_allowed(prepared, NOW, policy="statutory")[0]
    late = NOW + timedelta(days=61)
    assert not rz.transfer_allowed(dict(prepared, last_reviewed_at=late.isoformat()), late, "statutory")[0]
    stale = dict(approved, last_reviewed_at="2025-12-01T00:00:00")
    assert rz.transfer_allowed(stale, NOW) == (False, "TIA 6-month review is overdue")
