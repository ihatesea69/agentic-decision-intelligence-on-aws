"""Residency stack (opt-in, -c enableResidency=true): the in-country tokenizer in the Hanoi Local Zone.

Raw extracts land on an encrypted EBS volume in the Local Zone and never leave Vietnam; the
tokenization key stays on that volume. A monthly systemd timer runs the consent gate and
de-identification, and uploads only de-identified records to the ingest bucket in Singapore when
the TIA gate is open. Prerequisite: the account has opted in to the ap-southeast-1-han-1 zone group.
"""
from __future__ import annotations

from pathlib import Path

import aws_cdk as cdk
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_iam as iam
from aws_cdk import aws_s3_assets as s3_assets
from constructs import Construct

from . import grants
from .config import Config

SERVICE = """[Unit]
Description=ADI in-country tokenizer (previous month)
[Service]
Type=oneshot
EnvironmentFile=/etc/adi/tokenizer.env
ExecStart=/bin/bash -c 'python3 -m adi_residency.tokenizer --month "$(date -d "$(date +%%Y-%%m-01) -1 month" +%%Y-%%m)"'
"""
TIMER = """[Unit]
Description=Run the ADI tokenizer on day 3 of each month
[Timer]
OnCalendar=*-*-03 02:00:00
Persistent=true
[Install]
WantedBy=timers.target
"""


class ResidencyStack(cdk.Stack):
    def __init__(self, scope: Construct, cid: str, *, cfg: Config, sec, net, data, bundle: Path, **kw) -> None:
        super().__init__(scope, cid, **kw)
        if net.local_zone_subnet is None:
            raise ValueError("the network stack has no Local Zone subnet; deploy with -c enableResidency=true")
        key = sec.key
        code = s3_assets.Asset(self, "TokenizerBundle", path=str(bundle))
        role = iam.Role(self, "TokenizerRole", assumed_by=iam.ServicePrincipal("ec2.amazonaws.com"),
                        managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name("AmazonSSMManagedInstanceCore")])
        grants.write(role, data.ingest, "deidentified/", key)
        data.tia.grant_read_data(role)
        iam.Grant.add_to_principal(grantee=role, actions=["s3:GetObject"],
                                   resource_arns=[code.bucket.arn_for_objects(code.s3_object_key)])

        sg = ec2.SecurityGroup(self, "TokenizerSg", vpc=net.vpc, allow_all_outbound=False,
                               description="Hanoi tokenizer: HTTPS to VPC endpoints only, no inbound")
        sg.add_egress_rule(ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS; the subnet routes only to VPC endpoints")

        ud = ec2.UserData.for_linux()
        local_zip = ud.add_s3_download_command(bucket=code.bucket, bucket_key=code.s3_object_key,
                                               local_file="/tmp/adi-tokenizer.zip")
        env_file = "\n".join([
            f"ADI_INGEST_BUCKET={data.ingest.bucket_name}", f"ADI_TIA_TABLE={data.tia.table_name}",
            f"AWS_DEFAULT_REGION={cfg.region}", "ADI_TIA_ID=adi-credit-analytics", "ADI_TIA_POLICY=strict",
            "ADI_RAW_DIR=/data/raw", "ADI_KEY_PATH=/data/key/tokenization.key", "ADI_REPORT_DIR=/data/reports",
            "PYTHONPATH=/opt/adi"])
        ud.add_commands(
            "set -euo pipefail",
            "dnf install -y python3-boto3 unzip",
            "DEV=$(lsblk -dpno NAME,SIZE | awk '$2==\"100G\"{print $1}' | head -n1)",
            "blkid \"$DEV\" || mkfs -t xfs \"$DEV\"",
            "mkdir -p /data && grep -q ' /data ' /etc/fstab || echo \"UUID=$(blkid -s UUID -o value $DEV) /data xfs defaults,nofail 0 2\" >> /etc/fstab",
            "mount -a && mkdir -p /data/raw /data/key /data/reports && chmod 700 /data/key",
            f"mkdir -p /opt/adi && unzip -o {local_zip} -d /opt/adi",
            "mkdir -p /etc/adi",
            f"cat > /etc/adi/tokenizer.env <<'EOF'\n{env_file}\nEOF",
            f"cat > /etc/systemd/system/adi-tokenizer.service <<'EOF'\n{SERVICE}EOF",
            f"cat > /etc/systemd/system/adi-tokenizer.timer <<'EOF'\n{TIMER}EOF",
            "systemctl daemon-reload && systemctl enable --now adi-tokenizer.timer")

        self.instance = ec2.Instance(
            self, "Tokenizer", vpc=net.vpc, vpc_subnets=ec2.SubnetSelection(subnets=[net.local_zone_subnet]),
            instance_type=ec2.InstanceType("m7i.large"),
            machine_image=ec2.MachineImage.latest_amazon_linux2023(cpu_type=ec2.AmazonLinuxCpuType.X86_64),
            # termination protection always: this instance holds the only tokenization key
            role=role, security_group=sg, user_data=ud, require_imdsv2=True, detailed_monitoring=True,
            disable_api_termination=True, associate_public_ip_address=False,
            block_devices=[
                ec2.BlockDevice(device_name="/dev/xvda", volume=ec2.BlockDeviceVolume.ebs(
                    30, encrypted=True, kms_key=key, volume_type=ec2.EbsDeviceVolumeType.GP3)),
                ec2.BlockDevice(device_name="/dev/sdf", volume=ec2.BlockDeviceVolume.ebs(
                    100, encrypted=True, kms_key=key, volume_type=ec2.EbsDeviceVolumeType.GP3,
                    delete_on_termination=False)),
            ])
        cdk.CfnOutput(self, "TokenizerInstanceId", value=self.instance.instance_id)
