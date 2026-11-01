#!/usr/bin/env python3
"""Agentic Decision Intelligence on AWS: CDK app. Stacks: Security, Network, Data (stateful), Platform, Residency (opt-in)."""
from pathlib import Path

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from adi_infra.config import load
from adi_infra.data import DataStack
from adi_infra.nag import suppress
from adi_infra.network import NetworkStack
from adi_infra.platform_stack import PlatformStack
from adi_infra.security import SecurityStack
from adi_infra.stage import stage

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "infra" / ".build"

app = cdk.App()
cfg = load(app)
env = cdk.Environment(account=cfg.account, region=cfg.region)
prefix = f"Adi-{cfg.stage.title()}"

lambda_bundle = stage(BUILD / "lambda", [ROOT / "engines" / "adi_engines", ROOT / "services" / "adi_services"])

sec = SecurityStack(app, f"{prefix}-Security", cfg=cfg, env=env)
net = NetworkStack(app, f"{prefix}-Network", cfg=cfg, key=sec.key, env=env)
data = DataStack(app, f"{prefix}-Data", cfg=cfg, key=sec.key, net=net, env=env, termination_protection=cfg.prod)
plat = PlatformStack(app, f"{prefix}-Platform", cfg=cfg, sec=sec, net=net, data=data, root=ROOT,
                     lambda_bundle=lambda_bundle, env=env)
stacks = [sec, net, data, plat]

if cfg.enable_residency:
    from adi_infra.residency import ResidencyStack

    tokenizer_bundle = stage(BUILD / "tokenizer", [ROOT / "engines" / "adi_engines", ROOT / "residency" / "adi_residency"])
    stacks.append(ResidencyStack(app, f"{prefix}-Residency", cfg=cfg, sec=sec, net=net, data=data,
                                 bundle=tokenizer_bundle, env=env))

for s in stacks:
    suppress(s)
cdk.Tags.of(app).add("project", "agentic-decision-intelligence-on-aws")
cdk.Tags.of(app).add("stage", cfg.stage)
cdk.Aspects.of(app).add(AwsSolutionsChecks(verbose=True))
app.synth()
