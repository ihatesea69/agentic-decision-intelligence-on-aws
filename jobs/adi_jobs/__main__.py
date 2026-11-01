"""python -m adi_jobs <markov|mdp|mc-chunk|package> [--run s3://...] [--config s3://...] [--out s3://...]

Paths come from flags or from the environment (ADI_RUN, ADI_CONFIG, ADI_OUT, MC_CHUNKS,
AWS_BATCH_JOB_ARRAY_INDEX). The workflow passes them as environment variables because Step Functions
cannot place JsonPath values inside a container command array.
--out is the per-round prefix ({run}/r{round}); markov always writes to --run.
Exit codes: 0 ok, 2 fail closed (the workflow takes the fail-closed branch)."""
from __future__ import annotations

import argparse
import os
import sys

from .store import LocalStore, S3Store
from .tasks import FailClosed, task_markov, task_mc_chunk, task_mdp, task_package


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="adi_jobs")
    p.add_argument("task", choices=["markov", "mdp", "mc-chunk", "package"])
    p.add_argument("--run", default=os.environ.get("ADI_RUN"))
    p.add_argument("--config", default=os.environ.get("ADI_CONFIG"))
    p.add_argument("--out", default=os.environ.get("ADI_OUT") or None)
    p.add_argument("--chunks", type=int, default=int(os.environ.get("MC_CHUNKS", "1")))
    p.add_argument("--index", type=int, default=int(os.environ.get("AWS_BATCH_JOB_ARRAY_INDEX", "0")))
    p.add_argument("--local-root", default=os.environ.get("ADI_LOCAL_ROOT"))
    a = p.parse_args(argv)
    if not a.run or not a.config:
        p.error("--run and --config (or ADI_RUN and ADI_CONFIG) are required")
    store = LocalStore(a.local_root) if a.local_root else S3Store()
    try:
        if a.task == "markov":
            task_markov(store, a.run, a.config)
        elif a.task == "mdp":
            task_mdp(store, a.run, a.config, a.out)
        elif a.task == "mc-chunk":
            task_mc_chunk(store, a.run, a.config, a.index, a.chunks, a.out)
        else:
            task_package(store, a.run, a.config, a.chunks, a.out)
    except FailClosed as e:
        print(f"FAIL_CLOSED: {e}", file=sys.stderr)
        return 2
    print(f"OK {a.task}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
