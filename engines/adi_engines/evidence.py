"""Structured evidence contract for the insight agent.

Every claim is labelled by source: fact, model-calculated, AI-generated explanation or MDP
candidate action. The agent must reproduce approved numbers exactly and abstain when evidence or
controls are incomplete. `verify_narrative` enforces this before any text reaches an approver:

- groundedness: every number in the narrative must match a figure in the package, at the
  precision the narrative states it;
- PII control: the narrative must not contain identifiers (email, phone, ID numbers, account ids);
- abstention: an incomplete package, or an UNRECONCILED headline used in the text, fails the check.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Iterable, Sequence

LABELS = ("fact", "model_calculated", "ai_explanation", "mdp_candidate_action")

PII_PATTERNS = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"),
    "vn_phone": re.compile(r"(?<!\d)(?:\+84|0)[35789]\d{8}(?!\d)"),
    "citizen_id_12": re.compile(r"(?<!\d)\d{12}(?!\d)"),
    "id_card_9": re.compile(r"(?<!\d)\d{9}(?!\d)"),
    "account_id": re.compile(r"\bAC\d{6}\b"),
}
_NUM = re.compile(r"(?<![\w])[-+]?\d+(?:[.,]\d+)*")


@dataclass(frozen=True)
class Figure:
    id: str
    label: str
    value: float
    unit: str                      # "VND bn", "ratio", "count", "months", "percent"
    source: str
    horizon_months: int | None = None
    status: str = "OK"             # "UNRECONCILED" blocks use as a fact

    def __post_init__(self):
        if self.label not in LABELS:
            raise ValueError(f"label must be one of {LABELS}")

    def candidates(self) -> list[float]:
        vals = [self.value]
        if self.unit == "ratio":
            vals.append(self.value * 100)
        return vals


@dataclass
class EvidencePackage:
    decision_id: str
    snapshot_month: str
    figures: list[Figure]
    complete: bool = True
    missing: list[str] = field(default_factory=list)
    allowed_literals: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["figures"] = [asdict(f) for f in self.figures]
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "EvidencePackage":
        return cls(decision_id=d["decision_id"], snapshot_month=d["snapshot_month"],
                   figures=[Figure(**f) for f in d.get("figures", [])],
                   complete=bool(d.get("complete", True)), missing=list(d.get("missing", [])),
                   allowed_literals=[float(x) for x in d.get("allowed_literals", [])])


@dataclass
class GroundingResult:
    passed: bool
    must_abstain: bool
    unmatched_numbers: list[str]
    unreconciled_used: list[str]
    pii_hits: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _parse(token: str, decimal: str) -> tuple[float, int]:
    thousands = "," if decimal == "." else "."
    t = token.replace(thousands, "")
    if decimal == ",":
        t = t.replace(",", ".")
    decimals = len(t.split(".")[1]) if "." in t else 0
    return float(t), decimals


def numbers_in(text: str, decimal: str = ".") -> list[tuple[str, float, int]]:
    out = []
    for m in _NUM.finditer(text):
        tok = m.group(0)
        try:
            v, d = _parse(tok, decimal)
        except ValueError:
            continue
        out.append((tok, v, d))
    return out


def _matches(value: float, decimals: int, candidates: Iterable[float]) -> bool:
    tol = 0.5 * 10 ** (-decimals) + 1e-9
    return any(abs(abs(c) - abs(value)) <= tol for c in candidates)


def pii_hits(text: str) -> list[str]:
    return [name for name, rx in PII_PATTERNS.items() if rx.search(text)]


def verify_narrative(text: str, package: EvidencePackage, decimal: str = ".") -> GroundingResult:
    if not package.complete:
        return GroundingResult(False, True, [], [], pii_hits(text))
    ok_figs = [f for f in package.figures if f.status == "OK"]
    bad_figs = [f for f in package.figures if f.status != "OK"]
    unmatched, unreconciled = [], []
    for tok, v, d in numbers_in(text, decimal):
        if _matches(v, d, package.allowed_literals):
            continue
        if any(_matches(v, d, f.candidates()) for f in ok_figs):
            continue
        hit = [f.id for f in bad_figs if _matches(v, d, f.candidates())]
        (unreconciled.extend(hit) if hit else unmatched.append(tok))
    pii = pii_hits(text)
    passed = not unmatched and not unreconciled and not pii
    return GroundingResult(passed, False, unmatched, sorted(set(unreconciled)), pii)


def figures_from(source: str, label: str, values: dict, unit: str, horizon: int | None,
                 status: str = "OK", skip: Sequence[str] = ()) -> list[Figure]:
    return [Figure(k, label, float(v), unit, source, horizon, status)
            for k, v in values.items() if k not in skip and isinstance(v, (int, float))]
