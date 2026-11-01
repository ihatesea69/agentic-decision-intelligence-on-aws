"""Step 7: controlled, idempotent actions (Step Functions -> EventBridge -> Lambda, SQS dead-letter queue).

- `emit` (called by the workflow after an approval) turns the approved candidate policy into one
  EventBridge event per action, each with an idempotency key. No event is emitted when the agent
  check did not pass (an agent failure means no production action).
- `handler` (EventBridge target) executes one action. Only ENABLED_ACTIONS run; pricing and
  approval-threshold actions stay disabled while origination is frozen.
- Idempotency: a marker row per key. A completed key is ignored. A key still IN_PROGRESS raises,
  so the event goes through Lambda retries to the dead-letter queue for a human, instead of being
  silently dropped or run twice. A failed attempt marks the key FAILED and re-raises.
- Downstream: the lending and collections systems and their reversal APIs are an open question.
  Until they exist the adapter is RECORD_ONLY: it records the instruction and a rollback
  reference in the audit store and calls no external system.
"""
from __future__ import annotations

import hashlib

from botocore.exceptions import ClientError

from . import aws

REQUIRED = ("decision_id", "action", "effective_period", "idempotency_key", "approver")
SOURCE = "adi.decisions"
DETAIL_TYPE = "ADI Action Approved"


class NotConfigured(Exception):
    pass


class InProgressConflict(Exception):
    pass


def record_only(detail: dict) -> dict:
    return {"mode": "RECORD_ONLY", "external_call": False,
            "rollback_ref": "rb-" + hashlib.sha256(detail["idempotency_key"].encode()).hexdigest()[:16]}


def http_adapter(detail: dict) -> dict:
    raise NotConfigured("downstream system and reversal API are not defined yet (open question)")


ADAPTERS = {"RECORD_ONLY": record_only, "HTTP": http_adapter}


def _enabled() -> set[str]:
    return {a.strip() for a in aws.env("ENABLED_ACTIONS", "").split(",") if a.strip()}


def approved_actions(package: dict) -> list[str]:
    policy = package.get("candidate_policy") or {}
    return sorted({a for shares in policy.values() for a, w in shares.items() if a != "baseline" and w > 1e-9})


def emit(event, _context=None):
    decision = event["decision"]
    if decision.get("decision") != "approve":
        raise ValueError("emit is only valid after an approval")
    if decision.get("agent_status") != "PASSED":
        return {"emitted": 0, "reason": "the agent check did not pass; no production action"}
    package = aws.get_json(event["package_uri"])
    period = decision["effective_period"]
    entries = []
    for action in approved_actions(package):
        detail = {"decision_id": event["decision_id"], "action": action, "effective_period": period,
                  "idempotency_key": f"{event['decision_id']}#{action}#{period}",
                  "approver": decision["approver"], "policy": package["candidate_policy"],
                  "package_uri": event["package_uri"]}
        entries.append({"Source": SOURCE, "DetailType": DETAIL_TYPE, "EventBusName": aws.env("EVENT_BUS_NAME"),
                        "Detail": aws.dumps(detail)})
    if not entries:
        return {"emitted": 0, "reason": "the approved policy keeps every bucket at baseline"}
    resp = aws.client("events").put_events(Entries=entries)
    if resp.get("FailedEntryCount", 0):
        raise RuntimeError(f"{resp['FailedEntryCount']} action events were not accepted")
    return {"emitted": len(entries), "actions": approved_actions(package)}


def handler(event, _context=None):
    detail = event.get("detail") or {}
    missing = [k for k in REQUIRED if not detail.get(k)]
    if missing:
        raise ValueError(f"action event missing {missing}")
    if detail["action"] not in _enabled():
        audit(detail, "REFUSED_NOT_ENABLED", {})
        return {"status": "REFUSED_NOT_ENABLED"}

    idem = aws.table(aws.env("IDEMPOTENCY_TABLE"))
    key = {"idempotency_key": detail["idempotency_key"]}
    try:
        idem.put_item(Item={**key, "status": "IN_PROGRESS", "decision_id": detail["decision_id"],
                            "started_at": aws.now_iso()},
                      ConditionExpression="attribute_not_exists(idempotency_key) OR #s = :failed",
                      ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":failed": "FAILED"})
    except ClientError as e:
        if aws.error_code(e) != "ConditionalCheckFailedException":
            raise
        current = (idem.get_item(Key=key).get("Item") or {}).get("status")
        if current == "COMPLETED":
            audit(detail, "DUPLICATE_IGNORED", {})
            return {"status": "DUPLICATE_IGNORED"}
        raise InProgressConflict(f"{detail['idempotency_key']} is {current}; sent to the dead-letter queue for review")

    try:
        result = ADAPTERS[aws.env("DOWNSTREAM_MODE", "RECORD_ONLY")](detail)
    except Exception:
        idem.update_item(Key=key, UpdateExpression="SET #s = :f", ExpressionAttributeNames={"#s": "status"},
                         ExpressionAttributeValues={":f": "FAILED"})
        raise
    audit(detail, "EXECUTED", result)
    idem.update_item(Key=key, UpdateExpression="SET #s = :c, rollback_ref = :r",
                     ExpressionAttributeNames={"#s": "status"},
                     ExpressionAttributeValues={":c": "COMPLETED", ":r": result.get("rollback_ref", "")})
    return {"status": "EXECUTED", **result}


def audit(detail: dict, outcome: str, result: dict) -> None:
    ts = aws.now_iso()
    rec = {"decision_id": detail.get("decision_id", "unknown"),
           "event_ts": f"{ts}#{detail.get('action', '')}#{outcome}",
           "outcome": outcome, "action": detail.get("action"), "idempotency_key": detail.get("idempotency_key"),
           "effective_period": detail.get("effective_period"), "approver": detail.get("approver"),
           "result": result}
    aws.table(aws.env("AUDIT_TABLE")).put_item(Item=aws.to_dynamo(rec))
    key = f"actions/{rec['decision_id']}/{ts}-{detail.get('action', 'unknown')}-{outcome}.json"
    aws.put_json(f"s3://{aws.env('AUDIT_BUCKET')}/{key}", rec)   # Object Lock default retention applies
