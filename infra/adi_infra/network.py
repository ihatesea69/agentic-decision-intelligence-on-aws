"""Network stack: private-only VPC (no internet gateway, no NAT), VPC endpoints, flow logs.

Every compute resource runs in isolated subnets; AWS APIs are reached through gateway endpoints
(S3, DynamoDB) and interface endpoints (PrivateLink). With the residency option a subnet is added
in the Hanoi Local Zone.
"""
from __future__ import annotations

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_kms as kms
from aws_cdk import aws_logs as logs
from constructs import Construct

from .config import Config

S = ec2.InterfaceVpcEndpointAwsService
INTERFACE_ENDPOINTS = {
    "EcrApi": S.ECR, "EcrDocker": S.ECR_DOCKER, "Logs": S.CLOUDWATCH_LOGS, "Sns": S.SNS,
    "StepFunctions": S.STEP_FUNCTIONS, "EventBridge": S.EVENTBRIDGE, "BedrockRuntime": S.BEDROCK_RUNTIME,
}
RESIDENCY_ENDPOINTS = {"Ssm": S.SSM, "SsmMessages": S.SSM_MESSAGES, "Ec2Messages": S.EC2_MESSAGES}


class NetworkStack(cdk.Stack):
    def __init__(self, scope: Construct, cid: str, *, cfg: Config, key: kms.IKey, **kw) -> None:
        super().__init__(scope, cid, **kw)
        self.vpc = ec2.Vpc(
            self, "Vpc", ip_addresses=ec2.IpAddresses.cidr(cfg.vpc_cidr),
            availability_zones=cfg.availability_zones, nat_gateways=0,
            subnet_configuration=[ec2.SubnetConfiguration(name="private", subnet_type=ec2.SubnetType.PRIVATE_ISOLATED,
                                                          cidr_mask=22)],
            restrict_default_security_group=True)
        self.private = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_ISOLATED)

        flow_logs = logs.LogGroup(self, "FlowLogs", encryption_key=key, retention=logs.RetentionDays.ONE_YEAR,
                                  removal_policy=cdk.RemovalPolicy.RETAIN)
        self.vpc.add_flow_log("FlowLog", destination=ec2.FlowLogDestination.to_cloud_watch_logs(flow_logs),
                              traffic_type=ec2.FlowLogTrafficType.ALL)

        self.local_zone_subnet = None
        gateway_subnets = [self.private]
        if cfg.enable_residency:
            # Requires the account to opt in to the ap-southeast-1-han-1 zone group first.
            self.local_zone_subnet = ec2.Subnet(
                self, "HanoiLocalZone", vpc_id=self.vpc.vpc_id, availability_zone=cfg.hanoi_zone,
                cidr_block=cfg.residency_cidr, map_public_ip_on_launch=False)
            gateway_subnets.append(ec2.SubnetSelection(subnets=[self.local_zone_subnet]))

        self.vpc.add_gateway_endpoint("S3", service=ec2.GatewayVpcEndpointAwsService.S3, subnets=gateway_subnets)
        self.vpc.add_gateway_endpoint("DynamoDb", service=ec2.GatewayVpcEndpointAwsService.DYNAMODB,
                                      subnets=gateway_subnets)

        self.endpoint_sg = ec2.SecurityGroup(self, "EndpointSg", vpc=self.vpc, allow_all_outbound=False,
                                             description="Interface endpoints: HTTPS from inside the VPC")
        self.endpoint_sg.add_ingress_rule(ec2.Peer.ipv4(cfg.vpc_cidr), ec2.Port.tcp(443), "HTTPS from the VPC")
        endpoints = dict(INTERFACE_ENDPOINTS)
        if cfg.enable_residency:
            endpoints.update(RESIDENCY_ENDPOINTS)
        for name, service in endpoints.items():
            # open=False: the security group above already allows 443 from the VPC CIDR.
            self.vpc.add_interface_endpoint(name, service=service, private_dns_enabled=True, open=False,
                                            security_groups=[self.endpoint_sg], subnets=self.private)
