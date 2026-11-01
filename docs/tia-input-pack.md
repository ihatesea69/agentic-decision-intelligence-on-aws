# Technical input for the Transfer Impact Assessment (PDPL Law 91/2025, Decree 356/2025)

Prepared for the company's legal team. It states technical facts about the system; it does not
interpret the law. Source of the legal requirements: law-firm summary (DLA Piper, Vietnam), to be
confirmed by counsel.

## Data flows

| Flow | Data | From | To | Personal data? |
|---|---|---|---|---|
| Raw extract landing | Loan records with identifiers and consent flags | Lender data centre | Hanoi Local Zone instance (Vietnam) | Yes, stays in Vietnam |
| De-identified transfer | Loan state, balances, MOB, product, 10-year age band, province, keyed tokens | Hanoi Local Zone | Singapore Region (ingest bucket) | Designed to be de-identified; counsel to confirm |
| Evidence to the model | Aggregated figures only (counts, rates, VND totals) | Singapore Region | Bedrock in the chosen Region | No record-level data |

## Controls

- Consent: explicit, per purpose (`credit_risk_analytics`), withdrawable; silence is not consent;
  records without valid consent are dropped before de-identification (code: `adi_engines/residency.py`).
- De-identification: names, ID numbers, phone, email, street address, account numbers and free text
  are removed; date of birth becomes a 10-year band; identifiers become HMAC-SHA256 tokens whose key
  never leaves the Local Zone volume.
- Leak check: the run aborts if any identifier field remains (`leaked_identifiers`).
- Transfer gate: no upload unless the TIA register entry is approved (strict policy) and its 6-month
  review is not overdue; a daily job closes the gate when the review is late.
- Encryption: KMS customer-managed key at rest; TLS 1.2+ in transit; private network paths only.
- Audit: every action and its approver is written to an Object Lock bucket.

## Open questions for counsel

1. Does keyed tokenization with the key held in Vietnam count as de-identification under the PDPL?
2. Is the loan book "important data" or "core data" under the Data Law thresholds (10,000 / 100,000
   organizations; 100,000 / 1,000,000 citizens)?
3. Which consent wording covers credit-risk analytics on an existing loan?
