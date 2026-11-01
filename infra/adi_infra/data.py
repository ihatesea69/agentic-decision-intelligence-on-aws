"""Data stack (stateful): S3 buckets, DynamoDB tables, Redshift Serverless portfolio mart.

Buckets: ingest (de-identified arrivals from the Hanoi tokenizer), curated (runs, config, mart
input), audit (S3 Object Lock, WORM evidence), access-logs. Tables: decision ledger, idempotency
markers, audit trail, TIA register. All encrypted with the customer-managed key; RETAIN on delete.
"""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_dynamodb as ddb
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_kms as kms
from aws_cdk import aws_redshiftserverless as rss
from aws_cdk import aws_s3 as s3
from constructs import Construct

from . import grants
from .config import Config
from .network import NetworkStack

RETAIN = cdk.RemovalPolicy.RETAIN


class DataStack(cdk.Stack):
    def __init__(self, scope: Construct, cid: str, *, cfg: Config, key: kms.IKey, net: NetworkStack, **kw) -> None:
        super().__init__(scope, cid, **kw)
        self.key = key
        self.access_logs = s3.Bucket(
            self, "AccessLogs", encryption=s3.BucketEncryption.S3_MANAGED, enforce_ssl=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, versioned=True,
            object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED, removal_policy=RETAIN,
            lifecycle_rules=[s3.LifecycleRule(expiration=cdk.Duration.days(400),
                                              noncurrent_version_expiration=cdk.Duration.days(30))])
        self.ingest = self._bucket("Ingest", "ingest/")
        self.curated = self._bucket("Curated", "curated/")
        retention = cdk.Duration.days(cfg.audit_retention_days)
        lock = (s3.ObjectLockRetention.compliance(retention) if cfg.audit_lock_mode == "COMPLIANCE"
                else s3.ObjectLockRetention.governance(retention))
        self.audit = self._bucket("Audit", "audit/", object_lock_enabled=True, object_lock_default_retention=lock)

        self.decisions = self._table("Decisions", "decision_id", cfg)
        self.idempotency = self._table("Idempotency", "idempotency_key", cfg)
        self.audit_table = self._table("AuditTrail", "decision_id", cfg, sort_key="event_ts")
        self.tia = self._table("TiaRegister", "tia_id", cfg)

        self._redshift(cfg, net)

    def _bucket(self, cid: str, log_prefix: str, **extra) -> s3.Bucket:
        return s3.Bucket(
            self, cid, encryption=s3.BucketEncryption.KMS, encryption_key=self.key, bucket_key_enabled=True,
            block_public_access=s3.BlockPublicAccess.BLOCK_ALL, enforce_ssl=True, minimum_tls_version=1.2,
            versioned=True, object_ownership=s3.ObjectOwnership.BUCKET_OWNER_ENFORCED,
            server_access_logs_bucket=self.access_logs, server_access_logs_prefix=log_prefix,
            removal_policy=RETAIN, **extra)

    def _table(self, cid: str, pk: str, cfg: Config, sort_key: str | None = None) -> ddb.Table:
        return ddb.Table(
            self, cid, partition_key=ddb.Attribute(name=pk, type=ddb.AttributeType.STRING),
            sort_key=ddb.Attribute(name=sort_key, type=ddb.AttributeType.STRING) if sort_key else None,
            billing_mode=ddb.BillingMode.PAY_PER_REQUEST,
            encryption=ddb.TableEncryption.CUSTOMER_MANAGED, encryption_key=self.key,
            point_in_time_recovery_specification=ddb.PointInTimeRecoverySpecification(point_in_time_recovery_enabled=True),
            deletion_protection=cfg.prod, removal_policy=RETAIN)

    def _redshift(self, cfg: Config, net: NetworkStack) -> None:
        """Portfolio mart for the QuickSight evidence pages. Loaded by the workflow with COPY."""
        self.redshift_sg = ec2.SecurityGroup(self, "RedshiftSg", vpc=net.vpc, allow_all_outbound=False,
                                             description="Redshift Serverless portfolio mart")
        self.redshift_sg.add_ingress_rule(ec2.Peer.ipv4(cfg.vpc_cidr), ec2.Port.tcp(5439), "SQL from the VPC")
        self.redshift_sg.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443),
                                         "HTTPS; isolated subnets reach only VPC endpoints")
        self.copy_role = iam.Role(
            self, "RedshiftCopyRole", description="Redshift Serverless COPY from the curated bucket",
            assumed_by=iam.CompositePrincipal(iam.ServicePrincipal("redshift.amazonaws.com"),
                                              iam.ServicePrincipal("redshift-serverless.amazonaws.com")))
        grants.read(self.copy_role, self.curated, "runs/", self.key)
        ns = rss.CfnNamespace(
            self, "MartNamespace", namespace_name=cfg.name, db_name="adi", admin_username="adiadmin",
            manage_admin_password=True, admin_password_secret_kms_key_id=self.key.key_arn,
            kms_key_id=self.key.key_arn, iam_roles=[self.copy_role.role_arn],
            default_iam_role_arn=self.copy_role.role_arn,
            log_exports=["userlog", "connectionlog", "useractivitylog"])
        ns.apply_removal_policy(RETAIN)
        self.workgroup = rss.CfnWorkgroup(
            self, "MartWorkgroup", workgroup_name=cfg.name, namespace_name=cfg.name,
            base_capacity=cfg.redshift_base_rpu, publicly_accessible=False,
            enhanced_vpc_routing=cfg.redshift_evr,
            subnet_ids=net.vpc.select_subnets(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED).subnet_ids,
            security_group_ids=[self.redshift_sg.security_group_id],
            config_parameters=[rss.CfnWorkgroup.ConfigParameterProperty(parameter_key="require_ssl", parameter_value="true"),
                               rss.CfnWorkgroup.ConfigParameterProperty(parameter_key="enable_user_activity_logging",
                                                                        parameter_value="true")])
        self.workgroup.add_resource_dependency(ns)
        self.workgroup_name = cfg.name
        self.workgroup_arn = self.workgroup.attr_workgroup_workgroup_arn
