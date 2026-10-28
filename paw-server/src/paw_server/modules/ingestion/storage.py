"""Object storage adapter (D003). Originals are immutable and content-addressed."""

import hashlib
import io
from typing import IO, Protocol

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from paw_server.core.config import Settings
from paw_server.modules.ingestion.errors import PermanentIngestError, TransientIngestError


class ObjectStore(Protocol):
    def put(
        self, key: str, body: IO[bytes], *, size: int, sha256: str, content_type: str
    ) -> None: ...
    def get(self, key: str) -> bytes: ...


class MemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, body: IO[bytes], *, size: int, sha256: str, content_type: str) -> None:
        data = body.read()
        if len(data) != size or hashlib.sha256(data).hexdigest() != sha256:
            raise PermanentIngestError("checksum_mismatch", "stored bytes do not match checksum")
        self.objects[key] = data

    def get(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError as exc:
            raise PermanentIngestError("original_missing", "original object not found") from exc


class S3ObjectStore:
    def __init__(self, s: Settings) -> None:
        self.bucket = s.s3_bucket
        self._c = boto3.client(
            "s3",
            endpoint_url=s.s3_endpoint_url,
            aws_access_key_id=s.s3_access_key or None,
            aws_secret_access_key=s.s3_secret_key or None,
            region_name=s.s3_region,
            config=Config(
                retries={"max_attempts": 2},
                connect_timeout=3,
                read_timeout=15,
                s3={"addressing_style": "path"},
            ),
        )
        if s.s3_create_bucket:
            self.ensure_bucket()

    def ensure_bucket(self) -> None:
        try:
            self._c.head_bucket(Bucket=self.bucket)
        except ClientError:
            self._c.create_bucket(Bucket=self.bucket)

    def put(self, key: str, body: IO[bytes], *, size: int, sha256: str, content_type: str) -> None:
        try:
            self._c.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType=content_type,
                Metadata={"sha256": sha256},
            )
            head = self._c.head_object(Bucket=self.bucket, Key=key)
        except (BotoCoreError, ClientError) as exc:
            raise TransientIngestError("object store write failed") from exc
        if head["ContentLength"] != size or head.get("Metadata", {}).get("sha256") != sha256:
            raise PermanentIngestError("checksum_mismatch", "stored object failed verification")

    def get(self, key: str) -> bytes:
        try:
            buf = io.BytesIO()
            self._c.download_fileobj(self.bucket, key, buf)
            return buf.getvalue()
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
                raise PermanentIngestError("original_missing", "original object not found") from exc
            raise TransientIngestError("object store read failed") from exc
        except BotoCoreError as exc:
            raise TransientIngestError("object store read failed") from exc


def build_store(s: Settings) -> ObjectStore:
    return S3ObjectStore(s)
