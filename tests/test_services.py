import json
from datetime import datetime, timezone

import boto3
import pytest
from moto import mock_aws

from adi_engines import evidence as ev
from adi_services import actions, approval, aws, insight, run, tia_review

import cloud

APPROVER = "AWSReservedSSO_ADI-Approver_1a2b3c"
PKG_URI = "s3://adi-curated/runs/D1/package.json"


@pytest.fixture
def env(monkeypatch):
    for k, v in {"AWS_DEFAULT_REGION": cloud.REGION, "AWS_ACCESS_KEY_ID": "testing",
                 "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing", **cloud.ENV}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        aws.reset()
        s3 = cloud.make_buckets()
        cloud.make_tables()
        f = cloud.fakes()
        named = {"sns": f.sns, "stepfunctions": f.sfn, "events": f.events, "bedrock-runtime": f.bedrock}
        real = {}

        def client(name):
            if name in named:
                return named[name]
            if name not in real:
                real[name] = boto3.client(name, region_name=cloud.REGION)
            return real[name]

        monkeypatch.setattr(aws, "client", client)
        f.s3 = s3
        f.ddb = boto3.resource("dynamodb", region_name=cloud.REGION)
        yield f
        aws.reset()


def _package(**extra):
    figs = ev.figures_from("snapshot", "fact", {"rolled_forward": 96, "recovered": 45}, "count", 1)
    figs.append(ev.Figure("net_deterioration", "model_calculated", 0.051, "ratio", "markov", 1))
    pkg = ev.EvidencePackage("D1", "2026-07", figs).to_dict()
    pkg.update({"candidate_policy": {"B1": {"baseline": 0.2, "intensify_collections": 0.8},
                                     "B2": {"restructure": 1.0}, "B0": {"baseline": 1.0}},
                "drift": {"level": "alert"}, "effects_are_assumptions": True,
                "rejected_actions": {"tighten_approval": "frozen"}})
    pkg.update(extra)
    return pkg


def _put(env, uri, obj):
    aws.put_json(uri, obj)


def _decide_event(body, role=APPROVER, decision_id="D1"):
    return {"pathParameters": {"decision_id": decision_id}, "body": json.dumps(body),
            "requestContext": {"identity": {"userArn": f"arn:aws:sts::123456789012:assumed-role/{role}/approver@example.com"}}}


def _publish(env, rnd=1, hours=None, agent="PASSED"):
    e = {"decision_id": "D1", "task_token": f"token-{rnd}", "package_uri": PKG_URI, "round": rnd,
         "insight_uri": "s3://adi-curated/runs/D1/evidence/insight.json", "agent_status": agent}
    if hours is not None:
        e["approval_hours"] = hours
    return approval.publish(e)


APPROVE = {"decision": "approve", "rationale": "Collections capacity confirmed for August.", "effective_period": "2026-08"}


# ---- run context ---------------------------------------------------------------------------
def test_run_context_from_schedule_and_override(env):
    out = run.handler({"execution_name": "abc-123", "start_time": "2026-08-05T02:00:00.000Z",
                       "input": {"scheduled_time": "2026-08-05T02:00:00Z"}})
    assert out["snapshot_month"] == "2026-07" and out["round"] == 1
    assert out["run_prefix"] == "s3://adi-curated/runs/abc-123"
    assert out["source_prefix"] == "s3://adi-raw/deidentified/2026-07/"
    jan = run.handler({"execution_name": "x", "start_time": "2027-01-05T02:00:00Z", "input": {}})
    assert jan["snapshot_month"] == "2026-12"
    manual = run.handler({"execution_name": "x", "start_time": "2027-01-05T02:00:00Z",
                          "input": {"snapshot_month": "2026-07"}})
    assert manual["snapshot_month"] == "2026-07"
    with pytest.raises(ValueError):
        run.handler({"execution_name": "bad name", "start_time": "2026-08-05T00:00:00Z"})
    with pytest.raises(ValueError):
        run.handler({"execution_name": "x", "start_time": "2026-08-05T00:00:00Z", "input": {"snapshot_month": "2026-13"}})


# ---- approval --------------------------------------------------------------------------------
def test_publish_stores_token_server_side_and_notifies(env):
    _put(env, PKG_URI, _package())
    _publish(env)
    item = env.ddb.Table("decisions").get_item(Key={"decision_id": "D1"})["Item"]
    assert item["status"] == "PENDING" and item["task_token"] == "token-1"
    msg = env.sns.messages[0]["Message"]
    assert "token-1" not in msg
    assert "Drift level alert" in msg and "tighten_approval" in msg and "assumptions" in msg


def test_approve_resumes_the_workflow_once(env):
    _put(env, PKG_URI, _package())
    _publish(env)
    r = approval.decide(_decide_event(APPROVE))
    assert r["statusCode"] == 200 and json.loads(r["body"])["status"] == "APPROVED"
    assert env.sfn.calls[0]["token"] == "token-1"
    out = env.sfn.calls[0]["output"]
    assert out["decision"] == "approve" and out["effective_period"] == "2026-08" and out["agent_status"] == "PASSED"
    again = approval.decide(_decide_event(APPROVE))
    assert again["statusCode"] == 409
    assert len(env.sfn.calls) == 1


@pytest.mark.parametrize("body,code", [
    ({"decision": "maybe", "rationale": "long enough text"}, 400),
    ({"decision": "reject", "rationale": "short"}, 400),
    ({"decision": "approve", "rationale": "long enough rationale"}, 400),
    ({"decision": "approve", "rationale": "long enough rationale", "effective_period": "2026-13"}, 400),
])
def test_invalid_decisions_are_refused(env, body, code):
    _put(env, PKG_URI, _package())
    _publish(env)
    assert approval.decide(_decide_event(body))["statusCode"] == code
    assert env.sfn.calls == []


def test_non_approver_and_unknown_decision(env):
    _put(env, PKG_URI, _package())
    _publish(env)
    assert approval.decide(_decide_event(APPROVE, role="AWSReservedSSO_ReadOnly_9z"))["statusCode"] == 403
    assert approval.decide(_decide_event(APPROVE, decision_id="NOPE"))["statusCode"] == 404
    assert approval.decide(_decide_event(APPROVE, decision_id="bad id!"))["statusCode"] == 400


def test_closed_window_and_closed_token(env):
    _put(env, PKG_URI, _package())
    _publish(env, hours=-1)
    assert approval.decide(_decide_event(APPROVE))["statusCode"] == 409
    env.ddb.Table("decisions").delete_item(Key={"decision_id": "D1"})
    _publish(env)
    env.sfn.error = "TaskTimedOut"
    assert approval.decide(_decide_event(APPROVE))["statusCode"] == 409
    assert env.ddb.Table("decisions").get_item(Key={"decision_id": "D1"})["Item"]["status"] == "EXPIRED"


def test_transient_error_lets_the_approver_retry(env):
    _put(env, PKG_URI, _package())
    _publish(env)
    env.sfn.error = "ServiceUnavailable"
    with pytest.raises(Exception):
        approval.decide(_decide_event(APPROVE))
    assert env.ddb.Table("decisions").get_item(Key={"decision_id": "D1"})["Item"]["status"] == "PENDING"
    env.sfn.error = None
    assert approval.decide(_decide_event(APPROVE))["statusCode"] == 200


def test_revise_opens_a_new_round_only_once(env):
    _put(env, PKG_URI, _package())
    _publish(env)
    r = approval.decide(_decide_event({"decision": "revise", "rationale": "Re-optimize with lower capacity."}))
    assert json.loads(r["body"])["status"] == "REVISE_REQUESTED"
    _publish(env, rnd=2)
    item = env.ddb.Table("decisions").get_item(Key={"decision_id": "D1"})["Item"]
    assert item["status"] == "PENDING" and int(item["round"]) == 2
    with pytest.raises(Exception):
        _publish(env, rnd=2)


def test_get_decision_never_returns_the_token(env):
    _put(env, PKG_URI, _package())
    _publish(env)
    r = approval.get_decision(_decide_event({}))
    body = json.loads(r["body"])
    assert r["statusCode"] == 200 and body["status"] == "PENDING" and "task_token" not in body
    assert approval.get_decision(_decide_event({}, role="Other"))["statusCode"] == 403


def test_expire_only_touches_pending(env):
    _put(env, PKG_URI, _package())
    _publish(env)
    assert approval.expire({"decision_id": "D1"})["production_action"] is False
    assert env.ddb.Table("decisions").get_item(Key={"decision_id": "D1"})["Item"]["status"] == "EXPIRED"
    approval.expire({"decision_id": "D1"})  # second call is a no-op


# ---- actions ---------------------------------------------------------------------------------
def _approved(agent="PASSED", decision="approve"):
    return {"decision_id": "D1", "package_uri": PKG_URI,
            "decision": {"decision": decision, "approver": "arn:aws:sts::123456789012:assumed-role/x/y",
                         "effective_period": "2026-08", "agent_status": agent}}


def test_emit_one_event_per_approved_action(env):
    _put(env, PKG_URI, _package())
    out = actions.emit(_approved())
    assert out == {"emitted": 2, "actions": ["intensify_collections", "restructure"]}
    keys = [json.loads(e["Detail"])["idempotency_key"] for e in env.events.entries]
    assert keys == ["D1#intensify_collections#2026-08", "D1#restructure#2026-08"]
    assert all(e["EventBusName"] == "adi-actions" for e in env.events.entries)


def test_no_production_action_when_the_agent_check_failed(env):
    _put(env, PKG_URI, _package())
    assert actions.emit(_approved(agent="FAILED"))["emitted"] == 0
    assert env.events.entries == []
    with pytest.raises(ValueError):
        actions.emit(_approved(decision="reject"))


def _action_event(action="intensify_collections"):
    return {"detail": {"decision_id": "D1", "action": action, "effective_period": "2026-08",
                       "idempotency_key": f"D1#{action}#2026-08", "approver": "arn:x"}}


def test_action_runs_once_and_is_audited(env, monkeypatch):
    monkeypatch.setenv("ENABLED_ACTIONS", "intensify_collections")
    first = actions.handler(_action_event())
    assert first["status"] == "EXECUTED" and first["external_call"] is False
    assert actions.handler(_action_event())["status"] == "DUPLICATE_IGNORED"
    marker = env.ddb.Table("idempotency").get_item(Key={"idempotency_key": "D1#intensify_collections#2026-08"})["Item"]
    assert marker["status"] == "COMPLETED" and marker["rollback_ref"].startswith("rb-")
    outcomes = sorted(i["outcome"] for i in env.ddb.Table("audit").scan()["Items"])
    assert outcomes == ["DUPLICATE_IGNORED", "EXECUTED"]
    assert len(env.s3.list_objects_v2(Bucket="adi-audit", Prefix="actions/D1/")["Contents"]) == 2


def test_disabled_action_is_refused_and_audited(env, monkeypatch):
    monkeypatch.setenv("ENABLED_ACTIONS", "intensify_collections")
    assert actions.handler(_action_event("tighten_approval"))["status"] == "REFUSED_NOT_ENABLED"
    assert env.ddb.Table("audit").scan()["Items"][0]["outcome"] == "REFUSED_NOT_ENABLED"


def test_in_progress_key_goes_to_the_dead_letter_queue(env, monkeypatch):
    monkeypatch.setenv("ENABLED_ACTIONS", "intensify_collections")
    env.ddb.Table("idempotency").put_item(Item={"idempotency_key": "D1#intensify_collections#2026-08",
                                                "status": "IN_PROGRESS"})
    with pytest.raises(actions.InProgressConflict):
        actions.handler(_action_event())


def test_failed_attempt_can_be_retried(env, monkeypatch):
    monkeypatch.setenv("ENABLED_ACTIONS", "intensify_collections")
    monkeypatch.setenv("DOWNSTREAM_MODE", "HTTP")
    with pytest.raises(actions.NotConfigured):
        actions.handler(_action_event())
    marker = env.ddb.Table("idempotency").get_item(Key={"idempotency_key": "D1#intensify_collections#2026-08"})["Item"]
    assert marker["status"] == "FAILED"
    monkeypatch.setenv("DOWNSTREAM_MODE", "RECORD_ONLY")
    assert actions.handler(_action_event())["status"] == "EXECUTED"


def test_action_event_must_be_complete(env, monkeypatch):
    monkeypatch.setenv("ENABLED_ACTIONS", "intensify_collections")
    with pytest.raises(ValueError):
        actions.handler({"detail": {"action": "intensify_collections"}})


# ---- insight ---------------------------------------------------------------------------------
def test_grounded_narrative_passes(env):
    _put(env, PKG_URI, _package())
    env.bedrock.text = "[fact] 96 accounts rolled forward and 45 recovered. [model_calculated] Net migration 5.1%."
    out = insight.handler({"package_uri": PKG_URI, "evidence_prefix": "s3://adi-curated/runs/D1/evidence"})
    assert out["agent_status"] == "PASSED"
    stored = aws.get_json(out["insight_uri"])
    assert stored["narrative"].startswith("[fact]")
    call = env.bedrock.calls[0]
    assert call["guardrailConfig"]["guardrailIdentifier"] == "gr-test"
    assert call["inferenceConfig"]["temperature"] == 0


def test_invented_number_fails_and_is_not_shown(env):
    _put(env, PKG_URI, _package())
    env.bedrock.text = "[model_calculated] Net migration 6.1% this month."
    out = insight.handler({"package_uri": PKG_URI, "evidence_prefix": "s3://adi-curated/runs/D1/evidence"})
    stored = aws.get_json(out["insight_uri"])
    assert out["agent_status"] == "FAILED" and stored["narrative"] is None
    assert stored["grounding"]["unmatched_numbers"] == ["6.1"]


def test_guardrail_intervention_and_incomplete_package_abstain(env):
    _put(env, PKG_URI, _package())
    env.bedrock.stop = "guardrail_intervened"
    env.bedrock.text = "Sorry."
    assert insight.handler({"package_uri": PKG_URI, "evidence_prefix": "s3://adi-curated/e"})["agent_status"] == "ABSTAINED"
    _put(env, PKG_URI, _package(complete=False, missing=["mdp"]))
    n = len(env.bedrock.calls)
    assert insight.handler({"package_uri": PKG_URI, "evidence_prefix": "s3://adi-curated/e"})["agent_status"] == "ABSTAINED"
    assert len(env.bedrock.calls) == n  # the model is not called for an incomplete package


# ---- TIA review ------------------------------------------------------------------------------
def test_tia_review_closes_the_gate_when_overdue(env):
    t = env.ddb.Table("tia")
    t.put_item(Item={"tia_id": "fresh", "prepared_at": "2026-07-01T00:00:00+00:00", "status": "approved", "gate_open": True})
    t.put_item(Item={"tia_id": "due", "prepared_at": "2026-02-20T00:00:00+00:00", "status": "approved", "gate_open": True})
    t.put_item(Item={"tia_id": "late", "prepared_at": "2025-12-01T00:00:00+00:00", "status": "approved", "gate_open": True})
    counts = tia_review.handler(now=datetime(2026, 8, 1, tzinfo=timezone.utc))
    assert counts == {"OK": 1, "DUE_SOON": 1, "OVERDUE": 1}
    assert t.get_item(Key={"tia_id": "late"})["Item"]["gate_open"] is False
    assert t.get_item(Key={"tia_id": "due"})["Item"]["gate_open"] is True
    assert len(env.sns.messages) == 2
