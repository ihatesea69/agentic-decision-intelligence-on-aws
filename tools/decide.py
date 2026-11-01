"""Approver client for the decision API (SigV4 with your Identity Center credentials).

  python tools/decide.py --api <DecisionApiUrl> --id <decision_id> status
  python tools/decide.py --api <url> --id <id> approve --period 2026-08 --rationale "..."
  python tools/decide.py --api <url> --id <id> revise --rationale "..."
  python tools/decide.py --api <url> --id <id> reject --rationale "..."
Run `aws sso login --profile <approver-profile>` first and pass --profile."""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

import boto3
from botocore.auth import SigV4Auth
from botocore.awsrequest import AWSRequest


def call(api: str, method: str, path: str, body: dict | None, profile: str | None, region: str):
    session = boto3.Session(profile_name=profile, region_name=region)
    url = api.rstrip("/") + path
    data = json.dumps(body).encode() if body is not None else None
    req = AWSRequest(method=method, url=url, data=data, headers={"Content-Type": "application/json"})
    SigV4Auth(session.get_credentials().get_frozen_credentials(), "execute-api", region).add_auth(req)
    r = urllib.request.Request(url, data=data, method=method, headers=dict(req.headers))
    try:
        with urllib.request.urlopen(r, timeout=15) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api", required=True)
    p.add_argument("--id", required=True)
    p.add_argument("--profile")
    p.add_argument("--region", default="ap-southeast-1")
    p.add_argument("action", choices=["status", "approve", "revise", "reject"])
    p.add_argument("--rationale", default="")
    p.add_argument("--period")
    a = p.parse_args(argv)
    if a.action == "status":
        code, body = call(a.api, "GET", f"/decisions/{a.id}", None, a.profile, a.region)
    else:
        payload = {"decision": a.action, "rationale": a.rationale}
        if a.period:
            payload["effective_period"] = a.period
        code, body = call(a.api, "POST", f"/decisions/{a.id}/decision", payload, a.profile, a.region)
    print(code, json.dumps(body, indent=2))
    return 0 if code < 400 else 1


if __name__ == "__main__":
    sys.exit(main())
