"""Run the whole decision loop offline on the SYNTHETIC demo sample (invented numbers), no AWS needed.

Steps: 1 DQ gate -> 2 Markov -> 3 MDP -> 4 Monte Carlo (chunked) -> package -> 5 grounding check of a
narrative -> 6 approval is simulated -> 7 the actions that would be emitted. Writes everything under
--root (default ./.local-run) using the same S3 key layout as the cloud run.
Usage: python tools/local_run.py [--root DIR] [--chunks 4] [--paths 2000]"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
for p in ("engines", "jobs", "services"):
    sys.path.insert(0, str(ROOT / p))

from adi_engines import dq  # noqa: E402
from adi_engines import evidence as ev  # noqa: E402
from adi_engines.reference import SAMPLE_MONTH  # noqa: E402
from adi_engines.synthetic import snapshot  # noqa: E402
from adi_jobs.store import LocalStore  # noqa: E402
from adi_jobs.tasks import task_markov, task_mc_chunk, task_mdp, task_package  # noqa: E402


def main(argv=None) -> int:
    a = argparse.ArgumentParser()
    a.add_argument("--root", default=str(ROOT / ".local-run"))
    a.add_argument("--chunks", type=int, default=4)
    a.add_argument("--paths", type=int, default=2000)
    args = a.parse_args(argv)
    store, run, cfg = LocalStore(args.root), "s3://adi-curated/runs/local-demo", "s3://adi-curated/config"
    rows = snapshot()
    gate = dq.run_gate(rows, SAMPLE_MONTH, SAMPLE_MONTH)
    store.put_json(f"{run}/evidence/dq_report.json", gate.to_dict())
    print(f"1 DQ gate: passed={gate.passed} records={gate.records} exceptions={[e['rule'] for e in gate.exceptions]}")
    if not gate.passed:
        return 2
    store.put_bytes(f"{run}/snapshot.jsonl", "".join(json.dumps(r) + "\n" for r in rows).encode())
    for name in ("economics", "actions", "prior_matrix", "published_kpis"):
        store.put_bytes(f"{cfg}/{name}.json", (ROOT / "config" / "example" / f"{name}.json").read_bytes())
    store.put_json(f"{cfg}/montecarlo.json", {"paths": args.paths, "horizon_months": 12, "seed": 20300131})
    # EXAMPLE 12-month revenue budget for the synthetic sample; Finance sets the real one.
    store.put_json(f"{cfg}/budget.json", {"revenue_budget": 17.2, "horizon_months": 12})

    mk = task_markov(store, run, cfg)
    s = mk["summary"]
    print(f"2 Markov: {s['accounts']} accounts, forward {s['rolled_forward']}, recovered {s['recovered']}, "
          f"net {s['net_deterioration']:.1%}, smoothed rows {mk['smoothed_rows']}")
    out = f"{run}/r1"
    md = task_mdp(store, run, cfg, out)
    print(f"3 MDP: uplift {md['uplift']:.3f} bn over {md['horizon_months']} months, binding {len(md['binding_constraints'])} "
          f"constraints, refused {sorted(md['rejected_actions'])}, effects are assumptions={md['effects_are_assumptions']}")
    for i in range(args.chunks):
        task_mc_chunk(store, run, cfg, i, args.chunks, out)
    pkg = task_package(store, run, cfg, args.chunks, out)
    m = pkg["mc_summary"]
    print(f"4 Monte Carlo ({m['paths']} paths, {m['horizon_months']} months): revenue P5/P50/P95 "
          f"{m['revenue_p5']:.2f} / {m['revenue_p50']:.2f} / {m['revenue_p95']:.2f} bn, "
          f"P(miss budget) {m['p_miss_revenue_budget']:.0%}; "
          f"headline {pkg['reconciliation']['published']:.2f} is {pkg['reconciliation']['status']}")

    figs = {f["id"]: f for f in pkg["figures"]}
    good = (f"[fact] {figs['rolled_forward']['value']:.0f} accounts rolled forward and "
            f"{figs['recovered']['value']:.0f} recovered. [model_calculated] Over 12 months the median revenue is "
            f"VND {figs['revenue_p50']['value']:.2f} bn.")
    bad = f"[fact] Risk-adjusted net profit is {pkg['reconciliation']['published']:.2f} VND B this month."
    p = ev.EvidencePackage.from_dict(pkg)
    print(f"5 grounding: grounded narrative passed={ev.verify_narrative(good, p).passed}; "
          f"narrative stating the unreconciled headline passed={ev.verify_narrative(bad, p).passed} "
          f"(unreconciled used: {ev.verify_narrative(bad, p).unreconciled_used})")

    actions = sorted({a for sh in pkg["candidate_policy"].values() for a, w in sh.items() if a != "baseline"})
    print("6 approval: simulated 'approve' for effective period 2030-02")
    print(f"7 actions that would be emitted: {actions} (RECORD_ONLY until downstream systems exist)")
    print(f"evidence written under {Path(args.root) / 'adi-curated' / 'runs' / 'local-demo'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
