# Deploy guide

Nothing in this repository deploys by itself. Run these commands in your own terminal, against an
account you own. Every step that changes the account is marked **(changes AWS)**.

## 0. Prerequisites

- An AWS account for this stage (dedicated dev account recommended), Region `ap-southeast-1`.
- `aws sso login --profile <admin-profile>`; Node 22, AWS CDK CLI 2.1137+, Python 3.12+, Docker running.
- Bedrock model access granted in `ap-southeast-1` for the model you choose. Prefer an in-Region
  model. A cross-Region inference profile (`apac.` prefix) sends the de-identified, aggregated
  evidence package to other APAC Regions; decide that with your data-protection officer first.
- `python -m pip install -r infra/requirements.txt`

## 1. Bootstrap (changes AWS)

```powershell
cd infra
cdk bootstrap aws://<ACCOUNT_ID>/ap-southeast-1 --profile <admin-profile>
```

## 2. Synthesize and review

```powershell
cdk synth -c modelId=<model-id> -c alertEmail=<ops@company> --profile <admin-profile>
python tools/verify_synth.py cdk.out dev      # 35 checks must pass
cdk diff --profile <admin-profile>
```

## 3. Deploy (changes AWS)

```powershell
cdk deploy Adi-Dev-Security Adi-Dev-Network Adi-Dev-Data Adi-Dev-Platform `
  -c modelId=<model-id> -c alertEmail=<ops@company> --profile <admin-profile>
```

Confirm the SNS email subscription. Stack outputs list the API URL, bucket and table names.

## 4. Configuration (changes AWS)

Risk and Finance own the config files. Start from `config/example`, replace every value, then:

```powershell
aws s3 cp config/live s3://<CuratedBucket>/config/ --recursive --profile <admin-profile>
```

Required: `economics.json`, `actions.json`. Recommended: `montecarlo.json`, `budget.json` (with
`horizon_months` equal to the simulation horizon), `prior_matrix.json`, `published_kpis.json`.

## 5. Account-level settings done by hand

These are account singletons; a stack must not own them.

| Setting | Why | How |
|---|---|---|
| Identity Center permission set `ADI-Approver` | Approvers sign API calls; the handler checks the role prefix `AWSReservedSSO_ADI-Approver` | Inline policy: `execute-api:Invoke` on the `DecisionApiExecuteArn` output, `s3:GetObject` on `runs/*` of the curated bucket |
| TIA register entry | The tokenizer and the review job read it | Put an item `{tia_id: adi-credit-analytics, status, prepared_at, last_reviewed_at}` in the TIA table after legal prepares the dossier |
| QuickSight | Evidence pages for the approver | Connect to the Redshift workgroup through a VPC connection; dataset `adi.snapshot` |
| Lake Formation | Fine-grained access on curated data | Register the curated bucket, grant per-role column access |
| Macie | Verify no raw PII reached the Region | Enable automated sensitive-data discovery on the ingest and curated buckets |

## 6. First run (changes AWS)

```powershell
aws stepfunctions start-execution --state-machine-arn <StateMachineArn> `
  --name first-run-2026-07 --input '{\"snapshot_month\": \"2026-07\"}' --profile <admin-profile>
```

The run waits at step 6. Approve, revise or reject:

```powershell
python ../tools/decide.py --api <DecisionApiUrl> --id first-run-2026-07 --profile <approver> status
python ../tools/decide.py --api <DecisionApiUrl> --id first-run-2026-07 --profile <approver> approve --period 2026-08 --rationale "..."
```

The monthly schedule starts later runs automatically on day 5 at 09:00 Vietnam time.

## 7. Residency option (changes AWS)

1. Opt in to the Hanoi Local Zone group: `aws ec2 modify-availability-zone-group --group-name ap-southeast-1-han-1 --opt-in-status opted-in`.
2. `cdk deploy --all -c enableResidency=true ...` (adds the Local Zone subnet, SSM endpoints and the tokenizer instance).
3. Read `docs/residency.md` for the checks to run before the first transfer.

## 8. Production

`-c stage=prod -c auditLockMode=COMPLIANCE -c auditRetentionDays=<from NFR> -c modelId=...`.
Compliance-mode Object Lock cannot be shortened or removed, not even by the root user. Set the
retention from the approved NFR, not from this guide.
