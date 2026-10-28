"""Hybrid retrieval: PostgreSQL full-text + pgvector, fused with weighted reciprocal-rank fusion.

Every query is filtered to the caller's workspace and to ACTIVE chunks (the current version of each
non-deleted document) inside SQL, before any content leaves the database. Scores are rank-derived
and are NOT calibrated probabilities.
"""

import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from psycopg_pool import ConnectionPool

from paw_server.core.config import Settings
from paw_server.modules.ingestion.embedding import Embedder, estimate_tokens
from paw_server.modules.ingestion.errors import IngestError
from paw_server.modules.ingestion.jobs import Conn
from paw_server.modules.ingestion.models import Actor
from paw_server.modules.ingestion.pipeline import vec_literal

log = logging.getLogger("paw.retrieval")

# Fusion configs are versioned so results stay reproducible and evaluable when weights change.
FUSION_VERSIONS: dict[str, dict[str, float]] = {
    "rrf-1": {"keyword": 1.0, "vector": 1.0},
    "rrf-keyword-heavy-1": {"keyword": 1.5, "vector": 1.0},
}
DEFAULT_FUSION = "rrf-1"

_ACTIVE = """
    FROM document_chunks c
    JOIN documents d ON d.current_version_id = c.version_id
    WHERE d.workspace_id = %(ws)s AND c.workspace_id = %(ws)s AND d.deleted_at IS NULL
      AND (%(docs)s::uuid[] IS NULL OR d.id = ANY(%(docs)s))
"""


@dataclass
class Hit:
    chunk_id: UUID
    keyword_rank: int | None = None
    vector_rank: int | None = None
    keyword_score: float | None = None
    vector_distance: float | None = None
    fused: float = 0.0
    row: dict[str, Any] = field(default_factory=dict)

    @property
    def method(self) -> str:
        if self.keyword_rank and self.vector_rank:
            return "hybrid"
        return "keyword" if self.keyword_rank else "vector"


@dataclass
class SearchOutcome:
    hits: list[Hit]
    fusion_version: str
    methods: list[str]
    degraded: str | None
    latency_ms: float
    candidates: dict[str, int]
    event_id: UUID | None = None


def fuse(keyword: list[Hit], vector: list[Hit], weights: dict[str, float], k: int) -> list[Hit]:
    """Weighted RRF with deterministic tie-breaking (fused desc, best rank asc, chunk id asc)."""
    merged: dict[UUID, Hit] = {}
    for h in keyword:
        m = merged.setdefault(h.chunk_id, Hit(h.chunk_id, row=h.row))
        m.keyword_rank, m.keyword_score = h.keyword_rank, h.keyword_score
    for h in vector:
        m = merged.setdefault(h.chunk_id, Hit(h.chunk_id, row=h.row))
        m.vector_rank, m.vector_distance = h.vector_rank, h.vector_distance
    for m in merged.values():
        if m.keyword_rank:
            m.fused += weights["keyword"] / (k + m.keyword_rank)
        if m.vector_rank:
            m.fused += weights["vector"] / (k + m.vector_rank)
    return sorted(
        merged.values(),
        key=lambda m: (
            -round(m.fused, 12),
            min(r for r in (m.keyword_rank, m.vector_rank) if r),
            str(m.chunk_id),
        ),
    )


_COLS = """c.id, c.version_id, c.ordinal, c.text, c.page_number, c.section, c.source_start,
           c.source_end, c.metadata, d.id AS document_id, d.display_name, d.media_type"""


def _keyword(conn: Conn, ws: UUID, docs: list[UUID] | None, q: str, limit: int) -> list[Hit]:
    """Strict AND match first (precise); if nothing matches, relax to OR-of-terms ranked by ts_rank_cd."""
    tsq = {
        "strict": "websearch_to_tsquery('english', %(q)s)",
        "relaxed": "replace(plainto_tsquery('english', %(q)s)::text, '&', '|')::tsquery",
    }
    for mode in ("strict", "relaxed"):
        rows = conn.execute(
            f"""SELECT {_COLS}, ts_rank_cd(c.tsv, {tsq[mode]}) AS score
                {_ACTIVE} AND c.tsv @@ {tsq[mode]}
                ORDER BY score DESC, c.id LIMIT %(lim)s""",
            {"ws": ws, "docs": docs, "q": q, "lim": limit},
        ).fetchall()
        conn.commit()
        if rows:
            break
    return [
        Hit(r["id"], keyword_rank=i, keyword_score=float(r["score"]), row=r)
        for i, r in enumerate(rows, 1)
    ]


def _vector(
    conn: Conn,
    ws: UUID,
    docs: list[UUID] | None,
    vec: list[float],
    model: str,
    limit: int,
    max_dist: float,
) -> list[Hit]:
    conn.execute("SET LOCAL hnsw.iterative_scan = relaxed_order")
    conn.execute("SET LOCAL hnsw.ef_search = 100")
    rows = conn.execute(
        f"""SELECT {_COLS}, c.embedding <=> %(v)s::vector AS dist
            {_ACTIVE} AND c.embedding IS NOT NULL AND c.embedding_model = %(m)s
              AND c.embedding <=> %(v)s::vector <= %(maxd)s
            ORDER BY c.embedding <=> %(v)s::vector, c.id LIMIT %(lim)s""",
        {"ws": ws, "docs": docs, "v": vec_literal(vec), "m": model, "lim": limit, "maxd": max_dist},
    ).fetchall()
    conn.commit()
    return [
        Hit(r["id"], vector_rank=i, vector_distance=float(r["dist"]), row=r)
        for i, r in enumerate(rows, 1)
    ]


def search(
    pool: ConnectionPool[Conn],
    embedder: Embedder,
    s: Settings,
    actor: Actor,
    *,
    query: str,
    top_k: int,
    document_ids: list[UUID] | None,
    fusion_version: str,
) -> SearchOutcome:
    t0 = time.perf_counter()
    weights = FUSION_VERSIONS[fusion_version]
    degraded: str | None = None
    vec: list[float] | None = None

    with pool.connection() as conn:
        # budget gate for query embeddings (shares the per-workspace daily cap)
        used = conn.execute(
            "SELECT COALESCE(SUM(embedding_tokens), 0) AS t FROM usage_daily "
            "WHERE workspace_id = %s AND day = %s",
            (actor.workspace_id, date.today()),
        ).fetchone()["t"]  # type: ignore[index]
        conn.commit()
        tokens = estimate_tokens(query)
        if used + tokens > s.embedding_daily_token_budget:
            degraded = "embedding_budget_exceeded"
        else:
            try:
                vec = embedder.embed([query])[0]
            except IngestError as exc:
                log.warning("query embedding unavailable", extra={"error": type(exc).__name__})
                degraded = "embedding_unavailable"
            except Exception as exc:  # never fail the search because the provider misbehaves
                log.warning("query embedding error", extra={"error": type(exc).__name__})
                degraded = "embedding_unavailable"

        kw = _keyword(conn, actor.workspace_id, document_ids, query, s.search_candidates)
        vc: list[Hit] = []
        if vec is not None:
            vc = _vector(
                conn,
                actor.workspace_id,
                document_ids,
                vec,
                embedder.model,
                s.search_candidates,
                s.vector_max_distance,
            )
            conn.execute(
                """INSERT INTO usage_daily (workspace_id, day, embedding_tokens) VALUES (%s, %s, %s)
                   ON CONFLICT (workspace_id, day) DO UPDATE
                   SET embedding_tokens = usage_daily.embedding_tokens + EXCLUDED.embedding_tokens""",
                (actor.workspace_id, date.today(), tokens),
            )
            conn.commit()

        hits = fuse(kw, vc, weights, s.rrf_k)[:top_k]
        methods = ["keyword"] + (["vector"] if vec is not None else [])
        latency = (time.perf_counter() - t0) * 1000
        event_id = conn.execute(
            """INSERT INTO retrieval_events (workspace_id, actor_id, query_hash, query_chars, top_k,
                   fusion_version, methods, degraded, latency_ms, result_ids)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
            (
                actor.workspace_id,
                actor.id,
                hashlib.sha256(query.lower().encode()).hexdigest(),
                len(query),
                top_k,
                fusion_version,
                methods,
                degraded,
                latency,
                [h.chunk_id for h in hits],
            ),
        ).fetchone()["id"]  # type: ignore[index]
        conn.commit()

    log.info(
        "search",
        extra={
            "latency_ms": round(latency, 1),
            "results": len(hits),
            "methods": methods,
            "degraded": degraded,
        },
    )
    return SearchOutcome(
        hits,
        fusion_version,
        methods,
        degraded,
        round(latency, 1),
        {"keyword": len(kw), "vector": len(vc)},
        event_id,
    )
