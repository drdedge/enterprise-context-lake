"""Where the lake keeps files: uploaded evidence and the records describing them (ADR-010).

``LocalBlobStore`` writes under a folder (the prototype's behaviour). ``S3BlobStore`` writes to one S3
bucket under a key prefix, with server-side encryption under the lake's KMS key, so reading an object
needs both S3 read access and ``kms:Decrypt`` on that key. Text and metadata about uploads stay out
of SQL Server; the database only holds an ``s3://`` link in the claim's provenance.

Settings (environment): ``LAKE_S3_BUCKET``, ``LAKE_S3_PREFIX`` (default ``context-lake/``),
``LAKE_S3_KMS_KEY_ID`` (defaults to ``LAKE_KMS_KEY_ID``), ``LAKE_S3_REGION`` / ``AWS_REGION``,
``LAKE_S3_ENDPOINT_URL`` (a local stand-in for tests). Credentials come from the usual AWS chain
(the app server's IAM role); none are read from configuration here.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Protocol

from context_core.compression import atomic_write_bytes, to_posix_path


class BlobStore(Protocol):
    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str: ...

    def get(self, key: str) -> bytes: ...

    def list_keys(self, prefix: str) -> list[str]: ...

    def uri_for(self, key: str) -> str: ...


def _clean_key(key: str) -> str:
    parts = [p for p in key.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    if not parts:
        raise ValueError("Empty blob key")
    return "/".join(parts)


class LocalBlobStore:
    """Files under a local folder. Keys are relative paths; ``..`` is never followed."""

    def __init__(self, root: Path, *, repo_root: Path | None = None) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._repo_root = repo_root

    def _path(self, key: str) -> Path:
        return self.root / _clean_key(key)

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        atomic_write_bytes(self._path(key), data)
        return self.uri_for(key)

    def get(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def list_keys(self, prefix: str) -> list[str]:
        base = self.root / _clean_key(prefix) if prefix.strip("/") else self.root
        if not base.exists():
            return []
        return sorted(to_posix_path(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file())

    def uri_for(self, key: str) -> str:
        path = self._path(key)
        if self._repo_root is not None:
            try:
                return to_posix_path(path.resolve().relative_to(self._repo_root.resolve()))
            except ValueError:
                pass
        return f"uploads/{_clean_key(key).split('/')[-1]}"


class S3BlobStore:
    """Objects in one S3 bucket, encrypted at rest with SSE-KMS under the lake's key."""

    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "context-lake/",
        kms_key_id: str | None = None,
        region_name: str | None = None,
        endpoint_url: str | None = None,
        client: Any = None,
    ) -> None:
        if not bucket:
            raise ValueError("An S3 bucket name is required")
        self.bucket = bucket
        self.prefix = (prefix.strip("/") + "/") if prefix.strip("/") else ""
        self.kms_key_id = kms_key_id
        if client is None:
            import boto3

            client = boto3.client("s3", region_name=region_name, endpoint_url=endpoint_url)
        self._s3 = client

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "S3BlobStore":
        e = dict(os.environ if env is None else env)
        return cls(
            e.get("LAKE_S3_BUCKET", ""),
            prefix=e.get("LAKE_S3_PREFIX", "context-lake/"),
            kms_key_id=e.get("LAKE_S3_KMS_KEY_ID") or e.get("LAKE_KMS_KEY_ID") or None,
            region_name=e.get("LAKE_S3_REGION") or e.get("AWS_REGION") or None,
            endpoint_url=e.get("LAKE_S3_ENDPOINT_URL") or None,
        )

    def _key(self, key: str) -> str:
        return self.prefix + _clean_key(key)

    def put(self, key: str, data: bytes, *, content_type: str = "application/octet-stream") -> str:
        extra: dict[str, Any] = {"ServerSideEncryption": "aws:kms"}
        if self.kms_key_id:
            extra["SSEKMSKeyId"] = self.kms_key_id
        self._s3.put_object(Bucket=self.bucket, Key=self._key(key), Body=data, ContentType=content_type, **extra)
        return self.uri_for(key)

    def get(self, key: str) -> bytes:
        return self._s3.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()

    def list_keys(self, prefix: str) -> list[str]:
        full = self.prefix + (_clean_key(prefix) + "/" if prefix.strip("/") else "")
        keys: list[str] = []
        token: str | None = None
        while True:
            kw: dict[str, Any] = {"Bucket": self.bucket, "Prefix": full}
            if token:
                kw["ContinuationToken"] = token
            resp = self._s3.list_objects_v2(**kw)
            keys.extend(o["Key"][len(self.prefix) :] for o in resp.get("Contents", []))
            if not resp.get("IsTruncated"):
                break
            token = resp.get("NextContinuationToken")
        return sorted(keys)

    def uri_for(self, key: str) -> str:
        return f"s3://{self.bucket}/{self._key(key)}"


def blob_store_from_env(local_root: Path, *, repo_root: Path | None = None) -> BlobStore:
    """S3 when ``LAKE_S3_BUCKET`` is set, otherwise the local folder."""
    if os.environ.get("LAKE_S3_BUCKET", "").strip():
        return S3BlobStore.from_env()
    return LocalBlobStore(local_root, repo_root=repo_root)


def put_json(store: BlobStore, key: str, payload: Any) -> str:
    return store.put(key, json.dumps(payload, sort_keys=True).encode("utf-8"), content_type="application/json")


def get_json(store: BlobStore, key: str) -> Any:
    return json.loads(store.get(key).decode("utf-8"))


__all__ = ["BlobStore", "LocalBlobStore", "S3BlobStore", "blob_store_from_env", "get_json", "put_json"]
