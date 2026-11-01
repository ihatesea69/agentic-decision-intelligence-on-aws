# Deviations from the solution design and the diagrams

Each deviation has a reason that can be checked. Nothing here changes the decision logic.

| # | Design said | Built | Reason |
|---|---|---|---|
| 1 | Insight agent on Bedrock AgentCore | Lambda + Bedrock Converse + Guardrails, same evidence contract | The design's own fallback: direct Bedrock invocation with the same evidence contract. Fewer moving parts for the MVP; AgentCore can replace the Lambda without changing the contract. |
| 2 | Consent ledger in DynamoDB + Lambda gate (PDPL governance diagram) | Consent is read from the lender's in-country system with the raw extract; the gate runs in the Local Zone tokenizer | Lambda and DynamoDB do not run in a Local Zone, and a consent ledger in Singapore would move personal data across the border. |
| 3 | Data-residency diagram: de-identified data crosses "over Direct Connect Hanoi" | Direct Connect Hanoi connects the lender's data centre to the Local Zone; Local Zone to Region traffic uses the AWS network | Correction to the diagram label. |
| 4 | Monte Carlo on AWS Batch with EC2 Spot | AWS Batch on Fargate Spot, on-demand Fargate as fallback | No AMI or instance fleet to manage; same Spot pricing model; chunks are replayable so a reclaimed task is retried with identical results. |
| 5 | SageMaker Model Monitor | Drift check (PSI + Wilson-interval shift) inside the Markov task, surfaced to the approver | Model Monitor is closed to new customers. Retraining stays a human-approved step. |
| 6 | Pricing and approval-threshold actions | Not enabled (`enabledActions` excludes them; the MDP refuses them while origination is frozen) | They only affect new vintages, so they change nothing while the book takes no new loans (`origination_frozen` in `config/actions.json`). |
| 7 | Actions call lending and collections systems | `RECORD_ONLY` adapter: instruction + rollback reference to the audit store | The receiving systems and their reversal APIs are still an open question. |
| 8 | QuickSight, Lake Formation, Macie, Identity Center in the architecture | Configured by hand (deploy guide step 5) | Account-level singletons; a stack must not own them. |
| 9 | A published "risk-adjusted net profit" headline stated as a fact | Recomputed from its KPI lines; marked UNRECONCILED when it does not tie, and the agent may not state it | A headline that cannot be rebuilt from its own lines is not evidence. Finance must publish the formula and horizon. |
