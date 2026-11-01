"""Workflow step 0: run context. One Step Functions execution is one decision run.

Input:  {"execution_name", "start_time", "input": {"scheduled_time"?, "snapshot_month"?}}
        (the workflow passes $$.Execution.Name, $$.Execution.StartTime and the execution input)
Output: decision_id, snapshot_month, the run / evidence / config / source prefixes, round 1.
The execution name is the decision id; it is capped at 40 characters so derived SageMaker and
Batch job names stay inside their 63-character limit.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from . import aws

_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_NAME = re.compile(r"^[A-Za-z0-9-]{1,40}$")


def expected_month(scheduled_time: str) -> str:
    """The run on day N of month M validates the snapshot of month M-1."""
    d = datetime.fromisoformat(scheduled_time.replace("Z", "+00:00")).date()
    prev = date(d.year - 1, 12, 1) if d.month == 1 else date(d.year, d.month - 1, 1)
    return prev.strftime("%Y-%m")


def handler(event, _context=None):
    name = event["execution_name"]
    if not _NAME.match(name):
        raise ValueError("execution name must match [A-Za-z0-9-]{1,40}")
    inp = event.get("input") or {}
    month = inp.get("snapshot_month") or expected_month(inp.get("scheduled_time") or event["start_time"])
    if not _MONTH.match(month):
        raise ValueError(f"snapshot_month {month!r} is not YYYY-MM")
    run = f"s3://{aws.env('CURATED_BUCKET')}/runs/{name}"
    return {
        "decision_id": name,
        "snapshot_month": month,
        "run_prefix": run,
        "evidence_prefix": f"{run}/evidence",
        "config_prefix": aws.env("CONFIG_PREFIX"),
        "source_prefix": f"s3://{aws.env('INGEST_BUCKET')}/deidentified/{month}/",
        "round": 1,
    }
