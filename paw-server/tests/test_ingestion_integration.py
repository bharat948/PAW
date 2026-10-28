import hashlib

import psycopg
import pytest

from paw_server.modules.ingestion import jobs
from paw_server.modules.ingestion.errors import LeaseLost, TransientIngestError
from tests import fixtures as fx
from tests.conftest import AUTH, threads, upload

pytestmark = pytest.mark.integration

LONG = ("The observatory recorded quasar telemetry every night. " * 60).encode()


def q(env, sql, *a):
    with psycopg.connect(env.url) as c:
        return c.execute(sql, a).fetchall()


def status_of(client, doc_id):
    return client.get(f"/v1/documents/{doc_id}", headers=AUTH).json()["versions"][0]


# ---- T003: all supported formats reach COMPLETED ---------------------------------------------
CASES = [
    ("note.txt", LONG),
    ("guide.md", fx.MD_DOC),
    ("paper.pdf", fx.text_pdf(["Page one text about apples", "Page two about pears"])),
    ("people.csv", fx.CSV_DOC),
    ("items.json", fx.JSON_DOC),
    ("events.jsonl", fx.JSONL_DOC),
]


@pytest.mark.parametrize("name,data", CASES)
def test_supported_formats_complete(env, name, data):
    c = env.client()
    r = upload(c, name, data)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status_url"] == f"/v1/documents/{body['id']}" and not body["duplicate"]
    assert env.drain(env.worker()) == 1

    v = status_of(c, body["id"])
    assert v["status"] == "COMPLETED" and v["chunk_count"] > 0 and v["parser_version"]
    assert [e["status"] for e in v["events"]] == [
        "RECEIVED",
        "STORED",
        "EXTRACTING",
        "CHUNKING",
        "EMBEDDING",
        "INDEXING",
        "COMPLETED",
    ]
    # original preserved byte-for-byte with verifiable hash
    key = q(env, "SELECT object_key FROM document_versions")[0][0]
    assert env.store.objects[key] == data and v["sha256"] == hashlib.sha256(data).hexdigest()
    # all chunks embedded, listing exposes provenance but never vectors
    assert q(env, "SELECT count(*) FROM document_chunks WHERE embedding IS NULL")[0][0] == 0
    chunks = c.get(f"/v1/documents/{body['id']}/chunks", headers=AUTH).json()["items"]
    assert len(chunks) == v["chunk_count"] and "embedding" not in chunks[0]
    if name.endswith(".pdf"):
        assert [ch["page_number"] for ch in chunks] == [1, 2]
    if name.endswith(".md"):
        assert chunks[1]["section"] == "Guide > Install"


# ---- T004: unsupported / corrupt input ------------------------------------------------------
def test_image_only_pdf_marked_unsupported(env):
    c = env.client()
    doc = upload(c, "scan.pdf", fx.image_only_pdf()).json()
    env.drain(env.worker())
    v = status_of(c, doc["id"])
    assert v["status"] == "UNSUPPORTED" and v["error_code"] == "no_extractable_text"
    assert v["chunk_count"] == 0 and "OCR" in v["error_message"]
    assert (
        c.get("/v1/documents?status=UNSUPPORTED", headers=AUTH).json()["items"][0]["id"]
        == doc["id"]
    )


def test_parser_failure_keeps_original(env, monkeypatch):
    import paw_server.modules.ingestion.pipeline as p
    from paw_server.modules.ingestion.errors import PermanentIngestError

    def boom(*a, **k):
        raise PermanentIngestError("corrupt_pdf", "The PDF could not be parsed.")

    monkeypatch.setattr(p, "parse", boom)
    c = env.client()
    doc = upload(c, "a.txt", LONG).json()
    env.drain(env.worker())
    assert status_of(c, doc["id"])["status"] == "FAILED"
    assert len(env.store.objects) == 1  # original not erased


@pytest.mark.parametrize(
    "name,data,ctype,code,err",
    [
        ("../../etc/passwd.txt", b"x", "text/plain", 422, "invalid_filename"),
        ("a\\b.txt", b"x", "text/plain", 422, "invalid_filename"),
        ("pic.png", b"\x89PNG\r\n", "image/png", 415, "unsupported_format"),
        ("fake.txt", b"\x89PNG\r\n\x1a\n" + b"\x00" * 10, "text/plain", 415, "content_mismatch"),
        ("fake.pdf", b"not a pdf at all", "application/pdf", 415, "content_mismatch"),
        ("bin.txt", b"abc\x00def", "text/plain", 415, "content_mismatch"),
        ("bad.txt", b"\xff\xfe\xfa invalid", "text/plain", 422, "corrupt_file"),
        (
            "trunc.pdf",
            fx.text_pdf(["hello world from pdf"])[:-40],
            "application/pdf",
            422,
            "corrupt_file",
        ),
        ("bad.json", b'{"a": ', "application/json", 422, "corrupt_file"),
        ("empty.txt", b"", "text/plain", 422, "empty_file"),
        ("ok.txt", b"hello", "image/png", 415, "unsupported_format"),
    ],
)
def test_invalid_uploads_rejected_without_document(env, name, data, ctype, code, err):
    c = env.client()
    r = upload(c, name, data, ctype)
    assert r.status_code == code and r.json()["code"] == err, r.text
    assert q(env, "SELECT count(*) FROM documents")[0][0] == 0
    assert not env.store.objects


def test_oversize_and_auth(env):
    c = env.client(max_upload_bytes=1000)
    assert upload(c, "big.txt", b"x" * 5000, "text/plain").status_code == 413
    assert c.post("/v1/documents", files={"file": ("a.txt", b"hi")}).status_code == 401
    assert (
        c.get(
            "/v1/documents", headers={"Authorization": "Bearer wrong-token-000000000"}
        ).status_code
        == 401
    )
    assert (
        c.get("/v1/documents/00000000-0000-0000-0000-000000000000", headers=AUTH).json()["code"]
        == "not_found"
    )


# ---- T005: duplicates and concurrency --------------------------------------------------------
def test_concurrent_duplicate_uploads_and_workers(env):
    clients = [env.client() for _ in range(6)]
    res = threads([lambda c=c: upload(c, "same.txt", LONG, "text/plain") for c in clients])
    assert {r.status_code for r in res} == {202}
    assert (
        len({r.json()["id"] for r in res}) == 1 and len({r.json()["version_id"] for r in res}) == 1
    )
    assert sum(not r.json()["duplicate"] for r in res) == 1
    assert q(env, "SELECT count(*) FROM documents")[0][0] == 1
    assert q(env, "SELECT count(*) FROM ingestion_jobs")[0][0] == 1

    workers = [env.worker() for _ in range(4)]
    threads([lambda w=w: env.drain(w) for w in workers])
    rows = q(env, "SELECT status FROM document_versions")
    assert rows == [("COMPLETED",)]
    n_chunks, n_ord = q(env, "SELECT count(*), count(DISTINCT ordinal) FROM document_chunks")[0]
    assert n_chunks == n_ord > 1
    # re-upload after completion: still no new work
    again = upload(env.client(), "same.txt", LONG, "text/plain").json()
    assert again["duplicate"] and env.drain(env.worker()) == 0
    assert q(env, "SELECT count(*) FROM document_chunks")[0][0] == n_chunks


def test_idempotency_key(env):
    c = env.client()
    a = upload(c, "a.txt", b"first version text", "text/plain", {"Idempotency-Key": "k1"}).json()
    b = upload(
        c, "a.txt", b"different bytes entirely", "text/plain", {"Idempotency-Key": "k1"}
    ).json()
    assert a["version_id"] == b["version_id"] and b["duplicate"]


def test_config_change_creates_new_version_of_same_document(env):
    c1 = env.client()
    doc = upload(c1, "n.txt", LONG, "text/plain").json()
    env.drain(env.worker())
    c2 = env.client(chunk_size=500)  # different processing config, same bytes
    again = upload(c2, "n.txt", LONG, "text/plain").json()
    assert (
        again["id"] == doc["id"]
        and again["version_id"] != doc["version_id"]
        and not again["duplicate"]
    )
    env.drain(env.worker(chunk_size=500))
    d = c2.get(f"/v1/documents/{doc['id']}", headers=AUTH).json()
    assert [v["version_no"] for v in d["versions"]] == [2, 1]
    assert d["current_version_id"] == again["version_id"]
    cur = c2.get(f"/v1/documents/{doc['id']}/chunks?limit=200", headers=AUTH).json()["items"]
    assert len(cur) == d["versions"][0]["chunk_count"] != d["versions"][1]["chunk_count"]


# ---- T006: crash mid-embedding resumes from checkpoint ---------------------------------------
class Crash(BaseException):
    """Simulates SIGKILL: not caught by `except Exception`, leaves the lease held."""


def test_worker_crash_mid_embedding_resumes(env):
    c = env.client()
    doc = upload(c, "n.txt", LONG, "text/plain").json()

    def die_on_third(n):
        if n == 3:
            raise Crash

    env.embedder.hook = die_on_third
    with pytest.raises(Crash):
        env.worker().run_once()
    total = q(env, "SELECT count(*) FROM document_chunks")[0][0]
    done = q(env, "SELECT count(*) FROM document_chunks WHERE embedding IS NOT NULL")[0][0]
    assert (
        0 < done < total and status_of(c, doc["id"])["status"] == "EMBEDDING"
    )  # no partial COMPLETED
    assert q(env, "SELECT count(*) FROM documents WHERE current_version_id IS NOT NULL")[0][0] == 0

    assert env.drain(env.worker()) == 0  # lease still held by the dead worker
    q_exec = psycopg.connect(env.url, autocommit=True)
    q_exec.execute("UPDATE ingestion_jobs SET lease_expires_at = now() - interval '1 second'")
    env.embedder.hook, env.embedder.calls = None, []
    assert env.drain(env.worker()) == 1
    assert sum(env.embedder.calls) == total - done  # only the remainder was re-embedded
    v = status_of(c, doc["id"])
    assert v["status"] == "COMPLETED" and v["attempt_count"] == 2
    assert q(env, "SELECT count(*) FROM document_chunks")[0][0] == total  # no duplicates


def test_lease_fencing_blocks_stale_worker(env):
    c = env.client()
    upload(c, "n.txt", LONG, "text/plain")
    s = env.settings()
    pool = env.worker().pool
    with pool.connection() as conn:
        a = jobs.claim_job(conn, "A", 60, 5)
        conn.execute("UPDATE ingestion_jobs SET lease_expires_at = now() - interval '1 s'")
        conn.commit()
        b = jobs.claim_job(conn, "B", 60, 5)
        assert a and b and b.attempt_count == 2
        with pytest.raises(LeaseLost):
            jobs.set_stage(conn, a, "EXTRACTING", s.lease_seconds)
        jobs.set_stage(conn, b, "EXTRACTING", s.lease_seconds)


# ---- T007: provider failures -----------------------------------------------------------------
def test_transient_provider_errors_retry_then_complete(env):
    c = env.client()
    doc = upload(c, "n.txt", LONG, "text/plain").json()

    def flaky(n):
        if n <= 2:
            raise TransientIngestError("embedding provider returned 429")

    env.embedder.hook = flaky
    w = env.worker()
    env.drain(w)
    v = status_of(c, doc["id"])
    assert v["status"] == "COMPLETED" and v["attempt_count"] == 3
    assert [e["status"] for e in v["events"]].count("RETRY") == 2


def test_retries_exhausted_dead_letters_with_no_partial_state(env):
    c = env.client()
    doc = upload(c, "n.txt", LONG, "text/plain").json()
    env.embedder.hook = lambda n: (_ for _ in ()).throw(TransientIngestError("timeout"))
    env.drain(env.worker())
    v = status_of(c, doc["id"])
    assert v["status"] == "FAILED" and v["error_code"] == "retries_exhausted" and v["dead_letter"]
    assert q(env, "SELECT count(*) FROM documents WHERE current_version_id IS NOT NULL")[0][0] == 0
    assert env.drain(env.worker()) == 0


def test_daily_budget_enforced(env):
    c = env.client()
    doc = upload(c, "n.txt", LONG, "text/plain").json()
    env.drain(env.worker(embedding_daily_token_budget=10))
    v = status_of(c, doc["id"])
    assert v["status"] == "FAILED" and v["error_code"] == "embedding_budget_exceeded"
    assert env.embedder.calls == []


def test_tampered_original_fails_integrity(env):
    c = env.client()
    doc = upload(c, "n.txt", LONG, "text/plain").json()
    key = next(iter(env.store.objects))
    env.store.objects[key] = b"tampered"
    env.drain(env.worker())
    assert status_of(c, doc["id"])["error_code"] == "integrity_mismatch"


# ---- T008: workspace isolation ---------------------------------------------------------------
def test_cross_workspace_access_denied(env):
    c = env.client()
    mine = upload(c, "mine.txt", LONG, "text/plain").json()
    env.drain(env.worker())
    with psycopg.connect(env.url, autocommit=True) as conn:
        ws2 = conn.execute(
            "INSERT INTO workspaces (slug, owner_actor_id) VALUES ('other','x') RETURNING id"
        ).fetchone()[0]
        doc2 = conn.execute(
            "INSERT INTO documents (workspace_id, display_name, media_type, created_by) "
            "VALUES (%s,'secret.txt','text/plain','x') RETURNING id",
            (ws2,),
        ).fetchone()[0]
    for path in (f"/v1/documents/{doc2}", f"/v1/documents/{doc2}/chunks"):
        assert c.get(path, headers=AUTH).status_code == 404
    ids = [d["id"] for d in c.get("/v1/documents", headers=AUTH).json()["items"]]
    assert ids == [mine["id"]]
    # a chunk-listing request using my doc id but another version id yields nothing
    other_ver = q(env, "SELECT gen_random_uuid()")[0][0]
    assert (
        c.get(f"/v1/documents/{mine['id']}/chunks?version_id={other_ver}", headers=AUTH).json()[
            "items"
        ]
        == []
    )


def test_list_pagination(env):
    c = env.client()
    ids = [
        upload(c, f"d{i}.txt", f"document number {i} body".encode(), "text/plain").json()["id"]
        for i in range(5)
    ]
    seen, cur = [], None
    while True:
        r = c.get(
            "/v1/documents", params={"limit": 2, **({"cursor": cur} if cur else {})}, headers=AUTH
        ).json()
        seen += [d["id"] for d in r["items"]]
        cur = r["next_cursor"]
        if not cur:
            break
    assert seen == ids[::-1]
    assert c.get("/v1/documents?limit=0", headers=AUTH).status_code == 422
    assert c.get("/v1/documents?cursor=junk", headers=AUTH).json()["code"] == "invalid_cursor"
