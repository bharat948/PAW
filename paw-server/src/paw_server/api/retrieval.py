import io
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from paw_server.api.deps import CurrentActor
from paw_server.core.errors import ApiError
from paw_server.modules.retrieval import service
from paw_server.modules.retrieval.service import DEFAULT_FUSION, FUSION_VERSIONS

router = APIRouter(tags=["retrieval"])


class SearchRequest(BaseModel):
    query: str = Field(max_length=1000)
    top_k: int = Field(default=10, ge=1, le=50)
    document_ids: list[UUID] | None = Field(default=None, max_length=50)
    fusion_version: str = DEFAULT_FUSION

    @field_validator("query")
    @classmethod
    def _nonempty(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("query must not be empty")
        return v

    @field_validator("fusion_version")
    @classmethod
    def _known(cls, v: str) -> str:
        if v not in FUSION_VERSIONS:
            raise ValueError(f"unknown fusion_version; choose from {sorted(FUSION_VERSIONS)}")
        return v


class SourceLocation(BaseModel):
    page_number: int | None
    section: str | None
    source_start: int | None
    source_end: int | None
    source_unit: str  # "chars" or "rows"
    ordinal: int
    url: str  # resolves to the exact chunk/version


class Scores(BaseModel):
    """Rank-derived values for ordering/debugging only; not probabilities and not comparable
    across queries or retrieval methods."""

    fused: float
    keyword_rank: int | None
    vector_rank: int | None
    keyword_score: float | None
    vector_distance: float | None


class SearchResult(BaseModel):
    document_id: UUID
    document_version_id: UUID
    chunk_id: UUID
    document_name: str
    media_type: str
    excerpt: str
    excerpt_truncated: bool
    retrieval_method: str
    source: SourceLocation
    scores: Scores


class SearchMeta(BaseModel):
    query_id: UUID | None
    fusion_version: str
    methods_used: list[str]
    degraded: str | None
    latency_ms: float
    candidates: dict[str, int]
    score_note: str = "Scores are rank-based, not calibrated probabilities."


class SearchResponse(BaseModel):
    results: list[SearchResult]
    meta: SearchMeta


@router.post("/v1/retrieval/search", response_model=SearchResponse)
def search(request: Request, actor: CurrentActor, body: SearchRequest) -> SearchResponse:
    s = request.app.state.settings
    request.app.state.search_limiter.check(actor.id)
    out = service.search(
        request.app.state.pool,
        request.app.state.embedder,
        s,
        actor,
        query=body.query,
        top_k=body.top_k,
        document_ids=body.document_ids,
        fusion_version=body.fusion_version,
    )
    results = []
    for h in out.hits:
        r = h.row
        text: str = r["text"]
        md = r["metadata"] or {}
        results.append(
            SearchResult(
                document_id=r["document_id"],
                document_version_id=r["version_id"],
                chunk_id=h.chunk_id,
                document_name=r["display_name"],
                media_type=r["media_type"],
                excerpt=text[: s.excerpt_chars],
                excerpt_truncated=len(text) > s.excerpt_chars,
                retrieval_method=h.method,
                source=SourceLocation(
                    page_number=r["page_number"],
                    section=r["section"],
                    source_start=r["source_start"],
                    source_end=r["source_end"],
                    source_unit=md.get("source_unit", "chars"),
                    ordinal=r["ordinal"],
                    url=f"/v1/chunks/{h.chunk_id}",
                ),
                scores=Scores(
                    fused=round(h.fused, 6),
                    keyword_rank=h.keyword_rank,
                    vector_rank=h.vector_rank,
                    keyword_score=h.keyword_score,
                    vector_distance=h.vector_distance,
                ),
            )
        )
    return SearchResponse(
        results=results,
        meta=SearchMeta(
            query_id=out.event_id,
            fusion_version=out.fusion_version,
            methods_used=out.methods,
            degraded=out.degraded,
            latency_ms=out.latency_ms,
            candidates=out.candidates,
        ),
    )


class ChunkDetail(BaseModel):
    chunk_id: UUID
    document_id: UUID
    document_version_id: UUID
    document_name: str
    media_type: str
    ordinal: int
    text: str
    page_number: int | None
    section: str | None
    source_start: int | None
    source_end: int | None
    metadata: dict[str, Any]
    is_current_version: bool
    original_url: str
    previous_chunk_id: UUID | None
    next_chunk_id: UUID | None
    created_at: datetime | None = None


@router.get("/v1/chunks/{chunk_id}", response_model=ChunkDetail)
def get_chunk(request: Request, actor: CurrentActor, chunk_id: UUID) -> ChunkDetail:
    """Resolve a search result's source reference. Same 404 for missing and foreign chunks."""
    with request.app.state.pool.connection() as conn:
        r = conn.execute(
            """SELECT c.id, c.version_id, c.ordinal, c.text, c.page_number, c.section, c.source_start,
                      c.source_end, c.metadata, d.id AS document_id, d.display_name, d.media_type,
                      (d.current_version_id = c.version_id) AS is_current,
                      (SELECT id FROM document_chunks WHERE version_id = c.version_id
                         AND ordinal = c.ordinal - 1) AS prev_id,
                      (SELECT id FROM document_chunks WHERE version_id = c.version_id
                         AND ordinal = c.ordinal + 1) AS next_id
               FROM document_chunks c JOIN document_versions v ON v.id = c.version_id
               JOIN documents d ON d.id = v.document_id
               WHERE c.id = %s AND c.workspace_id = %s AND d.workspace_id = %s
                 AND d.deleted_at IS NULL""",
            (chunk_id, actor.workspace_id, actor.workspace_id),
        ).fetchone()
        conn.commit()
    if r is None:
        raise ApiError(404)
    return ChunkDetail(
        chunk_id=r["id"],
        document_id=r["document_id"],
        document_version_id=r["version_id"],
        document_name=r["display_name"],
        media_type=r["media_type"],
        ordinal=r["ordinal"],
        text=r["text"],
        page_number=r["page_number"],
        section=r["section"],
        source_start=r["source_start"],
        source_end=r["source_end"],
        metadata=r["metadata"],
        is_current_version=r["is_current"],
        previous_chunk_id=r["prev_id"],
        next_chunk_id=r["next_id"],
        original_url=f"/v1/documents/{r['document_id']}/original?version_id={r['version_id']}",
    )


@router.get("/v1/documents/{doc_id}/original")
def download_original(
    request: Request,
    actor: CurrentActor,
    doc_id: UUID,
    version_id: Annotated[UUID | None, Query()] = None,
) -> StreamingResponse:
    """Download the exact original bytes (always as an attachment; never rendered inline)."""
    with request.app.state.pool.connection() as conn:
        r = conn.execute(
            """SELECT v.object_key, v.original_filename, d.media_type
               FROM documents d JOIN document_versions v ON v.document_id = d.id
               WHERE d.id = %s AND d.workspace_id = %s AND d.deleted_at IS NULL
                 AND v.id = COALESCE(%s, d.current_version_id, (
                       SELECT id FROM document_versions WHERE document_id = d.id
                       ORDER BY version_no DESC LIMIT 1))""",
            (doc_id, actor.workspace_id, version_id),
        ).fetchone()
        conn.commit()
    if r is None:
        raise ApiError(404)
    data = request.app.state.store.get(r["object_key"])
    safe = r["original_filename"].replace('"', "")
    return StreamingResponse(
        io.BytesIO(data),
        media_type=r["media_type"],
        headers={
            "Content-Disposition": f'attachment; filename="{safe}"',
            "X-Content-Type-Options": "nosniff",
        },
    )
