"""Step 1 of the workflow: AWS Glue Python shell job (Python 3.9).

Reads the de-identified monthly extract under --source_prefix, runs the data-quality gate of
adi_engines.dq (balance identity, one-bucket-forward rule, freshness, completeness, uniqueness)
and writes:
  {run_prefix}/evidence/dq_report.json   always, so a failed run keeps its evidence
  {run_prefix}/snapshot.jsonl            only when the gate passes
A failed gate raises, the Glue job run fails, and the workflow takes the fail-closed branch.

The engine package arrives as a zip (--engines_zip) and is imported with zipimport, so the job
needs no extra Python modules on the Glue side.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

ARGS = ["source_prefix", "run_prefix", "snapshot_month", "engines_zip", "max_exception_rate"]


def _split(uri: str):
    if not uri.startswith("s3://"):
        raise ValueError("not an s3 uri: " + uri)
    bucket, _, key = uri[5:].partition("/")
    return bucket, key


def read_rows(s3, prefix: str):
    bucket, key = _split(prefix)
    rows, parts, token = [], 0, None
    while True:
        kw = {"Bucket": bucket, "Prefix": key}
        if token:
            kw["ContinuationToken"] = token
        page = s3.list_objects_v2(**kw)
        for obj in page.get("Contents", []):
            if not obj["Key"].endswith(".jsonl"):
                continue
            parts += 1
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read().decode("utf-8")
            rows.extend(json.loads(line) for line in body.splitlines() if line.strip())
        if not page.get("IsTruncated"):
            return rows, parts
        token = page["NextContinuationToken"]


def _put(s3, uri: str, text: str) -> None:
    bucket, key = _split(uri)
    s3.put_object(Bucket=bucket, Key=key, Body=text.encode("utf-8"), ChecksumAlgorithm="SHA256")


def build(s3, source_prefix: str, run_prefix: str, expected_month: str, max_exception_rate: float = 0.02) -> dict:
    from adi_engines import dq

    rows, parts = read_rows(s3, source_prefix)
    months = sorted({str(r.get("snapshot_month", "")) for r in rows})
    month = months[0] if len(months) == 1 else "MIXED:" + ",".join(months)
    res = dq.run_gate(rows, month, expected_month, max_exception_rate=max_exception_rate)
    report = res.to_dict()
    report.update({"source_prefix": source_prefix, "parts": parts, "expected_month": expected_month})
    run = run_prefix.rstrip("/")
    _put(s3, run + "/evidence/dq_report.json", json.dumps(report, sort_keys=True))
    if res.passed:
        _put(s3, run + "/snapshot.jsonl", "".join(json.dumps(r, sort_keys=True) + "\n" for r in rows))
    return report


def add_engines(s3, zip_uri: str) -> None:
    bucket, key = _split(zip_uri)
    path = os.path.join(tempfile.gettempdir(), "adi_engines.zip")
    s3.download_file(bucket, key, path)
    sys.path.insert(0, path)


def parse_args(argv):
    try:
        from awsglue.utils import getResolvedOptions
        return getResolvedOptions(argv, ARGS)
    except ImportError:
        import argparse
        p = argparse.ArgumentParser()
        for a in ARGS:
            p.add_argument("--" + a, required=True)
        return vars(p.parse_args(argv[1:]))


def main(argv=None) -> None:
    import boto3

    args = parse_args(argv or sys.argv)
    s3 = boto3.client("s3")
    add_engines(s3, args["engines_zip"])
    report = build(s3, args["source_prefix"], args["run_prefix"], args["snapshot_month"],
                   float(args["max_exception_rate"]))
    if not report["passed"]:
        failed = [c["name"] for c in report["checks"] if not c["passed"]]
        raise RuntimeError("FAIL_CLOSED data-quality gate: " + ", ".join(failed))
    print("OK data-quality gate: %d records, %d exceptions" % (report["records"], len(report["exceptions"])))


if __name__ == "__main__":
    main()
