"""moto-backed S3 and DynamoDB plus fakes for the AWS APIs moto does not model well here."""
from __future__ import annotations

import json
from types import SimpleNamespace

import boto3
from botocore.exceptions import ClientError

REGION = "ap-southeast-1"

ENV = {
    "DECISION_TABLE": "decisions", "IDEMPOTENCY_TABLE": "idempotency", "AUDIT_TABLE": "audit",
    "TIA_TABLE": "tia", "AUDIT_BUCKET": "adi-audit", "CURATED_BUCKET": "adi-curated", "INGEST_BUCKET": "adi-raw",
    "CONFIG_PREFIX": "s3://adi-config/config",
    "APPROVER_TOPIC_ARN": "arn:aws:sns:ap-southeast-1:123456789012:approvers",
    "DPO_TOPIC_ARN": "arn:aws:sns:ap-southeast-1:123456789012:dpo",
    "APPROVER_ROLE_PREFIXES": "AWSReservedSSO_ADI-Approver",
    "EVENT_BUS_NAME": "adi-actions", "MODEL_ID": "test-model", "GUARDRAIL_ID": "gr-test", "GUARDRAIL_VERSION": "1",
}


def client_error(code: str, op: str = "Op") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, op)


class FakeSNS:
    def __init__(self):
        self.messages = []

    def publish(self, **kw):
        self.messages.append(kw)
        return {"MessageId": str(len(self.messages))}


class FakeSFN:
    def __init__(self):
        self.calls, self.error = [], None

    def send_task_success(self, taskToken, output):
        if self.error:
            raise client_error(self.error, "SendTaskSuccess")
        self.calls.append({"token": taskToken, "output": json.loads(output)})
        return {}


class FakeEvents:
    def __init__(self):
        self.entries, self.fail = [], 0

    def put_events(self, Entries):
        self.entries += Entries
        return {"FailedEntryCount": self.fail, "Entries": [{"EventId": str(i)} for i, _ in enumerate(Entries)]}


class FakeBedrock:
    def __init__(self):
        self.text, self.stop, self.calls = "", "end_turn", []

    def converse(self, **kw):
        self.calls.append(kw)
        return {"output": {"message": {"content": [{"text": self.text}]}}, "stopReason": self.stop}


def make_tables():
    ddb = boto3.client("dynamodb", region_name=REGION)

    def mk(name, pk, sk=None):
        keys = [{"AttributeName": pk, "KeyType": "HASH"}]
        attrs = [{"AttributeName": pk, "AttributeType": "S"}]
        if sk:
            keys.append({"AttributeName": sk, "KeyType": "RANGE"})
            attrs.append({"AttributeName": sk, "AttributeType": "S"})
        ddb.create_table(TableName=name, KeySchema=keys, AttributeDefinitions=attrs, BillingMode="PAY_PER_REQUEST")

    mk("decisions", "decision_id")
    mk("idempotency", "idempotency_key")
    mk("audit", "decision_id", "event_ts")
    mk("tia", "tia_id")


def make_buckets():
    s3 = boto3.client("s3", region_name=REGION)
    loc = {"LocationConstraint": REGION}
    for b in ("adi-curated", "adi-raw", "adi-config"):
        s3.create_bucket(Bucket=b, CreateBucketConfiguration=loc)
    s3.create_bucket(Bucket="adi-audit", CreateBucketConfiguration=loc, ObjectLockEnabledForBucket=True)
    return s3


def fakes():
    return SimpleNamespace(sns=FakeSNS(), sfn=FakeSFN(), events=FakeEvents(), bedrock=FakeBedrock())
