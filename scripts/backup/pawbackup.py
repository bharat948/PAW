"""PAW home backup / restore (PAW-001D-02).

Run:  uv run --project paw-server --with pyrage python scripts/backup/pawbackup.py <command>

  keygen            print a new age identity (PRIVATE, store off-laptop) and recipient (public)
  run               pause the worker, dump DB + export objects, encrypt, upload, verify, prune
  list              completed backups at the destination
  prune             apply retention (never deletes the newest completed backup)
  restore ID        restore into an ISOLATED target project (does not touch the live install)
  drill ID          restore, run checks, then remove the target (measures recovery time)

Consistency: the original object is stored BEFORE its database row commits and objects are never deleted,
so a DB dump taken first and an object export taken after contain every object the dump references.
Restore re-verifies that (hashes + references) before it creates any resources.

A backup is complete only once the destination holds its manifest.json, which is written LAST.
Settings (env): PAW_HOME_ENV, PAW_BACKUP_ENDPOINT/BUCKET/ACCESS_KEY/SECRET_KEY[/REGION],
PAW_BACKUP_AGE_RECIPIENT (public key; the private key is never needed to back up),
PAW_BACKUP_STAGING, PAW_BACKUP_KEEP_DAILY (7), PAW_BACKUP_KEEP_WEEKLY (4).
"""

import argparse
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path

import boto3
import pyrage
from botocore.config import Config
from botocore.exceptions import ClientError, EndpointConnectionError

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "deploy" / "compose.home.yaml"
OBJECTS_IO = Path(__file__).with_name("objects_io.py")
FORMAT_VERSION = 1


def need(name: str) -> str:
    v = os.environ.get(name, "")
    if not v:
        sys.exit(f"error: {name} is required")
    return v


def read_env_file(path: str) -> dict[str, str]:
    out = {}
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


def dc(*args: str, project: str | None = None) -> list[str]:
    cmd = [
        "docker",
        "compose",
        "--env-file",
        need("PAW_HOME_ENV"),
        "-f",
        str(COMPOSE_FILE),
    ]
    if project:
        cmd += ["-p", project]
    return cmd + list(args)


def sh(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, cwd=ROOT, **kw)


def psql(sql: str, project: str | None = None) -> str:
    r = sh(
        dc(
            "exec",
            "-T",
            "db",
            "psql",
            "-U",
            "paw",
            "-d",
            "paw",
            "-tAc",
            sql,
            project=project,
        ),
        capture_output=True,
        text=True,
    )
    return r.stdout.strip()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def dest():
    c = boto3.client(
        "s3",
        endpoint_url=need("PAW_BACKUP_ENDPOINT"),
        aws_access_key_id=need("PAW_BACKUP_ACCESS_KEY"),
        aws_secret_access_key=need("PAW_BACKUP_SECRET_KEY"),
        region_name=os.environ.get("PAW_BACKUP_REGION", "auto"),
        config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 3}),
    )
    bucket = need("PAW_BACKUP_BUCKET")
    try:
        c.head_bucket(Bucket=bucket)
    except ClientError as e:
        code = str(e.response.get("Error", {}).get("Code"))
        if code not in ("404", "NoSuchBucket", "NotFound"):
            sys.exit(f"FAILED: cannot use the backup destination ({code}); nothing was changed")
        c.create_bucket(Bucket=bucket)
    except EndpointConnectionError:
        sys.exit("FAILED: backup destination unreachable; nothing was changed")
    return c, bucket


def local_journal():
    from paw_server.modules.sessions.journal import Journal

    return Journal(os.environ.get("PAW_DELETION_JOURNAL_DIR", ""))


def cmd_export_deletions(a) -> None:
    journal = local_journal()
    with journal.guard():
        payload = journal.export()
    target = Path(a.output)
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(payload, f)
        f.flush()
        os.fsync(f.fileno())


def cmd_sync_deletions(a) -> None:
    """Independent encrypted snapshots; pruning DB backups never removes deletion receipts."""
    while True:
        try:
            journal = local_journal()
            with journal.guard(synchronization=True):
                with journal.guard():
                    payload = journal.export()
                envf = read_env_file(need("PAW_HOME_ENV"))
                installation = envf.get("PAW_INSTALLATION_ID")
                if not installation:
                    sys.exit("PAW_INSTALLATION_ID is required for deletion journal sync")
                payload["installation_id"] = installation
                plain = json.dumps(payload, sort_keys=True).encode()
                recipient = pyrage.x25519.Recipient.from_str(need("PAW_BACKUP_AGE_RECIPIENT"))
                encrypted = pyrage.encrypt(plain, [recipient])
                digest = hashlib.sha256(encrypted).hexdigest()
                c, bucket = dest()
                prefix = f"privacy/{installation}/"
                key = prefix + digest + ".json.age"
                c.put_object(Bucket=bucket, Key=key, Body=encrypted)
                actual = c.get_object(Bucket=bucket, Key=key)["Body"].read()
                if hashlib.sha256(actual).hexdigest() != digest:
                    raise ValueError("journal_upload_hash_mismatch")
                manifest = {
                    "version": 1,
                    "key": key,
                    "sha256": digest,
                    "synced_through": payload["synced_through"],
                    "receipts": len(payload["receipts"]),
                    "installation_id": installation,
                }
                c.put_object(
                    Bucket=bucket,
                    Key=prefix + "latest.json",
                    Body=json.dumps(manifest).encode(),
                )
                status = journal.path / "sync-status.json"
                tmp = status.with_suffix(".tmp")
                tmp.write_text(json.dumps(manifest))
                os.chmod(tmp, 0o600)
                os.replace(tmp, status)
                print(
                    f"Deletion journal synchronized: {len(payload['receipts'])} receipts; through {payload['synced_through']}"
                )
        except (Exception, SystemExit):
            if not getattr(a, "watch", False):
                raise
            print("Deletion journal sync failed; retrying in 60 seconds", file=sys.stderr)
        if not getattr(a, "watch", False):
            return
        time.sleep(60)


def read_deletion_journal(path: str, identity_path: str) -> dict:
    """The supplied journal is intentionally independent of the chosen database backup."""
    from paw_server.modules.sessions.journal import Receipt

    raw = Path(path).read_bytes()
    if path.endswith(".age"):
        ident = pyrage.x25519.Identity.from_str(
            Path(identity_path).read_text().strip().splitlines()[-1]
        )
        raw = pyrage.decrypt(raw, [ident])
    payload = json.loads(raw)
    if payload.get("version") != 1 or not isinstance(payload.get("receipts"), list):
        sys.exit("FAILED: invalid deletion journal")
    for row in payload["receipts"]:
        Receipt.model_validate(row)
    return payload


def staging_root() -> Path:
    # must live under $HOME: Docker Desktop (Linux) only shares the home directory into containers
    d = Path(
        os.environ.get("PAW_BACKUP_STAGING") or Path.home() / ".local/state/paw/backup-staging"
    )
    d.mkdir(parents=True, exist_ok=True, mode=0o700)
    return d


def objects_io(cmd: str, host_dir: Path, project: str | None, rw: bool) -> None:
    mode = "rw" if rw else "ro"
    sh(
        dc(
            "run",
            "--rm",
            "--no-deps",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            "-v",
            f"{host_dir}:/stage:{mode}",
            "-v",
            f"{OBJECTS_IO}:/objects_io.py:ro",
            "api",
            "python",
            "/objects_io.py",
            cmd,
            "/stage",
            project=project,
        )
    )


# ------------------------------------------------------------------ backup


def cmd_keygen(_a) -> None:
    i = pyrage.x25519.Identity.generate()
    print(f"# recipient (public, goes on the laptop): {i.to_public()}")
    print(str(i))


def cmd_run(_a) -> None:
    started = time.time()
    envf = read_env_file(need("PAW_HOME_ENV"))
    recipient = pyrage.x25519.Recipient.from_str(need("PAW_BACKUP_AGE_RECIPIENT"))
    c, bucket = dest()  # fail early if the destination is unreachable
    ts = dt.datetime.now(dt.UTC)
    bid = f"paw-{envf.get('PAW_INSTALLATION_ID', 'unknown')}-{ts:%Y%m%dT%H%M%SZ}"
    stage = staging_root() / bid
    stage.mkdir(mode=0o700)
    print(f"backup {bid}")

    sh(dc("stop", "worker"))  # no ingestion mutating chunks while we capture
    try:
        with (stage / "db.dump").open("wb") as f:
            sh(dc("exec", "-T", "db", "pg_dump", "-U", "paw", "-Fc", "paw"), stdout=f)
        refs = json.loads(
            psql(
                "select coalesce(json_agg(json_build_object('key',object_key,'sha256',sha256)),'[]') "
                "from document_versions"
            )
        )
        meta = {
            "alembic_revision": psql("select version_num from alembic_version"),
            "postgres_version": psql("show server_version"),
            "pgvector_version": psql("select extversion from pg_extension where extname='vector'"),
            "documents": int(psql("select count(*) from documents")),
        }
        (stage / "objects").mkdir()  # created by us, not by Docker (which would make it root-owned)
        objects_io("export", stage / "objects", None, rw=True)
    finally:
        sh(dc("start", "worker"))

    objs = json.loads((stage / "objects" / "objects.json").read_text())
    have = {o["key"]: o["sha256"] for o in objs}
    missing = [r["key"] for r in refs if have.get(r["key"]) != r["sha256"]]
    if missing:
        sys.exit(
            f"FAILED: {len(missing)} referenced objects missing/mismatched in export: {missing[:5]}"
        )

    manifest = {
        "format_version": FORMAT_VERSION,
        "backup_id": bid,
        "created_at": ts.isoformat(),
        "installation_id": envf.get("PAW_INSTALLATION_ID"),
        "release_revision": envf.get("PAW_RELEASE_REVISION"),
        "db_dump_sha256": sha256_file(stage / "db.dump"),
        "object_count": len(objs),
        "referenced_objects": refs,
        **meta,
    }
    (stage / "manifest.json").write_text(json.dumps(manifest, indent=1))

    payload = stage / "payload.tar.gz"
    with tarfile.open(payload, "w:gz") as t:
        for name in ("db.dump", "objects", "manifest.json"):
            t.add(stage / name, arcname=name)
    blob = stage / "backup.tar.gz.age"
    blob.write_bytes(pyrage.encrypt(payload.read_bytes(), [recipient]))
    payload.unlink()
    blob_sha, blob_size = sha256_file(blob), blob.stat().st_size

    prefix = f"backups/{bid}/"
    c.upload_file(str(blob), bucket, prefix + "backup.tar.gz.age")
    head = c.head_object(Bucket=bucket, Key=prefix + "backup.tar.gz.age")
    if head["ContentLength"] != blob_size:
        sys.exit("FAILED: uploaded size differs; backup NOT marked complete")
    h = hashlib.sha256()
    for chunk in c.get_object(Bucket=bucket, Key=prefix + "backup.tar.gz.age")["Body"].iter_chunks(
        1 << 20
    ):
        h.update(chunk)
    if h.hexdigest() != blob_sha:
        sys.exit("FAILED: remote copy hash differs; backup NOT marked complete")

    public = {
        k: manifest[k]
        for k in (
            "format_version",
            "backup_id",
            "created_at",
            "installation_id",
            "release_revision",
            "alembic_revision",
            "documents",
            "object_count",
        )
    }
    public.update(blob_sha256=blob_sha, blob_size=blob_size)
    c.put_object(
        Bucket=bucket, Key=prefix + "manifest.json", Body=json.dumps(public).encode()
    )  # LAST
    shutil.rmtree(stage)  # only after the verified remote copy exists
    state = staging_root().parent / "last_backup.json"
    state.write_text(
        json.dumps({"backup_id": bid, "completed_at": dt.datetime.now(dt.UTC).isoformat()})
    )
    print(
        f"COMPLETE {bid}: {meta['documents']} documents, {len(objs)} objects, "
        f"{blob_size} bytes encrypted, {time.time() - started:.1f}s"
    )
    cmd_sync_deletions(argparse.Namespace(watch=False))
    cmd_prune(_a)


# ------------------------------------------------------------------ list / prune


def completed(c, bucket) -> list[dict]:
    out = []
    for page in c.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix="backups/"):
        for o in page.get("Contents", []):
            if o["Key"].endswith("/manifest.json"):
                out.append(json.loads(c.get_object(Bucket=bucket, Key=o["Key"])["Body"].read()))
    return sorted(out, key=lambda m: m["created_at"], reverse=True)


def cmd_list(_a) -> None:
    c, b = dest()
    for m in completed(c, b):
        print(
            f"{m['backup_id']}  {m['created_at']}  docs={m['documents']} objects={m['object_count']} "
            f"rev={m['alembic_revision']}"
        )


def cmd_prune(_a) -> None:
    c, bucket = dest()
    ms = completed(c, bucket)
    daily = int(os.environ.get("PAW_BACKUP_KEEP_DAILY", 7))
    weekly = int(os.environ.get("PAW_BACKUP_KEEP_WEEKLY", 4))
    keep = {m["backup_id"] for m in ms[:daily]}
    weeks: set[tuple[int, int]] = set()
    for m in ms[daily:]:
        iso = dt.datetime.fromisoformat(m["created_at"]).isocalendar()
        wk = (iso[0], iso[1])
        if wk not in weeks and len(weeks) < weekly:
            weeks.add(wk)
            keep.add(m["backup_id"])
    for m in ms:
        if m["backup_id"] in keep:
            continue
        for k in (
            "manifest.json",
            "backup.tar.gz.age",
        ):  # marker first: it is never "complete" without a blob
            c.delete_object(Bucket=bucket, Key=f"backups/{m['backup_id']}/{k}")
        print(f"pruned {m['backup_id']}")
    print(f"retention: {len(keep)} kept of {len(ms)}")


# ------------------------------------------------------------------ restore


def fetch_and_validate(bid: str, identity_path: str, work: Path) -> dict:
    """Download, decrypt, and prove the set is complete. Touches nothing but `work`."""
    c, bucket = dest()
    prefix = f"backups/{bid}/"
    try:
        public = json.loads(
            c.get_object(Bucket=bucket, Key=prefix + "manifest.json")["Body"].read()
        )
    except Exception:
        sys.exit(f"FAILED: {bid} is not a completed backup at the destination")
    blob = work / "backup.tar.gz.age"
    c.download_file(bucket, prefix + "backup.tar.gz.age", str(blob))
    if sha256_file(blob) != public["blob_sha256"]:
        sys.exit("FAILED: downloaded backup does not match its recorded hash (corrupt or tampered)")
    ident = pyrage.x25519.Identity.from_str(
        Path(identity_path).read_text().strip().splitlines()[-1]
    )
    try:
        plain = pyrage.decrypt(blob.read_bytes(), [ident])
    except Exception:
        sys.exit("FAILED: cannot decrypt with that identity")
    tarfile.open(fileobj=io.BytesIO(plain)).extractall(work / "set", filter="data")
    s = work / "set"
    m = json.loads((s / "manifest.json").read_text())
    if sha256_file(s / "db.dump") != m["db_dump_sha256"]:
        sys.exit("FAILED: database dump hash mismatch")
    objs = {o["key"]: o for o in json.loads((s / "objects" / "objects.json").read_text())}
    for k, o in objs.items():
        f = s / "objects" / "files" / k
        if not f.exists() or sha256_file(f) != o["sha256"]:
            sys.exit(f"FAILED: object {k} missing or hash mismatch")
    bad = [
        r["key"]
        for r in m["referenced_objects"]
        if objs.get(r["key"], {}).get("sha256") != r["sha256"]
    ]
    if bad:
        sys.exit(f"FAILED: {len(bad)} database-referenced objects absent from backup: {bad[:5]}")
    print(
        f"validated: dump ok, {len(objs)} objects ok, all {len(m['referenced_objects'])} references present"
    )
    return m


def volumes_for(project: str) -> tuple[str, str]:
    return f"{project}_pgdata", f"{project}_s3data"


def cmd_restore(a, teardown: bool = False) -> None:
    started = time.time()
    privacy_journal = read_deletion_journal(a.deletion_journal, a.identity)
    project = a.target_project
    if project in ("paw-home", ""):
        sys.exit("refusing: restore target must not be the live project 'paw-home'")
    pg_vol, s3_vol = volumes_for(project)
    os.environ["PAW_PGDATA_VOLUME"], os.environ["PAW_S3DATA_VOLUME"] = pg_vol, s3_vol
    for v in (pg_vol, s3_vol):
        if subprocess.run(["docker", "volume", "inspect", v], capture_output=True).returncode == 0:
            sys.exit(f"refusing: volume {v} already exists; choose a new --target-project")
    with tempfile.TemporaryDirectory(dir=staging_root()) as w:
        work = Path(w)
        m = fetch_and_validate(a.backup_id, a.identity, work)  # nothing created yet if this fails
        if (
            privacy_journal.get("installation_id")
            and privacy_journal["installation_id"] != m["installation_id"]
        ):
            sys.exit("FAILED: deletion journal belongs to another installation")
        for v in (pg_vol, s3_vol):
            sh(["docker", "volume", "create", v], stdout=subprocess.DEVNULL)
        try:
            sh(dc("up", "-d", "--wait", "db", "s3", project=project))
            with (work / "set" / "db.dump").open("rb") as f:
                sh(
                    dc(
                        "exec",
                        "-T",
                        "db",
                        "pg_restore",
                        "-U",
                        "paw",
                        "-d",
                        "paw",
                        "--no-owner",
                        "--exit-on-error",
                        project=project,
                    ),
                    stdin=f,
                )
            restored_revision = psql("select version_num from alembic_version", project)
            assert restored_revision == m["alembic_revision"], (
                "restored migration differs from backup"
            )
            sh(
                dc(
                    "run",
                    "--rm",
                    "--no-deps",
                    "api",
                    "alembic",
                    "-c",
                    "alembic.ini",
                    "upgrade",
                    "head",
                    project=project,
                )
            )
            sh(
                dc(
                    "run",
                    "--rm",
                    "--no-deps",
                    "api",
                    "python",
                    "-m",
                    "paw_server.session_restore",
                    project=project,
                ),
                input=json.dumps(privacy_journal).encode(),
            )
            os.chmod(work, 0o755)
            os.chmod(work / "set", 0o755)
            objects_io("import", work / "set" / "objects", project, rw=False)
            objects_io("check", work / "set" / "objects", project, rw=False)
            rev = psql("select version_num from alembic_version", project)
            docs = int(psql("select count(*) from documents", project))
            # The restored database is deliberately migrated before deletion replay.
            assert docs == m["documents"], f"documents {docs} != {m['documents']}"
            print(
                f"RESTORED into project '{project}' (NOT live): rev {rev}, {docs} documents, "
                f"{time.time() - started:.1f}s"
            )
        except BaseException:
            print("restore failed; target left for inspection:", project, file=sys.stderr)
            raise
        finally:
            if teardown:
                subprocess.run(dc("down", project=project), cwd=ROOT)
                subprocess.run(
                    ["docker", "volume", "rm", pg_vol, s3_vol],
                    stdout=subprocess.DEVNULL,
                )
                print("target removed")


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    for n in ("keygen", "run", "list", "prune"):
        sub.add_parser(n).set_defaults(
            fn={
                "keygen": cmd_keygen,
                "run": cmd_run,
                "list": cmd_list,
                "prune": cmd_prune,
            }[n]
        )
    export = sub.add_parser("export-deletions")
    export.add_argument("--output", required=True)
    export.set_defaults(fn=cmd_export_deletions)
    sync = sub.add_parser("sync-deletions")
    sync.add_argument(
        "--watch",
        action="store_true",
        help="retry every 60 seconds independently of database backups",
    )
    sync.set_defaults(fn=cmd_sync_deletions)
    for n, td in (("restore", False), ("drill", True)):
        q = sub.add_parser(n)
        q.add_argument("backup_id")
        q.add_argument("--identity", required=True, help="file with the PRIVATE age identity")
        q.add_argument("--target-project", default="paw-restore")
        q.add_argument(
            "--deletion-journal",
            required=True,
            help="current independent journal export (JSON or encrypted .age); never inferred from the old backup",
        )
        q.set_defaults(fn=(lambda a, td=td: cmd_restore(a, teardown=td)))
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
