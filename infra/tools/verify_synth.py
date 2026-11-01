"""Verify the synthesized templates by measurement. Usage: python tools/verify_synth.py [cdk.out] [stage]
Prints one line per check and exits 1 if any check fails."""
import json
import sys
from pathlib import Path

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "cdk.out")
STAGE = (sys.argv[2] if len(sys.argv) > 2 else "dev").title()
T = {n: json.loads((OUT / f"Adi-{STAGE}-{n}.template.json").read_text(encoding="utf-8"))
     for n in ("Security", "Network", "Data", "Platform")}
fails = []


def res(stack, typ):
    return {k: v for k, v in T[stack]["Resources"].items() if v["Type"] == typ}


def flat(x) -> str:
    """Flatten Fn::Join / Fn::Sub strings so the state machine definition can be searched."""
    if isinstance(x, str):
        return x
    if isinstance(x, list):
        return "".join(flat(i) for i in x)
    if isinstance(x, dict):
        if "Fn::Join" in x:
            return flat(x["Fn::Join"][1])
        return "<token>"
    return str(x)


def check(name, ok, detail=""):
    print(("PASS " if ok else "FAIL ") + name + (f"  [{detail}]" if detail else ""))
    if not ok:
        fails.append(name)


def statements(stack):
    for r in T[stack]["Resources"].values():
        if r["Type"] in ("AWS::IAM::Policy", "AWS::IAM::ManagedPolicy"):
            yield from r["Properties"]["PolicyDocument"]["Statement"]
        if r["Type"] == "AWS::IAM::Role":
            for p in r["Properties"].get("Policies", []):
                yield from p["PolicyDocument"]["Statement"]


def actions(st):
    a = st.get("Action", [])
    return a if isinstance(a, list) else [a]


# ---- network: private only -----------------------------------------------------------------
check("no internet gateway", not res("Network", "AWS::EC2::InternetGateway"))
check("no NAT gateway", not res("Network", "AWS::EC2::NatGateway"))
subnets = res("Network", "AWS::EC2::Subnet")
check("no subnet maps public IPs", all(not s["Properties"].get("MapPublicIpOnLaunch") for s in subnets.values()),
      f"{len(subnets)} subnets")
eps = res("Network", "AWS::EC2::VPCEndpoint").values()
gw = [e for e in eps if e["Properties"].get("VpcEndpointType") == "Gateway"]
itf = [e for e in eps if e["Properties"].get("VpcEndpointType") == "Interface"]
check("2 gateway endpoints (S3, DynamoDB)", len(gw) == 2, str(len(gw)))
check("7 interface endpoints", len(itf) == 7, str(len(itf)))
check("VPC flow log", len(res("Network", "AWS::EC2::FlowLog")) == 1)

# ---- security --------------------------------------------------------------------------------
keys = res("Security", "AWS::KMS::Key")
check("1 KMS key with rotation", len(keys) == 1 and all(k["Properties"]["EnableKeyRotation"] for k in keys.values()))
gr = list(res("Security", "AWS::Bedrock::Guardrail").values())
check("guardrail blocks PII and policy-change topic",
      len(gr) == 1 and gr[0]["Properties"]["SensitiveInformationPolicyConfig"]["PiiEntitiesConfig"]
      and gr[0]["Properties"]["TopicPolicyConfig"]["TopicsConfig"][0]["Type"] == "DENY")

# ---- data --------------------------------------------------------------------------------
buckets = res("Data", "AWS::S3::Bucket")
check("4 buckets", len(buckets) == 4, str(len(buckets)))
kms_buckets = [b for b in buckets.values() if b["Properties"]["BucketEncryption"]["ServerSideEncryptionConfiguration"][0]
               ["ServerSideEncryptionByDefault"]["SSEAlgorithm"] == "aws:kms"]
check("3 buckets on the customer-managed key", len(kms_buckets) == 3, str(len(kms_buckets)))
check("all buckets block public access",
      all(all(b["Properties"]["PublicAccessBlockConfiguration"].values()) for b in buckets.values()))
check("all buckets retained", all(b.get("DeletionPolicy") == "Retain" for b in buckets.values()))
locked = [b for b in buckets.values() if b["Properties"].get("ObjectLockEnabled")]
check("audit bucket has Object Lock with default retention",
      len(locked) == 1 and locked[0]["Properties"]["ObjectLockConfiguration"]["Rule"]["DefaultRetention"]["Days"] >= 1)
tables = res("Data", "AWS::DynamoDB::Table")
check("4 tables, CMK + PITR + retained", len(tables) == 4 and all(
    t["Properties"]["SSESpecification"]["SSEType"] == "KMS"
    and t["Properties"]["PointInTimeRecoverySpecification"]["PointInTimeRecoveryEnabled"]
    and t.get("DeletionPolicy") == "Retain" for t in tables.values()))
wg = list(res("Data", "AWS::RedshiftServerless::Workgroup").values())
check("Redshift workgroup is private", len(wg) == 1 and wg[0]["Properties"]["PubliclyAccessible"] is False)


# ---- platform ----------------------------------------------------------------------------
fns = {k: v for k, v in res("Platform", "AWS::Lambda::Function").items()
       if str(v["Properties"].get("Handler", "")).startswith("adi_services.")}
check("9 ADI functions, all in the VPC on python3.14 arm64", len(fns) == 9 and all(
    f["Properties"].get("VpcConfig") and f["Properties"]["Runtime"] == "python3.14"
    and f["Properties"]["Architectures"] == ["arm64"] for f in fns.values()), str(len(fns)))
sm = list(res("Platform", "AWS::StepFunctions::StateMachine").values())
check("1 Standard state machine", len(sm) == 1 and sm[0]["Properties"]["StateMachineType"] == "STANDARD")
lc = sm[0]["Properties"]["LoggingConfiguration"]
check("workflow logs ALL without execution data (task token stays out)",
      lc["Level"] == "ALL" and lc["IncludeExecutionData"] is False)
check("workflow X-Ray tracing", sm[0]["Properties"]["TracingConfiguration"]["Enabled"] is True)
d = flat(sm[0]["Properties"]["DefinitionString"])
steps = ["Step0RunContext", "Step1DataQuality", "Step2Markov", "Step3Mdp", "Step4MonteCarlo", "EvidencePackage",
         "Step5Insight", "Step6Approval", "Step7EmitActions", "LoadMart"]
check("all workflow steps present", all(f'"{s}"' in d for s in steps), str([s for s in steps if f'"{s}"' not in d]))
check("approval uses waitForTaskToken", "lambda:invoke.waitForTaskToken" in d)
check("Markov and MDP use SageMaker Processing .sync", d.count("sagemaker:createProcessingJob.sync") == 2)
check("Glue and Batch use .sync", "glue:startJobRun.sync" in d and d.count("batch:submitJob.sync") == 2)
check("approval timeout handled", '"States.Timeout"' in d and "ExpiredNoAction" in d)
check("mart SQL quotes are escaped", "snapshot_month = \\\\'{}\\\\'" in d or "snapshot_month = \\'{}\\'" in d)
methods = res("Platform", "AWS::ApiGateway::Method")
check("2 API methods, both AWS_IAM", len(methods) == 2 and all(
    m["Properties"]["AuthorizationType"] == "AWS_IAM" for m in methods.values()), str(len(methods)))
check("WAF on the decision API", len(res("Platform", "AWS::WAFv2::WebACLAssociation")) == 1)
bus = list(res("Platform", "AWS::Events::EventBus").values())
check("action bus on the CMK", len(bus) == 1 and "KmsKeyIdentifier" in bus[0]["Properties"])
queues = res("Platform", "AWS::SQS::Queue")
check("all queues on the CMK", queues and all("KmsMasterKeyId" in q["Properties"] for q in queues.values()), str(len(queues)))
ces = [c["Properties"]["ComputeResources"]["Type"] for c in res("Platform", "AWS::Batch::ComputeEnvironment").values()]
check("Batch Fargate Spot + on-demand fallback", sorted(ces) == ["FARGATE", "FARGATE_SPOT"], str(ces))
jd = list(res("Platform", "AWS::Batch::JobDefinition").values())
check("engine container read-only root fs", jd and jd[0]["Properties"]["ContainerProperties"]["ReadonlyRootFilesystem"] is True)
sched = res("Platform", "AWS::Scheduler::Schedule")
check("2 schedules in Asia/Ho_Chi_Minh", len(sched) == 2 and all(
    s["Properties"]["ScheduleExpressionTimezone"] == "Asia/Ho_Chi_Minh" for s in sched.values()))
glue_jobs = list(res("Platform", "AWS::Glue::Job").values())
check("Glue Python shell 3.9 in the VPC with a security configuration", len(glue_jobs) == 1 and
      glue_jobs[0]["Properties"]["Command"]["PythonVersion"] == "3.9" and glue_jobs[0]["Properties"].get("Connections")
      and glue_jobs[0]["Properties"].get("SecurityConfiguration"))
check("cost budget", len(res("Platform", "AWS::Budgets::Budget")) == 1)

bad = []
for stack in T:
    for st in statements(stack):
        for a in actions(st):
            if a in ("*", "s3:*", "kms:*", "iam:*") or a.startswith("s3:DeleteObject") or a in (
                    "s3:PutObjectRetention", "s3:PutObjectLegalHold", "s3:BypassGovernanceRetention"):
                bad.append(f"{stack}:{a}")
check("no admin, delete or lock-override S3 actions in any policy", not bad, ", ".join(sorted(set(bad))))
print(f"\n{len(fails)} failed")
sys.exit(1 if fails else 0)
