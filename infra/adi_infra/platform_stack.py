"""Platform stack: workflow, compute, decision API, controlled actions, governance jobs, operations."""
from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_events as events
from aws_cdk import aws_events_targets as targets
from aws_cdk import aws_iam as iam
from aws_cdk import aws_sns as sns
from aws_cdk import aws_sns_subscriptions as subs
from aws_cdk import aws_sqs as sqs
from constructs import Construct

from . import grants
from .api import DecisionApi
from .compute import Compute
from .config import Config, inference_profile
from .lambdas import LambdaFactory
from .ops import Operations
from .workflow import Workflow


class PlatformStack(cdk.Stack):
    def __init__(self, scope: Construct, cid: str, *, cfg: Config, sec, net, data, root: Path,
                 lambda_bundle: Path, **kw) -> None:
        super().__init__(scope, cid, **kw)
        key, vpc = sec.key, net.vpc
        self.alerts = self._topic("Alerts", "ADI alerts", key, cfg.alert_email)
        self.approvers = self._topic("Approvers", "ADI approvers", key)
        self.dpo = self._topic("Dpo", "ADI data protection officer", key)

        sg = ec2.SecurityGroup(self, "ComputeSg", vpc=vpc, allow_all_outbound=False,
                               description="ADI Lambda, Batch and SageMaker: HTTPS to VPC endpoints only")
        sg.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS; isolated subnets reach only VPC endpoints")
        lf = LambdaFactory(self, bundle=lambda_bundle, vpc=vpc, subnets=net.private, security_group=sg, key=key)
        fns = self._workflow_functions(cfg, lf, sec, data)
        action_fn, dlq = self._actions(cfg, lf, key, data, fns)

        compute = Compute(self, "Compute", cfg=cfg, root=root, vpc=vpc, subnets=net.private, sg=sg, key=key, data=data)
        subnet_ids = vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED).subnet_ids
        self.workflow = Workflow(self, "Workflow", cfg=cfg, key=key, data=data, compute=compute, fns=fns,
                                 alerts=self.alerts, subnet_ids=subnet_ids, sg_id=sg.security_group_id)
        sm = self.workflow.state_machine

        env = {"DECISION_TABLE": data.decisions.table_name, "APPROVER_ROLE_PREFIXES": cfg.approver_role_prefixes}
        decide = lf.make("Decide", "adi_services.approval.decide", env=env)
        data.decisions.grant_read_write_data(decide)
        sm.grant_task_response(decide)
        get = lf.make("GetDecision", "adi_services.approval.get_decision", env=env)
        data.decisions.grant_read_data(get)
        self.api = DecisionApi(self, "DecisionApi", cfg=cfg, key=key, decide_fn=decide, get_fn=get)

        tia = lf.make("TiaReview", "adi_services.tia_review.handler",
                      env={"TIA_TABLE": data.tia.table_name, "DPO_TOPIC_ARN": self.dpo.topic_arn})
        data.tia.grant_read_write_data(tia)
        self.dpo.grant_publish(tia)
        Operations(self, "Ops", cfg=cfg, key=key, state_machine=sm, alerts=self.alerts, action_dlq=dlq,
                   action_fn=action_fn, tia_fn=tia, insight_fn=fns["insight"])
        self._outputs(cfg, data, sm)

    def _topic(self, cid: str, name: str, key, email: str = "") -> sns.Topic:
        t = sns.Topic(self, cid, display_name=name, master_key=key, enforce_ssl=True)
        if email:
            t.add_subscription(subs.EmailSubscription(email))
        return t

    def _workflow_functions(self, cfg: Config, lf: LambdaFactory, sec, data) -> dict:
        fns = {}
        fns["run"] = lf.make("RunContext", "adi_services.run.handler", env={
            "CURATED_BUCKET": data.curated.bucket_name, "INGEST_BUCKET": data.ingest.bucket_name,
            "CONFIG_PREFIX": f"s3://{data.curated.bucket_name}/config"})

        fns["insight"] = lf.make("Insight", "adi_services.insight.handler", timeout=120, memory=512, env={
            "MODEL_ID": cfg.model_id, "GUARDRAIL_ID": sec.guardrail.attr_guardrail_id,
            "GUARDRAIL_VERSION": sec.guardrail_version.attr_version})
        grants.read_write(fns["insight"], data.curated, "runs/", sec.key)
        profile, base = inference_profile(cfg.model_id)
        models = [f"arn:{self.partition}:bedrock:{'*' if profile else self.region}::foundation-model/{base}"]
        if profile:
            models.append(f"arn:{self.partition}:bedrock:{self.region}:{self.account}:inference-profile/{cfg.model_id}")
        fns["insight"].add_to_role_policy(iam.PolicyStatement(sid="InvokeModel", actions=["bedrock:InvokeModel"],
                                                              resources=models))
        fns["insight"].add_to_role_policy(iam.PolicyStatement(sid="ApplyGuardrail", actions=["bedrock:ApplyGuardrail"],
                                                              resources=[sec.guardrail.attr_guardrail_arn]))

        fns["publish"] = lf.make("PublishApproval", "adi_services.approval.publish", env={
            "DECISION_TABLE": data.decisions.table_name, "APPROVER_TOPIC_ARN": self.approvers.topic_arn,
            "APPROVAL_HOURS": str(cfg.approval_hours)})
        data.decisions.grant_read_write_data(fns["publish"])
        self.approvers.grant_publish(fns["publish"])
        grants.read(fns["publish"], data.curated, "runs/", sec.key)

        fns["expire"] = lf.make("ExpireDecision", "adi_services.approval.expire",
                                env={"DECISION_TABLE": data.decisions.table_name})
        data.decisions.grant_read_write_data(fns["expire"])
        return fns

    def _actions(self, cfg: Config, lf: LambdaFactory, key, data, fns: dict):
        """Step 7: the workflow emits one event per approved action onto an encrypted bus; the handler
        is idempotent and every failure ends in the dead-letter queue for a human."""
        bus = events.EventBus(self, "ActionBus", event_bus_name=f"{cfg.name}-actions", kms_key=key)
        fns["emit"] = lf.make("EmitActions", "adi_services.actions.emit", env={"EVENT_BUS_NAME": bus.event_bus_name})
        bus.grant_put_events_to(fns["emit"])
        key.grant(fns["emit"], "kms:GenerateDataKey", "kms:Decrypt")
        grants.read(fns["emit"], data.curated, "runs/", key)

        dlq = sqs.Queue(self, "ActionDlq", encryption=sqs.QueueEncryption.KMS, encryption_master_key=key,
                        enforce_ssl=True, retention_period=cdk.Duration.days(14))
        action = lf.make("ActionHandler", "adi_services.actions.handler", timeout=60, env={
            "ENABLED_ACTIONS": cfg.enabled_actions, "IDEMPOTENCY_TABLE": data.idempotency.table_name,
            "AUDIT_TABLE": data.audit_table.table_name, "AUDIT_BUCKET": data.audit.bucket_name,
            "DOWNSTREAM_MODE": "RECORD_ONLY"}, dead_letter_queue=dlq, retry_attempts=2,
            reserved_concurrent_executions=5)
        data.idempotency.grant_read_write_data(action)
        data.audit_table.grant_write_data(action)
        grants.write(action, data.audit, "actions/", key)
        events.Rule(self, "ApprovedActions", event_bus=bus, description="Approved ADI actions to the action handler",
                    event_pattern=events.EventPattern(source=["adi.decisions"], detail_type=["ADI Action Approved"]),
                    targets=[targets.LambdaFunction(action, dead_letter_queue=dlq, retry_attempts=2,
                                                    max_event_age=cdk.Duration.hours(24))])
        return action, dlq

    def _outputs(self, cfg: Config, data, sm) -> None:
        out = {
            "StateMachineArn": sm.state_machine_arn,
            "DecisionApiUrl": self.api.api.url,
            "DecisionApiExecuteArn": self.api.api.arn_for_execute_api(),
            "IngestBucket": data.ingest.bucket_name,
            "CuratedBucket": data.curated.bucket_name,
            "ConfigPrefix": f"s3://{data.curated.bucket_name}/config",
            "AuditBucket": data.audit.bucket_name,
            "DecisionTable": data.decisions.table_name,
            "TiaTable": data.tia.table_name,
            "AlertTopicArn": self.alerts.topic_arn,
            "ApproverTopicArn": self.approvers.topic_arn,
            "DpoTopicArn": self.dpo.topic_arn,
        }
        for k, v in out.items():
            cdk.CfnOutput(self, k, value=v)
