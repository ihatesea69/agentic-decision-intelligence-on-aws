"""Thin AWS access layer. Tests replace `client` and `table` with fakes or moto."""
from __future__ import annotations

import decimal
import json
import os
from datetime import datetime, timezone
from typing import Any

_clients: dict[str, Any] = {}


def client(name: str):
    if name not in _clients:
        import boto3
        _clients[name] = boto3.client(name)
    return _clients[name]


def table(name: str):
    import boto3
    return boto3.resource("dynamodb").Table(name)


def reset() -> None:
    """Drop cached clients (tests switch between moto and fakes)."""
    _clients.clear()


def error_code(exc: Exception) -> str:
    resp = getattr(exc, "response", None) or {}
    return resp.get("Error", {}).get("Code", "")


def env(name: str, default: str | None = None) -> str:
    v = os.environ.get(name, default)
    if v is None:
        raise KeyError(f"environment variable {name} is not set")
    return v


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def parse_s3(uri: str) -> tuple[str, str]:
    if not uri.startswith("s3://"):
        raise ValueError(f"not an s3 uri: {uri}")
    bucket, _, key = uri[5:].partition("/")
    return bucket, key


def get_text(uri: str) -> str:
    b, k = parse_s3(uri)
    return client("s3").get_object(Bucket=b, Key=k)["Body"].read().decode("utf-8")


def get_json(uri: str):
    return json.loads(get_text(uri))


def put_json(uri: str, obj) -> str:
    """No ServerSideEncryption argument: the bucket default (customer-managed KMS key) applies.
    Passing "aws:kms" without a key id would switch the object to the AWS-managed key.
    An explicit checksum is sent because Object Lock buckets reject a PutObject without one, and
    older SDKs in the Lambda runtime do not add it by default."""
    b, k = parse_s3(uri)
    client("s3").put_object(Bucket=b, Key=k, Body=dumps(obj).encode("utf-8"), ContentType="application/json",
                            ChecksumAlgorithm="SHA256")
    return uri


def read_jsonl(uri: str) -> list[dict]:
    return [json.loads(line) for line in get_text(uri).splitlines() if line.strip()]


class _Enc(json.JSONEncoder):
    def default(self, o):
        if isinstance(o, decimal.Decimal):
            return int(o) if o == o.to_integral_value() else float(o)
        return super().default(o)


def dumps(obj) -> str:
    return json.dumps(obj, cls=_Enc, sort_keys=True)


def to_dynamo(obj):
    """Floats are not allowed in DynamoDB items; convert to Decimal recursively."""
    return json.loads(json.dumps(obj, cls=_Enc), parse_float=decimal.Decimal)
