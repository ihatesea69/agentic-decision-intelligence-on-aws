import json
from datetime import datetime
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

from adi_engines import evidence as ev
from adi_engines.synthetic import snapshot
from adi_glue import build_snapshot
from adi_jobs import __main__ as cli
from adi_jobs.store import LocalStore, MemoryStore
from adi_jobs.tasks import FailClosed, task_markov, task_mc_chunk, task_mdp, task_package
from adi_residency import tokenizer

import cloud

ROOT = Path(__file__).resolve().parents[1]
CFG, RUN = "s3://adi-config/config", "s3://adi-curated/runs/D1"


def _seed(store, run=RUN, actions=None):
    store.put_bytes(f"{run}/snapshot.jsonl", "".join(json.dumps(r) + "\n" for r in snapshot()).encode())
    for name in ("economics", "actions", "prior_matrix", "published_kpis"):
        store.put_bytes(f"{CFG}/{name}.json", (ROOT / "config" / "example" / f"{name}.json").read_bytes())
    store.put_json(f"{CFG}/montecarlo.json", {"paths": 1000, "horizon_months": 12, "seed": 1})
    store.put_json(f"{CFG}/budget.json", {"revenue_budget": 28.0, "horizon_months": 12})
    if actions is not None:
        store.put_json(f"{CFG}/actions.json", actions)


def test_pipeline_end_to_end_in_memory():
    s = MemoryStore()
    _seed(s)
    mk = task_markov(s, RUN, CFG)
    assert mk["mdp_ready"] and mk["smoothed_rows"] == ["B3", "B4"] and sum(mk["n0"].values()) == 1000
    md = task_mdp(s, RUN, CFG)
    assert md["feasible"] and md["uplift"] > 0
    assert set(md["rejected_actions"]) == {"tighten_approval"}
    assert {a["name"] for a in md["actions"]} == {"intensify_collections", "restructure"}
    for i in range(2):
        task_mc_chunk(s, RUN, CFG, i, 2)
    pkg = task_package(s, RUN, CFG, 2)
    assert pkg["complete"] and pkg["mc_summary"]["horizon_months"] == 12
    assert 0 <= pkg["mc_summary"]["p_miss_revenue_budget"] <= 1
    assert pkg["reconciliation"]["status"] == "UNRECONCILED"
    figs = {f["id"]: f for f in pkg["figures"]}
    assert figs["risk_adjusted_net_profit"]["status"] == "UNRECONCILED"
    assert figs["revenue_p50"]["horizon_months"] == 12
    assert pkg["effects_are_assumptions"] is True
    p = ev.EvidencePackage.from_dict(pkg)
    ok = ev.verify_narrative("[fact] 96 accounts rolled forward and 45 recovered across 1000 accounts.", p)
    assert ok.passed, ok


def test_missing_chunk_fails_closed():
    s = MemoryStore()
    _seed(s)
    task_markov(s, RUN, CFG)
    task_mdp(s, RUN, CFG)
    task_mc_chunk(s, RUN, CFG, 0, 2)
    with pytest.raises(FailClosed, match="expected 2"):
        task_package(s, RUN, CFG, 2)


def test_infeasible_limit_fails_closed_and_keeps_evidence():
    s = MemoryStore()
    acts = json.loads((ROOT / "config" / "example" / "actions.json").read_text())
    acts["max_wo_per_month"] = 0.5
    _seed(s, actions=acts)
    task_markov(s, RUN, CFG)
    with pytest.raises(FailClosed):
        task_mdp(s, RUN, CFG)
    assert s.get_json(f"{RUN}/mdp.json")["feasible"] is False


def test_sparse_matrix_without_prior_fails_closed():
    s = MemoryStore()
    _seed(s)
    del s.objects[f"{CFG}/prior_matrix.json"]
    with pytest.raises(FailClosed, match="sparse rows"):
        task_markov(s, RUN, CFG)
    assert s.get_json(f"{RUN}/markov.json")["mdp_ready"] is False


def test_cli_on_a_local_folder(tmp_path):
    store = LocalStore(tmp_path)
    run = "s3://adi-curated/runs/D2"
    _seed(store, run=run)
    base = ["--run", run, "--config", CFG, "--local-root", str(tmp_path), "--chunks", "2"]
    assert cli.main(["markov", *base]) == 0
    assert cli.main(["mdp", *base]) == 0
    assert cli.main(["mc-chunk", *base, "--index", "0"]) == 0
    assert cli.main(["package", *base]) == 2          # chunk 1 missing -> fail closed, exit code 2
    assert cli.main(["mc-chunk", *base, "--index", "1"]) == 0
    assert cli.main(["package", *base]) == 0
    assert store.get_json(f"{run}/package.json")["complete"] is True


def test_each_round_keeps_its_own_evidence(tmp_path):
    store = LocalStore(tmp_path)
    run = "s3://adi-curated/runs/D3"
    _seed(store, run=run)
    common = ["--run", run, "--config", CFG, "--local-root", str(tmp_path), "--chunks", "1"]
    assert cli.main(["markov", *common]) == 0
    for rnd in (1, 2):
        out = ["--out", f"{run}/r{rnd}"]
        assert cli.main(["mdp", *common, *out]) == 0
        assert cli.main(["mc-chunk", *common, *out, "--index", "0"]) == 0
        assert cli.main(["package", *common, *out]) == 0
    r1 = store.get_json(f"{run}/r1/package.json")
    r2 = store.get_json(f"{run}/r2/package.json")
    assert r1["round_prefix"].endswith("/r1") and r2["round_prefix"].endswith("/r2")
    assert r1["decision_id"] == r2["decision_id"] == "D3"
    assert not store.exists(f"{run}/package.json")


# ---- Glue data-quality job ---------------------------------------------------------------------
@pytest.fixture
def s3(monkeypatch):
    for k, v in {"AWS_DEFAULT_REGION": cloud.REGION, "AWS_ACCESS_KEY_ID": "t", "AWS_SECRET_ACCESS_KEY": "t"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        yield cloud.make_buckets()


def test_glue_gate_writes_snapshot_only_when_it_passes(s3):
    rows = snapshot()
    half = len(rows) // 2
    for i, part in enumerate((rows[:half], rows[half:])):
        s3.put_object(Bucket="adi-raw", Key=f"deidentified/2026-07/part-{i}.jsonl",
                      Body="".join(json.dumps(r) + "\n" for r in part).encode())
    ok = build_snapshot.build(s3, "s3://adi-raw/deidentified/2026-07/", RUN, "2030-01")
    assert ok["passed"] and ok["parts"] == 2 and ok["records"] == 1000
    body = s3.get_object(Bucket="adi-curated", Key="runs/D1/snapshot.jsonl")["Body"].read().decode()
    assert len(body.splitlines()) == 1000
    bad = build_snapshot.build(s3, "s3://adi-raw/deidentified/2026-07/", "s3://adi-curated/runs/D9", "2030-02")
    assert not bad["passed"]
    keys = [o["Key"] for o in s3.list_objects_v2(Bucket="adi-curated", Prefix="runs/D9/")["Contents"]]
    assert keys == ["runs/D9/evidence/dq_report.json"]


# ---- Hanoi Local Zone tokenizer --------------------------------------------------------------
class _TIA:
    def __init__(self, item):
        self.item = item

    def get_item(self, Key):
        return {"Item": self.item} if self.item else {}


class _S3:
    def __init__(self):
        self.puts = []

    def put_object(self, **kw):
        self.puts.append(kw)


def _raw(tmp_path):
    month = tmp_path / "raw" / "2026-07"
    month.mkdir(parents=True)
    granted = {"purpose": "credit_risk_analytics", "status": "granted", "granted_at": "2026-01-01T00:00:00"}
    recs = [{"account_number": f"00{i:08d}", "national_id": f"9990990{i:05d}", "full_name": "Test Person",
             "phone": "0912345678", "date_of_birth": "1988-02-03", "address": "x", "province": "Ha Noi",
             "from_state": "B0", "to_state": "B0", "mob": 10, "snapshot_month": "2026-07",
             "consents": [granted] if i % 4 else []} for i in range(8)]
    (month / "extract.jsonl").write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    return tmp_path


def test_tokenizer_uploads_only_deidentified_consented_rows(tmp_path):
    root = _raw(tmp_path)
    s3 = _S3()
    tia = _TIA({"tia_id": "t1", "status": "approved", "prepared_at": "2026-07-01T00:00:00", "gate_open": True})
    rep = tokenizer.run(str(root / "raw"), "2026-07", str(root / "key" / "k"), s3, tia, "adi-raw", "t1",
                        str(root / "reports"), now=datetime(2026, 8, 2))
    assert rep["transfer_allowed"] and (rep["received"], rep["kept"], rep["dropped_no_consent"]) == (8, 6, 2)
    body = s3.puts[0]["Body"].decode()
    assert "9990990" not in body and "Test Person" not in body and "0912345678" not in body
    first = json.loads(body.splitlines()[0])
    assert first["account_id"].startswith("T-") and first["age_band"] == "30-39"
    assert len((root / "key" / "k").read_bytes()) == 32
    assert s3.puts[0]["Key"] == "deidentified/2026-07/part-0000.jsonl"


def test_tokenizer_holds_data_when_the_gate_is_closed(tmp_path):
    root = _raw(tmp_path)
    s3 = _S3()
    closed = _TIA({"tia_id": "t1", "status": "approved", "prepared_at": "2026-07-01T00:00:00", "gate_open": False})
    rep = tokenizer.run(str(root / "raw"), "2026-07", str(root / "k"), s3, closed, "adi-raw", "t1",
                        str(root / "reports"), now=datetime(2026, 8, 2))
    assert not rep["transfer_allowed"] and s3.puts == []
    missing = tokenizer.run(str(root / "raw"), "2026-07", str(root / "k"), s3, _TIA(None), "adi-raw", "t1",
                            str(root / "reports"), now=datetime(2026, 8, 2))
    assert missing["reason"] == "no TIA on record" and s3.puts == []
    assert (root / "reports" / "tokenizer-2026-07.json").exists()


def test_cli_reads_paths_from_the_environment(tmp_path, monkeypatch):
    store = LocalStore(tmp_path)
    run = "s3://adi-curated/runs/D4"
    _seed(store, run=run)
    monkeypatch.setenv("ADI_RUN", run)
    monkeypatch.setenv("ADI_CONFIG", CFG)
    monkeypatch.setenv("ADI_OUT", f"{run}/r1")
    monkeypatch.setenv("ADI_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setenv("MC_CHUNKS", "1")
    monkeypatch.setenv("AWS_BATCH_JOB_ARRAY_INDEX", "0")
    for task in ("markov", "mdp", "mc-chunk", "package"):
        assert cli.main([task]) == 0, task
    assert store.get_json(f"{run}/r1/package.json")["complete"] is True
