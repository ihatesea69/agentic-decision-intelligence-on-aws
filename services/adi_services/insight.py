"""Step 5: grounded insight (Bedrock Converse + Guardrails), the design's fallback to AgentCore:
direct Bedrock invocation with the same evidence contract.

The agent explains; it never chooses or changes the action. Its text reaches the approver only
after `verify_narrative` passes: every number must come from the evidence package, no PII, and an
UNRECONCILED figure may not be stated. Otherwise agent_status is FAILED or ABSTAINED and the
package still goes to human review, with production actions disabled.
"""
from __future__ import annotations

from adi_engines.evidence import EvidencePackage, verify_narrative

from . import aws

SYSTEM = (
    "You explain a credit-portfolio decision package to a human approver. Rules: "
    "1) Use only the figures in the package, written exactly as given; do not compute new numbers. "
    "2) Tag each sentence with one source label: [fact], [model_calculated], [ai_explanation] or "
    "[mdp_candidate_action]. "
    "3) Never state a figure whose status is not OK; say that it is under reconciliation. "
    "4) Never output names, ID numbers, phone numbers, emails or account identifiers. "
    "5) You do not approve, choose or change any action; the approver decides. "
    "6) If the package is incomplete, reply exactly: ABSTAIN."
)


def build_prompt(pkg: EvidencePackage) -> str:
    lines = [f"Decision {pkg.decision_id}, snapshot {pkg.snapshot_month}.", "Figures:"]
    for f in pkg.figures:
        h = f", horizon {f.horizon_months} months" if f.horizon_months else ""
        lines.append(f"- {f.id} [{f.label}] = {f.value:g} {f.unit}{h}, source {f.source}, status {f.status}")
    lines.append("Write at most 8 sentences for the approver.")
    return "\n".join(lines)


def converse(prompt: str) -> tuple[str, str]:
    resp = aws.client("bedrock-runtime").converse(
        modelId=aws.env("MODEL_ID"),
        system=[{"text": SYSTEM}],
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"temperature": 0, "maxTokens": 700},
        guardrailConfig={"guardrailIdentifier": aws.env("GUARDRAIL_ID"),
                         "guardrailVersion": aws.env("GUARDRAIL_VERSION", "DRAFT"), "trace": "enabled"},
    )
    text = "".join(c.get("text", "") for c in resp["output"]["message"]["content"])
    return text.strip(), resp.get("stopReason", "")


def handler(event, _context=None):
    pkg = EvidencePackage.from_dict(aws.get_json(event["package_uri"]))
    out_uri = event["evidence_prefix"].rstrip("/") + "/insight.json"
    if not pkg.complete:
        aws.put_json(out_uri, {"agent_status": "ABSTAINED", "reason": "incomplete package", "missing": pkg.missing})
        return {"agent_status": "ABSTAINED", "insight_uri": out_uri}
    text, stop = converse(build_prompt(pkg))
    if stop == "guardrail_intervened" or text == "ABSTAIN":
        status, check = "ABSTAINED", None
    else:
        check = verify_narrative(text, pkg)
        status = "PASSED" if check.passed else "FAILED"
    aws.put_json(out_uri, {"agent_status": status, "stop_reason": stop,
                           "narrative": text if status == "PASSED" else None,
                           "grounding": check.to_dict() if check else None})
    return {"agent_status": status, "insight_uri": out_uri}
