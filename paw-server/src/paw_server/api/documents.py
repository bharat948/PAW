import base64
import json
from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, File, Header, Query, Request, UploadFile
from pydantic import BaseModel

from paw_server.api.deps import CurrentActor
from paw_server.core.errors import ApiError
from paw_server.modules.ingestion import service

router = APIRouter(prefix="/v1/documents", tags=["documents"])

STATUSES = {
    "RECEIVED",
    "STORED",
    "EXTRACTING",
    "CHUNKING",
    "EMBEDDING",
    "INDEXING",
    "COMPLETED",
    "FAILED",
    "UNSUPPORTED",
}


class UploadResponse(BaseModel):
    id: UUID
    version_id: UUID
    status_url: str
    duplicate: bool


class VersionSummary(BaseModel):
    id: UUID
    version_no: int
    status: str
    error_code: str | None = None
    error_message: str | None = None
    status_updated_at: datetime


class DocumentSummary(BaseModel):
    id: UUID
    display_name: str
    media_type: str
    created_at: datetime
    current_version_id: UUID | None
    latest_version: VersionSummary


class DocumentList(BaseModel):
    items: list[DocumentSummary]
    next_cursor: str | None


class EventOut(BaseModel):
    status: str
    detail: str | None
    created_at: datetime


class VersionDetail(VersionSummary):
    sha256: str
    size_bytes: int
    parser_version: str | None
    embedding_model: str | None
    stage: str | None
    attempt_count: int | None
    dead_letter: bool | None
    chunk_count: int
    events: list[EventOut]


class DocumentDetail(BaseModel):
    id: UUID
    display_name: str
    media_type: str
    created_at: datetime
    current_version_id: UUID | None
    versions: list[VersionDetail]


class ChunkOut(BaseModel):
    id: UUID
    ordinal: int
    text: str
    page_number: int | None
    section: str | None
    source_start: int | None
    source_end: int | None
    metadata: dict[str, Any]


class ChunkList(BaseModel):
    items: list[ChunkOut]
    next_cursor: int | None


@router.post("", status_code=202, response_model=UploadResponse)
def upload_document(
    request: Request,
    actor: CurrentActor,
    file: Annotated[UploadFile, File()],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> UploadResponse:
    s = request.app.state.settings
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > s.max_upload_bytes + 64 * 1024:
        raise ApiError(413)  # reject early; the proxy should also cap (001D)
    result = service.upload(
        request.app.state.pool,
        request.app.state.store,
        s,
        actor,
        filename=file.filename,
        declared_type=file.content_type,
        body=file.file,
        idempotency_key=idempotency_key,
    )
    return UploadResponse(
        id=result.document_id,
        version_id=result.version_id,
        status_url=f"/v1/documents/{result.document_id}",
        duplicate=result.duplicate,
    )


def _enc(ts: datetime, id_: UUID) -> str:
    return base64.urlsafe_b64encode(json.dumps([ts.isoformat(), str(id_)]).encode()).decode()


def _dec(cursor: str) -> tuple[datetime, UUID]:
    try:
        ts, id_ = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        return datetime.fromisoformat(ts), UUID(id_)
    except Exception as exc:
        raise ApiError(422, "invalid_cursor", "The cursor is invalid.") from exc


@router.get("", response_model=DocumentList)
def list_documents(
    request: Request,
    actor: CurrentActor,
    status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    cursor: Annotated[str | None, Query()] = None,
) -> DocumentList:
    if status is not None and status not in STATUSES:
        raise ApiError(422, "invalid_status", "Unknown status filter.")
    after = _dec(cursor) if cursor else None
    with request.app.state.pool.connection() as conn:
        rows = conn.execute(
            """SELECT d.id, d.display_name, d.media_type, d.created_at, d.current_version_id,
                      v.id AS vid, v.version_no, v.status, v.error_code, v.error_message,
                      v.status_updated_at
               FROM documents d
               JOIN LATERAL (SELECT * FROM document_versions WHERE document_id = d.id
                             ORDER BY version_no DESC LIMIT 1) v ON true
               WHERE d.workspace_id = %(ws)s AND d.deleted_at IS NULL
                 AND (%(st)s::text IS NULL OR v.status = %(st)s)
                 AND (%(ts)s::timestamptz IS NULL OR (d.created_at, d.id) < (%(ts)s, %(id)s::uuid))
               ORDER BY d.created_at DESC, d.id DESC LIMIT %(lim)s""",
            {
                "ws": actor.workspace_id,
                "st": status,
                "lim": limit + 1,
                "ts": after[0] if after else None,
                "id": after[1] if after else None,
            },
        ).fetchall()
        conn.commit()
    page = rows[:limit]
    items = [
        DocumentSummary(
            id=r["id"],
            display_name=r["display_name"],
            media_type=r["media_type"],
            created_at=r["created_at"],
            current_version_id=r["current_version_id"],
            latest_version=VersionSummary(
                id=r["vid"],
                version_no=r["version_no"],
                status=r["status"],
                error_code=r["error_code"],
                error_message=r["error_message"],
                status_updated_at=r["status_updated_at"],
            ),
        )
        for r in page
    ]
    nxt = _enc(page[-1]["created_at"], page[-1]["id"]) if len(rows) > limit else None
    return DocumentList(items=items, next_cursor=nxt)


def _owned_document(conn: Any, actor: Any, doc_id: UUID) -> dict[str, Any]:
    doc = conn.execute(
        "SELECT id, display_name, media_type, created_at, current_version_id FROM documents "
        "WHERE id = %s AND workspace_id = %s AND deleted_at IS NULL",
        (doc_id, actor.workspace_id),
    ).fetchone()
    if doc is None:  # same answer for "missing" and "someone else's": no ID enumeration
        raise ApiError(404)
    return doc  # type: ignore[no-any-return]


@router.get("/{doc_id}", response_model=DocumentDetail)
def get_document(request: Request, actor: CurrentActor, doc_id: UUID) -> DocumentDetail:
    with request.app.state.pool.connection() as conn:
        doc = _owned_document(conn, actor, doc_id)
        vers = conn.execute(
            """SELECT v.*, j.stage, j.attempt_count, j.dead_letter,
                      (SELECT count(*) FROM document_chunks c WHERE c.version_id = v.id) AS chunk_count
               FROM document_versions v LEFT JOIN ingestion_jobs j ON j.version_id = v.id
               WHERE v.document_id = %s AND v.workspace_id = %s ORDER BY v.version_no DESC""",
            (doc_id, actor.workspace_id),
        ).fetchall()
        events = conn.execute(
            "SELECT version_id, status, detail, created_at FROM ingestion_events "
            "WHERE version_id = ANY(%s) ORDER BY id",
            ([v["id"] for v in vers],),
        ).fetchall()
        conn.commit()
    by_ver: dict[UUID, list[EventOut]] = {}
    for e in events:
        by_ver.setdefault(e["version_id"], []).append(
            EventOut(status=e["status"], detail=e["detail"], created_at=e["created_at"])
        )
    return DocumentDetail(
        **doc,
        versions=[VersionDetail(**v, events=by_ver.get(v["id"], [])) for v in vers],
    )


@router.get("/{doc_id}/chunks", response_model=ChunkList)
def list_chunks(
    request: Request,
    actor: CurrentActor,
    doc_id: UUID,
    version_id: UUID | None = None,
    after: Annotated[int, Query(ge=-1)] = -1,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> ChunkList:
    """Inspect extracted passages and their provenance (defaults to the current version)."""
    with request.app.state.pool.connection() as conn:
        doc = _owned_document(conn, actor, doc_id)
        vid = version_id or doc["current_version_id"]
        if vid is None:
            return ChunkList(items=[], next_cursor=None)
        rows = conn.execute(
            """SELECT c.id, c.ordinal, c.text, c.page_number, c.section, c.source_start,
                      c.source_end, c.metadata
               FROM document_chunks c JOIN document_versions v ON v.id = c.version_id
               WHERE c.version_id = %s AND v.document_id = %s AND c.workspace_id = %s
                 AND c.ordinal > %s ORDER BY c.ordinal LIMIT %s""",
            (vid, doc_id, actor.workspace_id, after, limit + 1),
        ).fetchall()
        conn.commit()
    page = rows[:limit]
    return ChunkList(
        items=[ChunkOut(**r) for r in page],
        next_cursor=page[-1]["ordinal"] if len(rows) > limit else None,
    )
