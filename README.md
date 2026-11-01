# Agentic Decision Intelligence on AWS

Agentic Decision Intelligence for a consumer-lending portfolio: a monthly, governed loop of
Model -> Decide -> Simulate -> Interpret -> Recommend -> Act, with a human approval gate before any
action.

Inside the code, `adi` is the short prefix for Agentic Decision Intelligence (packages
`adi_engines`, stacks `Adi-Dev-*`, job names `adi-*`). It stays short because AWS caps some
names, for example SageMaker processing job names at 63 characters.

All numbers in this repository (demo sample, example configuration, test fixtures) are invented.
They do not come from any lender.

## What is in this repository

| Path | Contents |
|---|---|
| `engines/adi_engines` | Data-quality gate, Markov engine, constrained MDP (LP), Monte Carlo (deterministic, chunked), P&L with explicit horizon and reconciliation, evidence contract (grounding, PII, abstain), consent / de-identification / TIA gate, drift check |
| `jobs/adi_jobs` | Container tasks run by SageMaker Processing (Markov, MDP) and AWS Batch (Monte Carlo chunks, evidence package) |
| `services/adi_services` | Lambda handlers: run context, insight agent (Bedrock + Guardrails), approval publish / decide / status / expire, action emit + idempotent handler, TIA review |
| `glue/adi_glue` | Glue Python shell job: step 1 data-quality gate and curated snapshot |
| `residency/adi_residency` | Hanoi Local Zone tokenizer (consent gate, de-identification, TIA-gated upload) |
| `infra` | AWS CDK app (Python): Security, Network, Data, Platform, Residency (opt-in) stacks, cdk-nag clean |
| `config/example` | Example economics, actions, Monte Carlo, prior matrix and published KPIs (replace before production) |
| `tests` | pytest; the engines reproduce every hand-computed figure of the invented demo sample. `test_private_reference.py` checks a private published sample when `ADI_PRIVATE_REFERENCE` points to a JSON file kept outside the repository |
| `tools` | Offline end-to-end run, approver CLI |
| `docs` | Deploy guide, runbook, acceptance checklist, NFR decisions, TIA input pack, residency notes, deviations |

## Quick start (no AWS account needed)

```powershell
python -m pip install numpy scipy boto3 pytest "moto[dynamodb,s3]"
python -m pytest
python tools/local_run.py
```

## Deploy

See `docs/deploy.md`. Deployment needs an AWS account, credentials, CDK bootstrap and Docker for
the engine image. Nothing is deployed by default.

## Status

Code, infrastructure and tests are complete for the MVP scope. Open items that need people, not
code, are in `docs/nfr-decisions.md` and `docs/acceptance.md` (legal TIA filing, NFR targets,
downstream action systems, real data calibration, causal validation design).
