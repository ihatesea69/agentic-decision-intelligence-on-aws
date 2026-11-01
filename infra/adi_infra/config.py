"""Deployment configuration from CDK context (cdk.json, overridable with -c key=value)."""
from __future__ import annotations

import os
from dataclasses import dataclass

import aws_cdk as cdk

PLACEHOLDER_MODEL = "REPLACE_WITH_BEDROCK_MODEL_ID"


@dataclass(frozen=True)
class Config:
    stage: str
    region: str
    account: str | None
    availability_zones: list
    vpc_cidr: str
    model_id: str
    approver_role_prefixes: str
    approval_hours: int
    max_revisions: int
    mc_chunks: int
    enabled_actions: str
    alert_email: str
    monthly_budget_usd: float
    audit_lock_mode: str
    audit_retention_days: int
    redshift_base_rpu: int
    redshift_evr: bool
    enable_residency: bool
    hanoi_zone: str
    residency_cidr: str

    @property
    def prod(self) -> bool:
        return self.stage == "prod"

    @property
    def name(self) -> str:
        return f"adi-{self.stage}"


def _bool(v) -> bool:
    return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes")


def _list(v) -> list:
    return list(v) if isinstance(v, (list, tuple)) else [x.strip() for x in str(v).split(",") if x.strip()]


def load(app: cdk.App) -> Config:
    def g(key, default=None):
        v = app.node.try_get_context(key)
        return default if v is None or v == "" else v

    cfg = Config(
        stage=str(g("stage", "dev")),
        region=str(g("region", "ap-southeast-1")),
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        availability_zones=_list(g("availabilityZones", ["ap-southeast-1a", "ap-southeast-1b"])),
        vpc_cidr=str(g("vpcCidr", "10.40.0.0/16")),
        model_id=str(g("modelId", PLACEHOLDER_MODEL)),
        approver_role_prefixes=str(g("approverRolePrefixes", "AWSReservedSSO_ADI-Approver")),
        approval_hours=int(g("approvalHours", 120)),
        max_revisions=int(g("maxRevisions", 2)),
        mc_chunks=int(g("mcChunks", 4)),
        enabled_actions=str(g("enabledActions", "intensify_collections,restructure")),
        alert_email=str(g("alertEmail", "")),
        monthly_budget_usd=float(g("monthlyBudgetUsd", 500)),
        audit_lock_mode=str(g("auditLockMode", "GOVERNANCE")).upper(),
        audit_retention_days=int(g("auditRetentionDays", 30)),
        redshift_base_rpu=int(g("redshiftBaseRpu", 8)),
        redshift_evr=_bool(g("redshiftEnhancedVpcRouting", False)),
        enable_residency=_bool(g("enableResidency", False)),
        hanoi_zone=str(g("hanoiZone", "ap-southeast-1-han-1a")),
        residency_cidr=str(g("residencyCidr", "10.40.200.0/24")),
    )
    validate(cfg)
    return cfg


def validate(cfg: Config) -> None:
    if cfg.stage not in ("dev", "test", "prod"):
        raise ValueError("stage must be dev, test or prod")
    if cfg.audit_lock_mode not in ("GOVERNANCE", "COMPLIANCE"):
        raise ValueError("auditLockMode must be GOVERNANCE or COMPLIANCE")
    if cfg.prod and cfg.audit_lock_mode != "COMPLIANCE":
        raise ValueError("prod needs auditLockMode=COMPLIANCE (immutable audit evidence)")
    if cfg.prod and cfg.audit_retention_days < 365:
        raise ValueError("prod needs auditRetentionDays set from the retention NFR (at least 365)")
    if cfg.prod and cfg.model_id == PLACEHOLDER_MODEL:
        raise ValueError("prod needs -c modelId=<Bedrock model or inference profile id>")
    if cfg.prod and len(cfg.availability_zones) < 3 and cfg.redshift_evr:
        raise ValueError("Redshift Serverless with enhanced VPC routing needs three Availability Zones")
    if not 1 <= cfg.mc_chunks <= 100:
        raise ValueError("mcChunks must be 1-100")
    if not 1 <= cfg.approval_hours <= 24 * 60:
        raise ValueError("approvalHours must be 1-1440")
    if cfg.region != "ap-southeast-1" and cfg.enable_residency:
        raise ValueError("the Hanoi Local Zone hangs off ap-southeast-1")


def inference_profile(model_id: str) -> tuple[bool, str]:
    """('apac.x.y', ...) is a cross-Region inference profile; returns (is_profile, base model id)."""
    head, _, rest = model_id.partition(".")
    if head in ("apac", "global", "us", "eu", "jp", "au") and rest:
        return True, rest
    return False, model_id
