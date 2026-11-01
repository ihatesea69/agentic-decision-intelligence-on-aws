"""Lambda factory: one pure-Python bundle (adi_engines + adi_services), one handler per function.
Every function runs in the isolated subnets, logs JSON to a CMK-encrypted log group, ARM64."""
from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_kms as kms
from aws_cdk import aws_lambda as lambda_
from aws_cdk import aws_logs as logs
from constructs import Construct


class LambdaFactory:
    def __init__(self, scope: Construct, *, bundle: Path, vpc: ec2.IVpc, subnets: ec2.SubnetSelection,
                 security_group: ec2.ISecurityGroup, key: kms.IKey) -> None:
        self.scope, self.vpc, self.subnets, self.sg, self.key = scope, vpc, subnets, security_group, key
        self.code = lambda_.Code.from_asset(str(bundle))

    def make(self, cid: str, handler: str, *, env: dict | None = None, timeout: int = 30,
             memory: int = 256, **extra) -> lambda_.Function:
        log_group = logs.LogGroup(self.scope, f"{cid}Logs", encryption_key=self.key,
                                  retention=logs.RetentionDays.ONE_YEAR, removal_policy=cdk.RemovalPolicy.RETAIN)
        return lambda_.Function(
            self.scope, cid, runtime=lambda_.Runtime.PYTHON_3_14, architecture=lambda_.Architecture.ARM_64,
            handler=handler, code=self.code, timeout=cdk.Duration.seconds(timeout), memory_size=memory,
            environment=env or {}, vpc=self.vpc, vpc_subnets=self.subnets, security_groups=[self.sg],
            log_group=log_group, logging_format=lambda_.LoggingFormat.JSON, **extra)
