"""Compute for steps 1-4: Glue data-quality job, the engine image, SageMaker Processing role,
AWS Batch on Fargate Spot (with an on-demand fallback) for the Monte Carlo array job."""
from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_batch as batch
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr_assets as ecr_assets
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_glue as glue
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3_assets as s3_assets
from constructs import Construct

from . import grants
from .config import Config


class Compute(Construct):
    def __init__(self, scope: Construct, cid: str, *, cfg: Config, root: Path, vpc: ec2.IVpc,
                 subnets: ec2.SubnetSelection, sg: ec2.ISecurityGroup, key, data) -> None:
        super().__init__(scope, cid)
        stack = cdk.Stack.of(self)
        self.image = ecr_assets.DockerImageAsset(
            self, "EngineImage", directory=str(root), file="Dockerfile",
            platform=ecr_assets.Platform.LINUX_AMD64, exclude=["infra/cdk.out", "infra/.build", "**/cdk.out"])
        self._glue(cfg, root, vpc, data, key, stack)
        self._sagemaker(data, stack)
        self._batch(cfg, vpc, subnets, sg, data, key)

    def _glue(self, cfg: Config, root: Path, vpc: ec2.IVpc, data, key, stack: cdk.Stack) -> None:
        """Step 1: Python shell job (3.9) running adi_engines.dq inside the VPC (fail closed)."""
        self.glue_sg = ec2.SecurityGroup(self, "GlueSg", vpc=vpc, allow_all_outbound=False,
                                         description="Glue Python shell job in the isolated subnets")
        self.glue_sg.add_ingress_rule(self.glue_sg, ec2.Port.all_tcp(), "Glue needs a self-referencing rule")
        self.glue_sg.add_egress_rule(self.glue_sg, ec2.Port.all_tcp(), "Glue self-referencing egress")
        self.glue_sg.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443),
                                     "HTTPS; isolated subnets reach only VPC endpoints")
        subnet = vpc.isolated_subnets[0]
        conn_name = f"{cfg.name}-network"
        conn = glue.CfnConnection(
            self, "GlueNetwork", catalog_id=stack.account,
            connection_input=glue.CfnConnection.ConnectionInputProperty(
                name=conn_name, connection_type="NETWORK", connection_properties={},
                physical_connection_requirements=glue.CfnConnection.PhysicalConnectionRequirementsProperty(
                    availability_zone=subnet.availability_zone, subnet_id=subnet.subnet_id,
                    security_group_id_list=[self.glue_sg.security_group_id])))
        E = glue.CfnSecurityConfiguration
        sec = E(self, "GlueSecurity", name=f"{cfg.name}-glue", encryption_configuration=E.EncryptionConfigurationProperty(
            cloud_watch_encryption=E.CloudWatchEncryptionProperty(cloud_watch_encryption_mode="SSE-KMS", kms_key_arn=key.key_arn),
            job_bookmarks_encryption=E.JobBookmarksEncryptionProperty(job_bookmarks_encryption_mode="CSE-KMS", kms_key_arn=key.key_arn),
            s3_encryptions=[E.S3EncryptionProperty(s3_encryption_mode="SSE-KMS", kms_key_arn=key.key_arn)]))
        script = s3_assets.Asset(self, "GlueScript", path=str(root / "glue" / "adi_glue" / "build_snapshot.py"))
        engines = s3_assets.Asset(self, "EnginesZip", path=str(root / "engines"),
                                  exclude=["**/__pycache__", "**/*.pyc"])
        role = iam.Role(self, "GlueRole", assumed_by=iam.ServicePrincipal("glue.amazonaws.com"),
                        managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("service-role/AWSGlueServiceRole")])
        grants.read(role, data.ingest, "deidentified/", key)
        grants.write(role, data.curated, "runs/", key)
        # exactly the two asset objects, not the whole CDK asset bucket
        for asset in (script, engines):
            iam.Grant.add_to_principal(grantee=role, actions=["s3:GetObject"],
                                       resource_arns=[asset.bucket.arn_for_objects(asset.s3_object_key)])
        role.add_to_policy(iam.PolicyStatement(actions=["logs:AssociateKmsKey"], resources=[
            stack.format_arn(service="logs", resource="log-group", resource_name="/aws-glue/*",
                             arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME)]))
        self.glue_job_name = f"{cfg.name}-snapshot"
        job = glue.CfnJob(
            self, "SnapshotJob", name=self.glue_job_name, role=role.role_arn, max_capacity=0.0625,
            max_retries=0, timeout=30, security_configuration=f"{cfg.name}-glue",
            command=glue.CfnJob.JobCommandProperty(name="pythonshell", python_version="3.9",
                                                   script_location=script.s3_object_url),
            connections=glue.CfnJob.ConnectionsListProperty(connections=[conn_name]),
            default_arguments={"--engines_zip": engines.s3_object_url, "--max_exception_rate": "0.02",
                               "--job-language": "python"})
        job.add_resource_dependency(conn)
        job.add_resource_dependency(sec)

    def _sagemaker(self, data, stack: cdk.Stack) -> None:
        """Steps 2-3: SageMaker Processing runs the Markov and MDP engines from the engine image."""
        self.processing_role = iam.Role(self, "ProcessingRole", assumed_by=iam.ServicePrincipal("sagemaker.amazonaws.com"),
                                        description="SageMaker Processing: Markov and MDP engines")
        self.image.repository.grant_pull(self.processing_role)
        grants.read_write(self.processing_role, data.curated, "runs/", data.key)
        grants.read(self.processing_role, data.curated, "config/", data.key)
        self.processing_role.add_to_policy(iam.PolicyStatement(
            sid="VpcNetworkInterfaces",
            actions=["ec2:CreateNetworkInterface", "ec2:CreateNetworkInterfacePermission", "ec2:DeleteNetworkInterface",
                     "ec2:DeleteNetworkInterfacePermission", "ec2:DescribeNetworkInterfaces", "ec2:DescribeVpcs",
                     "ec2:DescribeDhcpOptions", "ec2:DescribeSubnets", "ec2:DescribeSecurityGroups"],
            resources=["*"]))
        self.processing_role.add_to_policy(iam.PolicyStatement(
            sid="ProcessingLogs",
            actions=["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"],
            resources=[stack.format_arn(service="logs", resource="log-group",
                                        resource_name="/aws/sagemaker/ProcessingJobs:*",
                                        arn_format=cdk.ArnFormat.COLON_RESOURCE_NAME)]))
        self.processing_role.add_to_policy(iam.PolicyStatement(
            sid="ProcessingMetrics", actions=["cloudwatch:PutMetricData"], resources=["*"],
            conditions={"StringLike": {"cloudwatch:namespace": "/aws/sagemaker/*"}}))

    def _batch(self, cfg: Config, vpc, subnets, sg, data, key) -> None:
        """Step 4: Monte Carlo as an array job (one child per chunk), plus the package task."""
        spot = batch.FargateComputeEnvironment(self, "SpotEnv", vpc=vpc, vpc_subnets=subnets, security_groups=[sg],
                                               spot=True, maxv_cpus=256)
        on_demand = batch.FargateComputeEnvironment(self, "OnDemandEnv", vpc=vpc, vpc_subnets=subnets,
                                                    security_groups=[sg], spot=False, maxv_cpus=64)
        self.queue = batch.JobQueue(self, "McQueue", priority=1, compute_environments=[
            batch.OrderedComputeEnvironment(compute_environment=spot, order=1),
            batch.OrderedComputeEnvironment(compute_environment=on_demand, order=2)])
        job_role = iam.Role(self, "BatchJobRole", assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))
        grants.read_write(job_role, data.curated, "runs/", key)
        grants.read(job_role, data.curated, "config/", key)
        exec_role = iam.Role(self, "BatchExecRole", assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"))
        self.image.repository.grant_pull(exec_role)
        log_group = logs.LogGroup(self, "BatchLogs", encryption_key=key, retention=logs.RetentionDays.ONE_YEAR,
                                  removal_policy=cdk.RemovalPolicy.RETAIN)
        container = batch.EcsFargateContainerDefinition(
            self, "EngineContainer", image=ecs.ContainerImage.from_docker_image_asset(self.image), cpu=2,
            memory=cdk.Size.gibibytes(4), command=["mc-chunk"], job_role=job_role, execution_role=exec_role,
            logging=ecs.LogDriver.aws_logs(stream_prefix="engine", log_group=log_group),
            readonly_root_filesystem=True, assign_public_ip=False,
            fargate_cpu_architecture=ecs.CpuArchitecture.X86_64,
            fargate_operating_system_family=ecs.OperatingSystemFamily.LINUX)
        self.job_definition = batch.EcsJobDefinition(
            self, "EngineJob", container=container, retry_attempts=3, timeout=cdk.Duration.hours(2), propagate_tags=True,
            retry_strategies=[batch.RetryStrategy.of(batch.Action.RETRY, batch.Reason.SPOT_INSTANCE_RECLAIMED),
                              batch.RetryStrategy.of(batch.Action.EXIT, batch.Reason.NON_ZERO_EXIT_CODE)])
