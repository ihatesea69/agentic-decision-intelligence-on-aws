"""Hanoi Local Zone tokenizer.

Runs on EC2 in the Local Zone subnet. Raw extracts land on the instance's encrypted data volume
under {raw_dir}/{YYYY-MM}/*.jsonl and never leave Vietnam. The tokenization key is generated on
first use and kept on that volume (backed up with EBS Local Snapshots in the Local Zone); it is
never sent to the Region, so the Singapore side cannot re-identify anyone.

One run:
  1. read the TIA register (dossier status only, no personal data) and decide if transfer is allowed;
  2. drop records without valid per-purpose consent; de-identify the rest;
  3. assert no identifier field is left;
  4. only if the TIA gate is open, upload the de-identified file to the Region ingest bucket.
A local JSON report is written for every run, transferred or not.
"""
from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

from adi_engines.residency import consent_and_deidentify, leaked_identifiers, transfer_allowed


def load_key(path: str | os.PathLike) -> bytes:
    p = Path(path)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(secrets.token_bytes(32))
    key = p.read_bytes()
    if len(key) < 32:
        raise ValueError("tokenization key is shorter than 32 bytes")
    return key


def read_raw(raw_dir: str | os.PathLike, month: str) -> list[dict]:
    rows = []
    for f in sorted(Path(raw_dir, month).glob("*.jsonl")):
        rows.extend(json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip())
    return rows


def _plain(item: dict | None) -> dict | None:
    if item is None:
        return None
    return {k: (v if isinstance(v, (str, bool)) or v is None else str(v)) for k, v in item.items()}


def run(raw_dir: str, month: str, key_path: str, s3, tia_table, ingest_bucket: str, tia_id: str,
        report_dir: str, now: datetime | None = None, policy: str = "strict") -> dict:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    tia = _plain(tia_table.get_item(Key={"tia_id": tia_id}).get("Item"))
    if tia is not None and tia.get("gate_open") is False:
        allowed, reason = False, "transfer gate closed by the TIA review job"
    else:
        allowed, reason = transfer_allowed(tia, now, policy)

    rows = read_raw(raw_dir, month)
    out, stats = consent_and_deidentify(rows, load_key(key_path), as_of=now)
    leaks = leaked_identifiers(out)
    if leaks:
        raise RuntimeError(f"identifier fields left after de-identification: {leaks}")
    for r in out:
        r.setdefault("snapshot_month", month)

    report = {"month": month, "received": stats.received, "kept": stats.kept,
              "dropped_no_consent": stats.dropped_no_consent, "transfer_allowed": allowed,
              "reason": reason, "tia_id": tia_id, "policy": policy, "run_at": now.isoformat()}
    if allowed and out:
        key = f"deidentified/{month}/part-0000.jsonl"
        body = "".join(json.dumps(r, sort_keys=True) + "\n" for r in out).encode("utf-8")
        s3.put_object(Bucket=ingest_bucket, Key=key, Body=body, ChecksumAlgorithm="SHA256")
        report["uploaded"] = f"s3://{ingest_bucket}/{key}"
    rp = Path(report_dir, f"tokenizer-{month}.json")
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def main(argv=None) -> int:
    import boto3

    p = argparse.ArgumentParser(prog="adi_residency.tokenizer")
    p.add_argument("--month", required=True)
    p.add_argument("--raw-dir", default=os.environ.get("ADI_RAW_DIR", "/data/raw"))
    p.add_argument("--key-path", default=os.environ.get("ADI_KEY_PATH", "/data/key/tokenization.key"))
    p.add_argument("--report-dir", default=os.environ.get("ADI_REPORT_DIR", "/data/reports"))
    p.add_argument("--ingest-bucket", default=os.environ.get("ADI_INGEST_BUCKET"))
    p.add_argument("--tia-table", default=os.environ.get("ADI_TIA_TABLE"))
    p.add_argument("--tia-id", default=os.environ.get("ADI_TIA_ID", "adi-credit-analytics"))
    p.add_argument("--policy", default=os.environ.get("ADI_TIA_POLICY", "strict"), choices=["strict", "statutory"])
    a = p.parse_args(argv)
    if not a.ingest_bucket or not a.tia_table:
        p.error("--ingest-bucket and --tia-table are required")
    # Optional endpoint overrides (interface endpoint DNS names) if the Local Zone subnet cannot use the
    # VPC gateway endpoints; see docs/residency.md.
    s3 = boto3.client("s3", endpoint_url=os.environ.get("ADI_S3_ENDPOINT_URL") or None)
    ddb = boto3.resource("dynamodb", endpoint_url=os.environ.get("ADI_DDB_ENDPOINT_URL") or None)
    report = run(a.raw_dir, a.month, a.key_path, s3, ddb.Table(a.tia_table),
                 a.ingest_bucket, a.tia_id, a.report_dir, policy=a.policy)
    print(json.dumps(report, sort_keys=True))
    return 0 if report["transfer_allowed"] else 3


if __name__ == "__main__":
    sys.exit(main())
