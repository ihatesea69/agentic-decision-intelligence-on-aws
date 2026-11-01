"""Compute tasks. Each reads its inputs from the run prefix and the config prefix and writes one
artifact. A task that cannot produce a valid result raises FailClosed: the container exits with
code 2, the SageMaker or Batch job fails, and the workflow takes the fail-closed branch.

Layout (one Step Functions execution = one run; each approval round has its own prefix so a
revise never overwrites the evidence an approver already saw):
  {run}/snapshot.jsonl            curated, de-identified monthly snapshot (step 1 output)
  {run}/markov.json               step 2
  {out}/mdp.json                  step 3        (out = {run}/r{round})
  {out}/mc/chunk-0000.npz ...     step 4, one per AWS Batch array child
  {out}/package.json              evidence package for steps 5 and 6
Config prefix (owned by Risk and Finance):
  economics.json, actions.json, montecarlo.json, budget.json (optional),
  prior_matrix.json (optional), baseline.json (optional), published_kpis.json (optional)
"""
from __future__ import annotations

import dataclasses

import numpy as np

from adi_engines import drift, markov, mdp, pnl
from adi_engines import evidence as ev
from adi_engines import montecarlo as mc
from adi_engines.mdp import ActionSpec, Economics


class FailClosed(Exception):
    pass


def _clean(d: dict) -> dict:
    """Config files may carry "_note" keys for humans; drop them before use."""
    return {k: v for k, v in d.items() if not k.startswith("_")}


def _econ(store, cfg) -> Economics:
    return Economics(**_clean(store.get_json(f"{cfg}/economics.json")))


def _spec(s: dict) -> ActionSpec:
    return ActionSpec(s["name"], tuple(s["states"]), dict(s["shift"]), float(s["cost_per_account"]),
                      s.get("capacity_per_month"))


def _actions(store, cfg) -> dict:
    a = _clean(store.get_json(f"{cfg}/actions.json"))
    return {**a, "specs": [_spec(s) for s in a.get("specs", [])]}


def _mc_config(store, cfg) -> mc.MCConfig:
    return mc.MCConfig(**_clean(store.get_json_opt(f"{cfg}/montecarlo.json") or {}))


def task_markov(store, run: str, cfg: str, min_n: int = 30) -> dict:
    rows = store.read_jsonl(f"{run}/snapshot.jsonl")
    if not rows:
        raise FailClosed("empty snapshot")
    counts = markov.count_transitions((r["from_state"], r["to_state"]) for r in rows)
    prior = store.get_json_opt(f"{cfg}/prior_matrix.json")
    doc = {"snapshot_month": rows[0].get("snapshot_month"),
           "n0": {s: int(n) for s, n in zip(markov.FROM_STATES, counts.sum(axis=1))}}
    try:
        res = markov.prepare_for_mdp(counts, prior_matrix=prior["matrix"] if prior else None, min_n=min_n)
    except ValueError as e:
        store.put_json(f"{run}/markov.json", {**doc, "mdp_ready": False, "error": str(e),
                                               "counts": counts.astype(float).tolist()})
        raise FailClosed(str(e)) from e
    base = store.get_json_opt(f"{cfg}/baseline.json")
    doc.update(res.to_dict())
    doc["drift"] = drift.check(base["matrix"], base["mix"], counts) if base else None
    lo, hi = markov.interval_matrix(counts)
    doc["interval_low"], doc["interval_high"] = lo.tolist(), hi.tolist()
    store.put_json(f"{run}/markov.json", doc)
    return doc


def task_mdp(store, run: str, cfg: str, out: str | None = None) -> dict:
    out = out or run
    mk = store.get_json(f"{run}/markov.json")
    if not mk.get("mdp_ready"):
        raise FailClosed("markov output is not MDP-ready")
    a = _actions(store, cfg)
    allowed, rejected = mdp.check_actions([s.name for s in a["specs"]], bool(a.get("origination_frozen", True)))
    specs = [s for s in a["specs"] if s.name in allowed]
    try:
        res = mdp.solve(mk["matrix"], mk["n0"], _econ(store, cfg), actions=specs,
                        horizon=int(a.get("horizon_months", 12)), max_wo_per_month=a.get("max_wo_per_month"),
                        effects_measured=bool(a.get("effects_measured", False)), rejected_actions=rejected)
    except mdp.MDPInfeasible as e:
        store.put_json(f"{out}/mdp.json", {"feasible": False, "error": str(e), "rejected_actions": rejected})
        raise FailClosed(str(e)) from e
    doc = {"feasible": True, **res.to_dict(), "actions": [dataclasses.asdict(s) for s in specs]}
    store.put_json(f"{out}/mdp.json", doc)
    return doc


def _policy(mdp_doc: dict) -> dict:
    return {s: {a: w for a, w in shares.items() if w > 1e-9} for s, shares in mdp_doc["first_month_policy"].items()}


def task_mc_chunk(store, run: str, cfg: str, index: int, chunks: int, out: str | None = None) -> str:
    out = out or run
    mk = store.get_json(f"{run}/markov.json")
    md = store.get_json(f"{out}/mdp.json")
    econ, c = _econ(store, cfg), _mc_config(store, cfg)
    specs = [_spec(s) for s in md.get("actions", [])]
    arrays = {}
    for variant, policy in (("candidate", _policy(md)), ("baseline", None)):
        part = mc.run_chunk(index, chunks, mk["counts"], mk["n0"], econ, c, policy=policy, actions=specs)
        for b, block in part["blocks"].items():
            for k, v in block.items():
                arrays[f"{variant}__{b}__{k}"] = v
    uri = f"{out}/mc/chunk-{index:04d}.npz"
    store.put_npz(uri, arrays)
    return uri


def _merge_variant(chunk_arrays: list[dict], variant: str, c: mc.MCConfig) -> dict:
    blocks: dict[int, dict] = {}
    for arrays in chunk_arrays:
        for key, v in arrays.items():
            var, b, k = key.split("__")
            if var == variant:
                blocks.setdefault(int(b), {})[k] = v
    return mc.merge([{"blocks": blocks}], c)


def task_package(store, run: str, cfg: str, chunks: int, out: str | None = None) -> dict:
    out = out or run
    c = _mc_config(store, cfg)
    uris = store.list(f"{out}/mc/chunk-")
    if len(uris) != chunks:
        raise FailClosed(f"expected {chunks} Monte Carlo chunks, found {len(uris)}")
    arrays = [store.get_npz(u) for u in uris]
    try:
        cand, base = _merge_variant(arrays, "candidate", c), _merge_variant(arrays, "baseline", c)
    except ValueError as e:
        raise FailClosed(str(e)) from e
    budget = _clean(store.get_json_opt(f"{cfg}/budget.json") or {})
    summary = mc.summarize(cand, c, budget.get("revenue_budget"), budget.get("horizon_months"))
    base_summary = mc.summarize(base, c)
    uplift = cand["net_profit"] - base["net_profit"]
    mk, md = store.get_json(f"{run}/markov.json"), store.get_json(f"{out}/mdp.json")
    h = c.horizon_months
    roll = mk["summary"]
    figs = [
        ev.Figure("net_deterioration", "model_calculated", roll["net_deterioration"], "ratio", "markov", 1),
        ev.Figure("rolled_forward", "fact", roll["rolled_forward"], "count", "snapshot", 1),
        ev.Figure("recovered", "fact", roll["recovered"], "count", "snapshot", 1),
        ev.Figure("accounts", "fact", roll["accounts"], "count", "snapshot", 1),
        ev.Figure("mdp_value_uplift", "mdp_candidate_action", md["uplift"], "VND bn", "mdp", md["horizon_months"]),
    ]
    figs += [ev.Figure(k, "model_calculated", v, "ratio" if k == "p_miss_revenue_budget" else "VND bn",
                       "monte carlo", h) for k, v in summary.items()
             if k not in ("horizon_months", "paths", "seed", "revenue_budget")]
    figs += [ev.Figure("baseline_net_profit_p50", "model_calculated", base_summary["net_profit_p50"], "VND bn", "monte carlo", h),
             ev.Figure("uplift_p5", "model_calculated", float(np.percentile(uplift, 5)), "VND bn", "monte carlo", h),
             ev.Figure("uplift_p50", "model_calculated", float(np.percentile(uplift, 50)), "VND bn", "monte carlo", h),
             ev.Figure("p_uplift_negative", "model_calculated", float((uplift < 0).mean()), "ratio", "monte carlo", h)]
    recon = None
    kpis = store.get_json_opt(f"{cfg}/published_kpis.json")
    if kpis:
        recon = pnl.reconcile(_clean(kpis))
        figs.append(ev.Figure("risk_adjusted_net_profit", "fact", kpis["risk_adjusted_net_profit"], "VND bn",
                              "finance KPI", 1, "OK" if recon["status"] == "RECONCILED" else recon["status"]))
    missing = [] if md.get("feasible") and mk.get("mdp_ready") else ["mdp"]
    pkg = ev.EvidencePackage(decision_id=run.rstrip("/").rsplit("/", 1)[-1], snapshot_month=mk["snapshot_month"],
                             figures=figs, complete=not missing, missing=missing,
                             allowed_literals=[float(h), 5.0, 50.0, 95.0, float(c.paths)]
                             + [float(x) for x in str(mk["snapshot_month"]).split("-")])
    doc = pkg.to_dict()
    doc.update({"round_prefix": out, "mc_summary": summary, "mc_baseline_summary": base_summary,
                "reconciliation": recon, "effects_are_assumptions": md.get("effects_are_assumptions", True),
                "rejected_actions": md.get("rejected_actions", {}), "drift": mk.get("drift"),
                "candidate_policy": _policy(md)})
    store.put_json(f"{out}/package.json", doc)
    return doc
