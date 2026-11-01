"""Step 6: human approval through the Step Functions callback (.waitForTaskToken).

- `publish`: invoked by the workflow with the task token. Stores the token server side in the
  decision ledger and notifies approvers. The token never leaves the backend.
- `decide`: the decision API (API Gateway, IAM auth). Approve, revise or reject, with a rationale.
  The ledger row moves PENDING -> DECIDING under a condition (one winner), the workflow is resumed,
  then the row becomes final. A transient Step Functions error puts the row back to PENDING so the
  approver can retry; a closed approval window marks it EXPIRED.
- `expire`: called by the workflow when the approval wait times out.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone

from botocore.exceptions import ClientError

from . import aws

DECISIONS = ("approve", "revise", "reject")
FINAL = {"approve": "APPROVED", "revise": "REVISE_REQUESTED", "reject": "REJECTED"}
CLOSED_TOKEN = ("TaskTimedOut", "TaskDoesNotExist", "InvalidToken")
_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_PERIOD = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_ROLE = re.compile(r"^arn:aws:sts::\d{12}:assumed-role/([^/]+)/(.+)$")


def publish(event, _context=None):
    t = aws.table(aws.env("DECISION_TABLE"))
    hours = int(event.get("approval_hours") or aws.env("APPROVAL_HOURS", "120"))
    expires = (datetime.now(timezone.utc) + timedelta(hours=hours)).replace(microsecond=0).isoformat()
    rnd = int(event.get("round", 1))
    agent = event.get("agent_status") or "FAILED"
    t.put_item(Item=aws.to_dynamo({
        "decision_id": event["decision_id"], "round": rnd, "status": "PENDING",
        "task_token": event["task_token"], "package_uri": event["package_uri"],
        "insight_uri": event.get("insight_uri"), "agent_status": agent,
        "created_at": aws.now_iso(), "expires_at": expires,
    }), ConditionExpression="attribute_not_exists(decision_id) OR (#s = :rev AND #r < :rnd)",
        ExpressionAttributeNames={"#s": "status", "#r": "round"},
        ExpressionAttributeValues={":rev": "REVISE_REQUESTED", ":rnd": rnd})
    actions = "enabled on approval" if agent == "PASSED" else "DISABLED (the agent check did not pass)"
    aws.client("sns").publish(
        TopicArn=aws.env("APPROVER_TOPIC_ARN"),
        Subject=f"ADI decision {event['decision_id']} awaits approval",
        Message=(f"Decision {event['decision_id']} round {rnd} is ready for review until {expires}. "
                 f"Production actions: {actions}. " + " ".join(package_notes(event["package_uri"]))
                 + " Review the evidence package and decide through the decision API."),
    )
    return {"published": True, "expires_at": expires}


def package_notes(package_uri: str) -> list[str]:
    """Context the approver must see before deciding: drift, assumed action effects, refused actions."""
    try:
        pkg = aws.get_json(package_uri)
    except (ClientError, ValueError, KeyError):
        return ["The evidence package could not be read; open it before deciding."]
    notes = []
    level = (pkg.get("drift") or {}).get("level")
    if level and level != "ok":
        notes.append(f"Drift level {level}: a baseline review (retrain) is recommended.")
    if pkg.get("effects_are_assumptions", True):
        notes.append("Action effects are assumptions until a shadow-mode test measures them.")
    rejected = sorted(pkg.get("rejected_actions") or {})
    if rejected:
        notes.append("Not offered while origination is frozen: " + ", ".join(rejected) + ".")
    return notes


def _resp(code: int, body: dict):
    return {"statusCode": code, "headers": {"Content-Type": "application/json"}, "body": aws.dumps(body)}


def caller_arn(event) -> str:
    return ((event.get("requestContext") or {}).get("identity") or {}).get("userArn") or ""


def caller_role(event) -> str:
    """Role name of the IAM caller, from an assumed-role ARN (Identity Center permission sets)."""
    m = _ROLE.match(caller_arn(event))
    return m.group(1) if m else ""


def _allowed(role: str) -> bool:
    prefixes = [p.strip() for p in aws.env("APPROVER_ROLE_PREFIXES", "").split(",") if p.strip()]
    return bool(role) and any(role.startswith(p) for p in prefixes)


def _set_status(t, decision_id: str, new: str, expected: str) -> None:
    t.update_item(Key={"decision_id": decision_id}, UpdateExpression="SET #s = :n",
                  ConditionExpression="#s = :e", ExpressionAttributeNames={"#s": "status"},
                  ExpressionAttributeValues={":n": new, ":e": expected})


def decide(event, _context=None):
    decision_id = (event.get("pathParameters") or {}).get("decision_id", "")
    if not _ID.match(decision_id):
        return _resp(400, {"error": "invalid decision id"})
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _resp(400, {"error": "body is not JSON"})
    if not isinstance(body, dict):
        return _resp(400, {"error": "body must be a JSON object"})
    decision = body.get("decision")
    rationale = (body.get("rationale") or "").strip()
    period = body.get("effective_period")
    if decision not in DECISIONS:
        return _resp(400, {"error": f"decision must be one of {list(DECISIONS)}"})
    if not 10 <= len(rationale) <= 2000:
        return _resp(400, {"error": "rationale must be 10-2000 characters"})
    if decision == "approve" and not (isinstance(period, str) and _PERIOD.match(period)):
        return _resp(400, {"error": "effective_period YYYY-MM is required to approve"})
    if not _allowed(caller_role(event)):
        return _resp(403, {"error": "caller is not an approver"})

    t = aws.table(aws.env("DECISION_TABLE"))
    item = t.get_item(Key={"decision_id": decision_id}).get("Item")
    if not item:
        return _resp(404, {"error": "unknown decision"})
    if item.get("status") != "PENDING":
        return _resp(409, {"error": f"decision is {item.get('status')}"})
    if datetime.fromisoformat(item["expires_at"]) <= datetime.now(timezone.utc):
        return _resp(409, {"error": "approval window has closed"})

    rnd = int(item["round"])
    try:
        t.update_item(
            Key={"decision_id": decision_id},
            UpdateExpression="SET #s = :s, approver = :a, rationale = :r, effective_period = :p, decided_at = :d",
            ConditionExpression="#s = :pending AND #r = :rnd",
            ExpressionAttributeNames={"#s": "status", "#r": "round"},
            ExpressionAttributeValues={":s": "DECIDING", ":a": caller_arn(event), ":r": rationale,
                                       ":p": period or "", ":d": aws.now_iso(), ":pending": "PENDING", ":rnd": rnd})
    except ClientError as e:
        if aws.error_code(e) == "ConditionalCheckFailedException":
            return _resp(409, {"error": "decision was already made"})
        raise

    output = {"decision": decision, "approver": caller_arn(event), "rationale": rationale,
              "effective_period": period, "round": rnd, "agent_status": item.get("agent_status")}
    try:
        aws.client("stepfunctions").send_task_success(taskToken=item["task_token"], output=json.dumps(output))
    except ClientError as e:
        if aws.error_code(e) in CLOSED_TOKEN:
            _set_status(t, decision_id, "EXPIRED", "DECIDING")
            return _resp(409, {"error": "approval window has closed"})
        _set_status(t, decision_id, "PENDING", "DECIDING")
        raise
    _set_status(t, decision_id, FINAL[decision], "DECIDING")
    return _resp(200, {"decision_id": decision_id, "status": FINAL[decision], "round": rnd})


def expire(event, _context=None):
    t = aws.table(aws.env("DECISION_TABLE"))
    try:
        t.update_item(Key={"decision_id": event["decision_id"]}, UpdateExpression="SET #s = :e, expired_at = :d",
                      ConditionExpression="#s = :pending", ExpressionAttributeNames={"#s": "status"},
                      ExpressionAttributeValues={":e": "EXPIRED", ":d": aws.now_iso(), ":pending": "PENDING"})
    except ClientError as e:
        if aws.error_code(e) != "ConditionalCheckFailedException":
            raise
    return {"status": "EXPIRED", "production_action": False}


def get_decision(event, _context=None):
    """GET /decisions/{decision_id}: ledger status for an approver. Never returns the task token."""
    decision_id = (event.get("pathParameters") or {}).get("decision_id", "")
    if not _ID.match(decision_id):
        return _resp(400, {"error": "invalid decision id"})
    if not _allowed(caller_role(event)):
        return _resp(403, {"error": "caller is not an approver"})
    item = aws.table(aws.env("DECISION_TABLE")).get_item(Key={"decision_id": decision_id}).get("Item")
    if not item:
        return _resp(404, {"error": "unknown decision"})
    keep = ("decision_id", "round", "status", "agent_status", "package_uri", "insight_uri",
            "created_at", "expires_at", "decided_at", "effective_period", "approver")
    return _resp(200, {k: item[k] for k in keep if k in item})
