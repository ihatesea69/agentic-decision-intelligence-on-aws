"""Guards against committing identifiers. A synth against a real account writes that account id into
infra/cdk.context.json; this test fails until the file holds placeholders only again."""
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLACEHOLDERS = {"111111111111", "123456789012"}


def test_cdk_context_holds_placeholder_accounts_only():
    path = ROOT / "infra" / "cdk.context.json"
    if not path.exists():
        return
    accounts = set(re.findall(r"account=(\d{12})", json.dumps(json.loads(path.read_text(encoding="utf-8")))))
    real = accounts - PLACEHOLDERS
    assert not real, ("infra/cdk.context.json holds a real AWS account id. Do not commit it; restore the "
                      "placeholder version with: git checkout -- infra/cdk.context.json")


def test_demo_sample_is_labelled_synthetic():
    text = (ROOT / "engines" / "adi_engines" / "reference.py").read_text(encoding="utf-8")
    assert "invented numbers" in text
