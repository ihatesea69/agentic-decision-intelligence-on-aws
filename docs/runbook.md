# Runbook

## Monthly cycle

| When | What | Who |
|---|---|---|
| Day 1-3 | Lender delivers the raw extract to the Hanoi Local Zone; the tokenizer runs on day 3 02:00 and uploads de-identified data if the TIA gate is open | Data engineering |
| Day 5 09:00 (Vietnam) | Scheduler starts the workflow: DQ gate -> mart -> Markov -> MDP -> Monte Carlo -> package -> insight | automatic |
| Day 5-10 | Approver reads the package (QuickSight + package.json + insight.json) and decides through the API | Approver |
| After approval | Workflow emits one event per action; handler records them (RECORD_ONLY) | automatic |

## Alarms and what to do

| Alarm | Meaning | First action |
|---|---|---|
| WorkflowFailed | A gate failed closed; no action ran | Read the SNS alert (failed step), then `runs/<id>/evidence/` (dq_report.json, markov.json, mdp.json). Fix the input or config, start a new execution. Never edit evidence. |
| WorkflowTimedOut | Run exceeded 35 days | Check for a stuck approval; start a new run for the month. |
| ActionDlq | An action event failed or was IN_PROGRESS twice | Read the message; check the idempotency row. Re-drive only after confirming the action did not run. |
| SchedulerDlq | The monthly schedule could not start the workflow | Check the scheduler role and the state machine; start the run by hand. |
| ActionErrors / InsightErrors / TiaReviewErrors | A function raised | CloudWatch Logs for that function (JSON format). |
| Budget 80% / 100% forecast | Monthly cost | Check Redshift RPU-hours, interface endpoints, Batch vCPU-hours. |

## Common situations

- **Agent check FAILED or ABSTAINED**: the package still reaches the approver, but production actions
  stay disabled for that round. Revise (new round) after fixing the cause, or decide without actions.
- **Revise**: the workflow re-runs steps 3-5 into `runs/<id>/r<n+1>/`; the evidence of earlier rounds
  is kept. After `maxRevisions` the run fails closed with `revision-limit`.
- **Approval window closed**: the ledger row becomes EXPIRED and no action runs; start a new run.
- **Sparse rows refused**: Markov fails closed when B3/B4 have fewer than 30 accounts and no prior
  is configured. Upload `config/prior_matrix.json` pooled over several months.
- **TIA review overdue**: the daily job closes the transfer gate and alerts the DPO; the tokenizer
  holds data in Vietnam until the review date is updated.

## Changes

- Config (`s3://<curated>/config/`) is versioned by the bucket; record who changed what in the
  change log of the risk committee.
- Infrastructure: `cdk diff` before every `cdk deploy`; stateful resources are retained on delete.
