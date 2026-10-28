import os
import time

import psycopg
import pytest

from paw_server.modules.ingestion.errors import TransientIngestError
from paw_server.modules.retrieval.service import Hit, fuse
from tests import eval_corpus as ev
from tests.conftest import AUTH, upload

pytestmark = pytest.mark.integration
SEARCH = "/v1/retrieval/search"


def q(env, sql, *a):
    with psycopg.connect(env.url) as c:
        return c.execute(sql, a).fetchall()


@pytest.fixture
def corpus(env):
    """Uploads and fully ingests the evaluation corpus; returns (client, {name: doc_id})."""
    c = env.client()
    ids = {n: upload(c, n, d).json()["id"] for n, d in ev.DOCS.items()}
    env.drain(env.worker())
    return c, ids


def search(c, query, **kw):
    return c.post(SEARCH, json={"query": query, **kw}, headers=AUTH)


# ---- T009 / 001C-R06: expected sources appear under pinned settings --------------------------
@pytest.mark.parametrize("kind,query,expected", ev.QUERIES)
def test_eval_corpus(corpus, kind, query, expected):
    c, ids = corpus
    r = search(c, query, top_k=5)
    assert r.status_code == 200, r.text
    res = r.json()["results"]
    if expected is None:
        assert res == []  # predictable zero-result behavior
        return
    assert res and res[0]["document_id"] == ids[expected], [x["document_name"] for x in res]
    assert expected in [x["document_name"] for x in res][:3]


def test_result_contract_and_resolvable_sources(corpus, env):
    c, ids = corpus
    body = search(c, "quarterly revenue Helsinki").json()
    top = body["results"][0]
    assert set(top) >= {
        "document_id",
        "document_version_id",
        "chunk_id",
        "excerpt",
        "retrieval_method",
        "source",
        "scores",
        "document_name",
        "media_type",
    }
    assert top["source"]["page_number"] == 1 and top["retrieval_method"] in (
        "hybrid",
        "keyword",
        "vector",
    )
    assert (
        body["meta"]["methods_used"] == ["keyword", "vector"] and body["meta"]["degraded"] is None
    )
    assert "probabilit" in body["meta"]["score_note"]
    # reference resolves to the same version/document, and the original is downloadable
    chunk = c.get(top["source"]["url"], headers=AUTH).json()
    assert (chunk["document_id"], chunk["document_version_id"], chunk["chunk_id"]) == (
        top["document_id"],
        top["document_version_id"],
        top["chunk_id"],
    )
    assert chunk["is_current_version"] and "Helsinki" in chunk["text"]
    dl = c.get(chunk["original_url"], headers=AUTH)
    assert (
        dl.content == ev.DOCS["finance.pdf"] and "attachment" in dl.headers["content-disposition"]
    )
    assert dl.headers["x-content-type-options"] == "nosniff"


def test_tabular_result_carries_rows(corpus):
    c, _ = corpus
    top = search(c, "Linus kernel").json()["results"][0]
    assert top["source"]["source_unit"] == "rows" and top["source"]["source_start"] == 1


def test_deterministic_ordering(corpus):
    c, _ = corpus
    a = [x["chunk_id"] for x in search(c, "observatory telemetry pulsar").json()["results"]]
    b = [x["chunk_id"] for x in search(c, "observatory telemetry pulsar").json()["results"]]
    assert a == b and len(a) >= 1


# ---- 001C-R03: provider unavailable => explicit lexical fallback -----------------------------
def test_embedding_outage_degrades_to_keyword(corpus, env):
    c, ids = corpus

    def down(_):
        raise TransientIngestError("provider down")

    env.embedder.hook = down
    body = search(c, "sourdough starter").json()
    assert body["meta"]["degraded"] == "embedding_unavailable" and body["meta"]["methods_used"] == [
        "keyword"
    ]
    assert body["results"][0]["document_id"] == ids["cooking.txt"]
    assert body["results"][0]["retrieval_method"] == "keyword"
    # a purely semantic query returns nothing (honestly) rather than failing
    assert search(c, "zzzqqq").json()["results"] == []


def test_query_budget_degrades(corpus, env):
    c = env.client(embedding_daily_token_budget=1)
    body = search(c, "sourdough starter").json()
    assert body["meta"]["degraded"] == "embedding_budget_exceeded" and body["results"]


# ---- 001C-R01 / T008: scoping in SQL ---------------------------------------------------------
def test_cross_workspace_content_never_returned(corpus, env):
    c, ids = corpus
    with psycopg.connect(env.url, autocommit=True) as conn:
        ws2 = conn.execute(
            "INSERT INTO workspaces (slug, owner_actor_id) VALUES ('other','x') RETURNING id"
        ).fetchone()[0]
        d2 = conn.execute(
            "INSERT INTO documents (workspace_id, display_name, media_type, created_by) "
            "VALUES (%s,'secret.txt','text/plain','x') RETURNING id",
            (ws2,),
        ).fetchone()[0]
        v2 = conn.execute(
            "INSERT INTO document_versions (document_id, workspace_id, version_no, sha256, object_key, "
            "size_bytes, original_filename, status, ingest_config_hash) VALUES "
            "(%s,%s,1,'x','k',1,'secret.txt','COMPLETED','c') RETURNING id",
            (d2, ws2),
        ).fetchone()[0]
        vec = "[" + ",".join(["0.1"] * 1536) + "]"
        c2 = conn.execute(
            "INSERT INTO document_chunks (version_id, workspace_id, ordinal, text, embedding, embedding_model) "
            "VALUES (%s,%s,0,'secret sourdough starter formula',%s::vector,'fake-hash-v1') RETURNING id",
            (v2, ws2, vec),
        ).fetchone()[0]
        conn.execute("UPDATE documents SET current_version_id=%s WHERE id=%s", (v2, d2))
    res = search(c, "secret sourdough starter formula").json()["results"]
    assert str(c2) not in [x["chunk_id"] for x in res] and all(
        x["document_name"] != "secret.txt" for x in res
    )
    # guessing the foreign chunk/document id, or filtering by it, reveals nothing
    assert c.get(f"/v1/chunks/{c2}", headers=AUTH).status_code == 404
    assert c.get(f"/v1/documents/{d2}/original", headers=AUTH).status_code == 404
    assert search(c, "secret sourdough", document_ids=[str(d2)]).json()["results"] == []


def test_document_filter(corpus):
    c, ids = corpus
    r = search(c, "starter revenue observatory", document_ids=[ids["cooking.txt"]]).json()[
        "results"
    ]
    assert r and {x["document_id"] for x in r} == {ids["cooking.txt"]}


def test_only_active_complete_versions_searchable(env):
    c = env.client()
    doc = upload(c, "a.txt", ("uniqueterm alpha. " * 50).encode(), "text/plain").json()
    # before ingestion completes nothing is searchable
    assert search(c, "uniqueterm").json()["results"] == []
    env.drain(env.worker())
    assert search(c, "uniqueterm").json()["results"]
    # reprocess same bytes under a new chunk config => only the current version's chunks are active
    c2 = env.client(chunk_size=500)
    again = upload(c2, "a.txt", ("uniqueterm alpha. " * 50).encode(), "text/plain").json()
    env.drain(env.worker(chunk_size=500))
    res = search(c2, "uniqueterm", top_k=50).json()["results"]
    assert res and {x["document_version_id"] for x in res} == {again["version_id"]}
    assert doc["version_id"] != again["version_id"]


def test_duplicate_upload_yields_single_hit(env):
    c = env.client()
    a = upload(c, "one.txt", b"the zebra crossing is striped", "text/plain").json()
    b = upload(c, "copy-renamed.txt", b"the zebra crossing is striped", "text/plain").json()
    assert a["id"] == b["id"]
    env.drain(env.worker())
    assert len(search(c, "zebra crossing").json()["results"]) == 1


# ---- 001C-R05: input bounds, adversarial input, rate limiting --------------------------------
@pytest.mark.parametrize(
    "payload",
    [
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 1001},
        {"query": "ok", "top_k": 0},
        {"query": "ok", "top_k": 51},
        {"query": "ok", "top_k": "many"},
        {"query": "ok", "fusion_version": "nope"},
        {"query": "ok", "document_ids": ["not-a-uuid"]},
        {},
        {"query": None},
    ],
)
def test_invalid_search_params_422(corpus, payload):
    c, _ = corpus
    r = c.post(SEARCH, json=payload, headers=AUTH)
    assert r.status_code == 422 and r.json()["code"] == "validation_error"


@pytest.mark.parametrize("query", ev.ADVERSARIAL)
def test_adversarial_queries_do_not_error(corpus, query):
    c, _ = corpus
    assert search(c, query).status_code == 200


def test_html_in_documents_returned_as_inert_text(corpus):
    c, ids = corpus
    top = search(c, "script alert widget").json()["results"][0]
    assert (
        top["document_id"] == ids["markup.txt"] and "<script>" in top["excerpt"]
    )  # raw text; UI must escape
    assert search(c, "x").headers["content-type"].startswith("application/json")


def test_search_requires_auth_and_is_rate_limited(env):
    c = env.client(search_rate_limit_per_min=3)
    assert c.post(SEARCH, json={"query": "a"}).status_code == 401
    codes = [search(c, "alpha").status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    r = search(c, "alpha")
    assert r.json()["code"] == "rate_limited" and int(r.headers["retry-after"]) >= 1


# ---- telemetry: hashed query only ------------------------------------------------------------
def test_retrieval_event_records_no_raw_query(corpus, env):
    c, _ = corpus
    secret = "confidential sourdough merger plan"
    body = search(c, secret, top_k=3).json()
    row = q(
        env,
        "SELECT query_hash, query_chars, top_k, methods, latency_ms, result_ids, workspace_id "
        "FROM retrieval_events WHERE id = %s",
        body["meta"]["query_id"],
    )[0]
    assert row[1] == len(secret) and row[2] == 3 and row[4] > 0
    assert secret not in " ".join(str(x) for x in row) and len(row[0]) == 64
    assert [str(x) for x in row[5]] == [r["chunk_id"] for r in body["results"]]


# ---- fusion unit behavior --------------------------------------------------------------------
def _h(i, **kw):
    import uuid

    return Hit(uuid.UUID(int=i), row={}, **kw)


def test_rrf_prefers_agreement_and_breaks_ties_deterministically():
    kw = [_h(1, keyword_rank=1), _h(2, keyword_rank=2)]
    vc = [_h(3, vector_rank=1), _h(2, vector_rank=2)]
    out = fuse(kw, vc, {"keyword": 1.0, "vector": 1.0}, 60)
    assert [h.chunk_id.int for h in out] == [2, 1, 3]  # 2 found by both; 1 vs 3 tie -> id order
    assert out[0].method == "hybrid" and out[1].method == "keyword" and out[2].method == "vector"
    heavy = fuse(kw, vc, {"keyword": 3.0, "vector": 1.0}, 60)
    assert [h.chunk_id.int for h in heavy][:2] == [2, 1]


# ---- live quality smoke (real provider; opt-in) ----------------------------------------------
@pytest.mark.live
@pytest.mark.skipif(
    not os.environ.get("PAW_EMBEDDING_API_KEY"), reason="needs PAW_EMBEDDING_API_KEY"
)
def test_live_semantic_synonym_smoke(env):
    from paw_server.modules.ingestion.embedding import OpenAICompatEmbedder

    emb = OpenAICompatEmbedder(
        "https://api.openai.com/v1",
        os.environ["PAW_EMBEDDING_API_KEY"],
        "text-embedding-3-small",
        1536,
        20,
    )
    env.embedder = emb  # type: ignore[assignment]
    env.overrides = {"embedding_model": emb.model}
    c = env.client()
    ids = {
        n: upload(c, n, d).json()["id"]
        for n, d in {
            "car.txt": b"How to repair an automobile engine and replace the brake pads.",
            "cake.txt": b"A recipe for chocolate cake with vanilla frosting and berries.",
        }.items()
    }
    env.drain(env.worker())
    time.sleep(0)
    top = search(c, "vehicle maintenance guide").json()["results"][0]
    assert top["document_id"] == ids["car.txt"]
