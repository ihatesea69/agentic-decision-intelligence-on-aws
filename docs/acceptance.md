# Production acceptance checklist (eight control domains)

Status legend: BUILT = implemented and tested here; EVIDENCE = needs a run or a person to produce
proof; OPEN = needs a decision first.

| Domain | Acceptance condition | What the build provides | Status | Evidence still needed |
|---|---|---|---|---|
| Data | Reconciliation, freshness, quality, lineage, access controls pass | Glue DQ gate: freshness, completeness, uniqueness, valid states, balance identity, one-bucket rule, exception-rate limit; dq_report.json per run | BUILT | One full month of real data through the gate; lineage in Lake Formation |
| Model | Independent validation, registry approval, back-testing, drift thresholds, rollback | Sparse-row refusal, Wilson intervals, smoothing rule, drift check, versioned config prefix | EVIDENCE | Independent validation report; back-test on 12+ months; model registry and rollback procedure |
| Simulation | Convergence, sensitivity, deterministic replay | Seeded block replay identical across any chunking (tested); horizon on every number | BUILT | Convergence plot (paths vs P5/P50/P95 stability) and sensitivity runs on real data |
| Agent | Groundedness, policy fidelity, PII protection, tool authorization meet thresholds | Numeric grounding check, PII regexes + Guardrail, UNRECONCILED blocking, abstain path, no tools | BUILT | Evaluation set of 50+ packages with pass rate and false-abstain rate |
| Security | Threat model, penetration test, least privilege, key rotation | Isolated VPC, CMK with rotation, scoped IAM (no delete or lock-override actions), WAF, cdk-nag clean | EVIDENCE | Threat model, penetration test of the decision API |
| Reliability | Retry/fallback tests, backup restore, DR exercise meet RTO/RPO | Fail-closed gates, Spot fallback, DLQs, PITR, Object Lock, idempotency | OPEN | RTO/RPO and DR tier (see nfr-decisions.md), then a restore exercise |
| Operations | Monitoring, alarms, runbooks, ownership, support model, cost budgets | 7 alarms, monthly budget, runbook | EVIDENCE | Named owners and on-call; first month of alarm tuning |
| Business | Shadow-mode decisions show measurable value with no material control breach | RECORD_ONLY actions, assumed effects flagged, audit trail | OPEN | Pre-registered shadow-mode design on collections + restructure, 3+ months |
