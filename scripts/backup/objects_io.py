"""Runs INSIDE the paw-server image (it has boto3 and the app settings): export or import the object bucket.

python objects_io.py export /stage/objects   -> files + objects.json {key,size,sha256,content_type}
python objects_io.py import /stage/objects   -> creates the bucket if absent, uploads every file
"""

import hashlib
import json
import sys
from pathlib import Path

import boto3
from botocore.config import Config

from paw_server.core.config import get_settings


def client_and_bucket():
    s = get_settings()
    c = boto3.client(
        "s3",
        endpoint_url=s.s3_endpoint_url,
        aws_access_key_id=s.s3_access_key,
        aws_secret_access_key=s.s3_secret_key,
        region_name=s.s3_region,
        config=Config(s3={"addressing_style": "path"}),
    )
    return c, s.s3_bucket


def export(dest: Path) -> None:
    c, bucket = client_and_bucket()
    dest.mkdir(parents=True, exist_ok=True)
    items = []
    for page in c.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        for o in page.get("Contents", []):
            key = o["Key"]
            body = c.get_object(Bucket=bucket, Key=key)
            data = body["Body"].read()
            path = dest / "files" / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            items.append(
                {
                    "key": key,
                    "size": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "content_type": body.get("ContentType", "application/octet-stream"),
                }
            )
    (dest / "objects.json").write_text(json.dumps(sorted(items, key=lambda i: i["key"])))
    print(f"exported {len(items)} objects")


def import_(src: Path) -> None:
    c, bucket = client_and_bucket()
    try:
        c.head_bucket(Bucket=bucket)
    except Exception:
        c.create_bucket(Bucket=bucket)
    items = json.loads((src / "objects.json").read_text())
    for i in items:
        c.put_object(
            Bucket=bucket,
            Key=i["key"],
            Body=(src / "files" / i["key"]).read_bytes(),
            ContentType=i["content_type"],
        )
    print(f"imported {len(items)} objects")


def check(src: Path) -> None:
    """Every object listed in objects.json must exist in the bucket with the same bytes."""
    c, bucket = client_and_bucket()
    items = json.loads((src / "objects.json").read_text())
    bad = []
    for i in items:
        try:
            data = c.get_object(Bucket=bucket, Key=i["key"])["Body"].read()
        except Exception:
            bad.append(i["key"])
            continue
        if hashlib.sha256(data).hexdigest() != i["sha256"]:
            bad.append(i["key"])
    if bad:
        print(f"MISMATCH {len(bad)} objects: {bad[:5]}")
        sys.exit(3)
    print(f"verified {len(items)} objects")


if __name__ == "__main__":
    {"export": export, "import": import_, "check": check}[sys.argv[1]](Path(sys.argv[2]))
