"""Object store used by the container tasks: S3 in AWS, a folder for local runs, memory for tests."""
from __future__ import annotations

import io
import json
import os
from pathlib import Path


class Store:
    def get_bytes(self, uri: str) -> bytes: ...
    def put_bytes(self, uri: str, data: bytes) -> None: ...
    def list(self, prefix: str) -> list[str]: ...
    def exists(self, uri: str) -> bool: ...

    def get_json(self, uri: str):
        return json.loads(self.get_bytes(uri).decode("utf-8"))

    def get_json_opt(self, uri: str):
        return self.get_json(uri) if self.exists(uri) else None

    def put_json(self, uri: str, obj) -> None:
        self.put_bytes(uri, json.dumps(obj, sort_keys=True, default=float).encode("utf-8"))

    def read_jsonl(self, uri: str) -> list[dict]:
        return [json.loads(x) for x in self.get_bytes(uri).decode("utf-8").splitlines() if x.strip()]

    def put_npz(self, uri: str, arrays: dict) -> None:
        import numpy as np
        buf = io.BytesIO()
        np.savez_compressed(buf, **arrays)
        self.put_bytes(uri, buf.getvalue())

    def get_npz(self, uri: str) -> dict:
        import numpy as np
        with np.load(io.BytesIO(self.get_bytes(uri))) as z:
            return {k: z[k] for k in z.files}


class S3Store(Store):
    def __init__(self):
        import boto3
        self.s3 = boto3.client("s3")

    @staticmethod
    def _split(uri: str):
        if not uri.startswith("s3://"):
            raise ValueError(f"not an s3 uri: {uri}")
        b, _, k = uri[5:].partition("/")
        return b, k

    def get_bytes(self, uri):
        b, k = self._split(uri)
        return self.s3.get_object(Bucket=b, Key=k)["Body"].read()

    def put_bytes(self, uri, data):
        # bucket default encryption (customer-managed KMS key) applies; see services/adi_services/aws.py
        b, k = self._split(uri)
        self.s3.put_object(Bucket=b, Key=k, Body=data, ChecksumAlgorithm="SHA256")

    def exists(self, uri):
        b, k = self._split(uri)
        try:
            self.s3.head_object(Bucket=b, Key=k)
            return True
        except self.s3.exceptions.ClientError:
            return False

    def list(self, prefix):
        b, k = self._split(prefix)
        out, token = [], None
        while True:
            kw = {"Bucket": b, "Prefix": k}
            if token:
                kw["ContinuationToken"] = token
            page = self.s3.list_objects_v2(**kw)
            out += [f"s3://{b}/{o['Key']}" for o in page.get("Contents", [])]
            if not page.get("IsTruncated"):
                return sorted(out)
            token = page["NextContinuationToken"]


class LocalStore(Store):
    """Maps s3://bucket/key to <root>/bucket/key for local runs."""

    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)

    def _p(self, uri: str) -> Path:
        return self.root / uri.replace("s3://", "", 1)

    def get_bytes(self, uri):
        return self._p(uri).read_bytes()

    def put_bytes(self, uri, data):
        p = self._p(uri)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def exists(self, uri):
        return self._p(uri).exists()

    def list(self, prefix):
        base = self._p(prefix)
        parent = base if base.is_dir() else base.parent
        if not parent.exists():
            return []
        out = []
        for f in parent.rglob("*"):
            if f.is_file():
                uri = "s3://" + f.relative_to(self.root).as_posix()
                if uri.startswith(prefix):
                    out.append(uri)
        return sorted(out)


class MemoryStore(Store):
    def __init__(self):
        self.objects: dict[str, bytes] = {}

    def get_bytes(self, uri):
        return self.objects[uri]

    def put_bytes(self, uri, data):
        self.objects[uri] = bytes(data)

    def exists(self, uri):
        return uri in self.objects

    def list(self, prefix):
        return sorted(k for k in self.objects if k.startswith(prefix))
