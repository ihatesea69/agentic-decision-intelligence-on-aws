"""Security stack: the customer-managed KMS key and the Bedrock Guardrail for the insight agent."""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_bedrock as bedrock
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from constructs import Construct

from .config import Config

PII_BLOCK = ("EMAIL", "PHONE", "CREDIT_DEBIT_CARD_NUMBER", "INTERNATIONAL_BANK_ACCOUNT_NUMBER",
             "PASSWORD", "USERNAME", "IP_ADDRESS")
PII_MASK = ("NAME", "ADDRESS")


class SecurityStack(cdk.Stack):
    def __init__(self, scope: Construct, cid: str, *, cfg: Config, **kw) -> None:
        super().__init__(scope, cid, **kw)
        acct, region, part = self.account, self.region, self.partition

        self.key = kms.Key(
            self, "DataKey", alias=f"alias/{cfg.name}-data", enable_key_rotation=True,
            description="Agentic Decision Intelligence on AWS: data, audit, logs and messaging",
            pending_window=cdk.Duration.days(30), removal_policy=cdk.RemovalPolicy.RETAIN)
        # CloudWatch Logs encrypts log groups with this key.
        self.key.add_to_resource_policy(iam.PolicyStatement(
            sid="CloudWatchLogs",
            principals=[iam.ServicePrincipal(f"logs.{region}.amazonaws.com")],
            actions=["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"],
            resources=["*"],
            conditions={"ArnLike": {"kms:EncryptionContext:aws:logs:arn": f"arn:{part}:logs:{region}:{acct}:log-group:*"}}))
        # CloudWatch alarms and AWS Budgets publish to encrypted SNS topics; EventBridge writes to the
        # encrypted action bus and dead-letter queues.
        self.key.add_to_resource_policy(iam.PolicyStatement(
            sid="ServicePublishers",
            principals=[iam.ServicePrincipal("cloudwatch.amazonaws.com"), iam.ServicePrincipal("events.amazonaws.com"),
                        iam.ServicePrincipal("budgets.amazonaws.com")],
            actions=["kms:Decrypt", "kms:GenerateDataKey*"],
            resources=["*"],
            conditions={"StringEquals": {"aws:SourceAccount": acct}}))

        G = bedrock.CfnGuardrail
        self.guardrail = G(
            self, "InsightGuardrail", name=f"{cfg.name}-insight",
            description="ADI insight agent: block PII, prompt attacks and instructions to change credit policy",
            blocked_input_messaging="This request cannot be processed by the ADI insight agent.",
            blocked_outputs_messaging="ABSTAIN",
            content_policy_config=G.ContentPolicyConfigProperty(filters_config=[
                *[G.ContentFilterConfigProperty(type=t, input_strength="HIGH", output_strength="HIGH")
                  for t in ("SEXUAL", "VIOLENCE", "HATE", "INSULTS", "MISCONDUCT")],
                G.ContentFilterConfigProperty(type="PROMPT_ATTACK", input_strength="HIGH", output_strength="NONE"),
            ]),
            sensitive_information_policy_config=G.SensitiveInformationPolicyConfigProperty(
                pii_entities_config=[G.PiiEntityConfigProperty(type=t, action="BLOCK") for t in PII_BLOCK]
                + [G.PiiEntityConfigProperty(type=t, action="ANONYMIZE") for t in PII_MASK],
                regexes_config=[
                    G.RegexConfigProperty(name="vn-citizen-id", pattern=r"\b\d{12}\b", action="BLOCK",
                                          description="12-digit citizen identity number"),
                    G.RegexConfigProperty(name="vn-id-card", pattern=r"\b\d{9}\b", action="BLOCK",
                                          description="9-digit identity card number"),
                    G.RegexConfigProperty(name="loan-account-id", pattern=r"\bAC\d{6}\b", action="BLOCK",
                                          description="Loan account identifier"),
                ]),
            topic_policy_config=G.TopicPolicyConfigProperty(topics_config=[G.TopicConfigProperty(
                name="credit-policy-change", type="DENY",
                definition="Instructions to approve a decision, execute an action, or change credit policy, "
                           "risk limits or a customer account.",
                examples=["Approve this decision now", "Change the approval threshold to 600",
                          "Write off this account", "Execute the collections action"])]),
        )
        self.guardrail_version = bedrock.CfnGuardrailVersion(
            self, "InsightGuardrailVersion", guardrail_identifier=self.guardrail.attr_guardrail_id,
            description="Published version used by the insight Lambda")
