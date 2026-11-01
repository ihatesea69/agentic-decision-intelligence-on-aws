# Decisions the business must make

The code reads each of these from configuration. Defaults are placeholders, not recommendations.

| Decision | Where it is set | Default in the build | Owner |
|---|---|---|---|
| Audit evidence retention | `-c auditRetentionDays`, `-c auditLockMode=COMPLIANCE` | 30 days, GOVERNANCE (dev only) | Compliance |
| RTO / RPO, DR tier, recovery Region | Not automated yet; PITR and versioning are on | none | Risk + IT |
| Approval window | `-c approvalHours` | 120 hours | Business owner |
| Revision limit | `-c maxRevisions` | 2 | Business owner |
| Enabled actions | `-c enabledActions` | intensify_collections, restructure | Business owner |
| Capacity and risk-appetite limits | `config/actions.json` (`capacity_per_month`, `max_wo_per_month`) | example values | Risk |
| Action effect sizes | `config/actions.json` (`shift`) | ASSUMED; flagged in every package | Risk, until measured |
| Revenue budget and its horizon | `config/budget.json` | example | Finance |
| Formula behind "risk-adjusted net profit" | `config/published_kpis.json` + Finance note | UNRECONCILED in the example (lines give 0.17, headline says 0.25) | Finance |
| Bedrock model and Region policy | `-c modelId` | placeholder | Data protection officer + IT |
| Downstream systems and reversal APIs | `DOWNSTREAM_MODE` adapter | RECORD_ONLY | Lending + Collections IT |
| Monthly cost budget | `-c monthlyBudgetUsd` | 500 USD | IT finance |
