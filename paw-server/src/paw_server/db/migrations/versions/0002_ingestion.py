"""ingestion schema: documents, versions, jobs, events, chunks, usage

Revision ID: 0002
Revises: 0001
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

STATUSES = (
    "RECEIVED",
    "STORED",
    "EXTRACTING",
    "CHUNKING",
    "EMBEDDING",
    "INDEXING",
    "COMPLETED",
    "FAILED",
    "UNSUPPORTED",
)


def upgrade() -> None:
    status_list = ", ".join(f"'{s}'" for s in STATUSES)
    op.execute(
        """
        CREATE TABLE documents (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES workspaces(id),
            display_name text NOT NULL,
            media_type text NOT NULL,
            current_version_id uuid,
            created_by text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            deleted_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX documents_ws_created ON documents (workspace_id, created_at DESC, id DESC)"
    )
    op.execute(
        f"""
        CREATE TABLE document_versions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            document_id uuid NOT NULL REFERENCES documents(id),
            workspace_id uuid NOT NULL REFERENCES workspaces(id),
            version_no int NOT NULL,
            sha256 text NOT NULL,
            object_key text NOT NULL,
            size_bytes bigint NOT NULL,
            original_filename text NOT NULL,
            parser_version text,
            status text NOT NULL DEFAULT 'STORED' CHECK (status IN ({status_list})),
            ingest_config_hash text NOT NULL,
            embedding_model text,
            idempotency_key text,
            error_code text,
            error_message text,
            created_at timestamptz NOT NULL DEFAULT now(),
            status_updated_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (document_id, version_no),
            UNIQUE (workspace_id, sha256, ingest_config_hash)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX document_versions_idem ON document_versions (workspace_id, idempotency_key) "
        "WHERE idempotency_key IS NOT NULL"
    )
    op.execute("CREATE INDEX document_versions_sha ON document_versions (workspace_id, sha256)")
    op.execute(
        "ALTER TABLE documents ADD CONSTRAINT documents_current_version_fk "
        "FOREIGN KEY (current_version_id) REFERENCES document_versions(id)"
    )
    op.execute(
        """
        CREATE TABLE ingestion_jobs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            version_id uuid NOT NULL UNIQUE REFERENCES document_versions(id),
            workspace_id uuid NOT NULL REFERENCES workspaces(id),
            stage text NOT NULL DEFAULT 'STORED',
            lease_owner text,
            lease_expires_at timestamptz,
            attempt_count int NOT NULL DEFAULT 0,
            checkpoint jsonb NOT NULL DEFAULT '{}',
            next_retry_at timestamptz NOT NULL DEFAULT now(),
            last_error text,
            dead_letter boolean NOT NULL DEFAULT false,
            created_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz,
            finished_at timestamptz
        )
        """
    )
    op.execute(
        "CREATE INDEX ingestion_jobs_runnable ON ingestion_jobs (next_retry_at) WHERE finished_at IS NULL"
    )
    op.execute(
        """
        CREATE TABLE ingestion_events (
            id bigserial PRIMARY KEY,
            version_id uuid NOT NULL REFERENCES document_versions(id),
            status text NOT NULL,
            detail text,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX ingestion_events_version ON ingestion_events (version_id, id)")
    op.execute(
        """
        CREATE TABLE document_chunks (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            version_id uuid NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
            workspace_id uuid NOT NULL REFERENCES workspaces(id),
            ordinal int NOT NULL,
            text text NOT NULL,
            page_number int,
            section text,
            source_start int,
            source_end int,
            metadata jsonb NOT NULL DEFAULT '{}',
            embedding vector(1536),
            embedding_model text,
            tsv tsvector GENERATED ALWAYS AS (to_tsvector('english', text)) STORED,
            UNIQUE (version_id, ordinal)
        )
        """
    )
    op.execute("CREATE INDEX document_chunks_tsv ON document_chunks USING gin (tsv)")
    op.execute(
        "CREATE INDEX document_chunks_embedding ON document_chunks "
        "USING hnsw (embedding vector_cosine_ops)"
    )
    op.execute("CREATE INDEX document_chunks_ws ON document_chunks (workspace_id)")
    op.execute(
        """
        CREATE TABLE usage_daily (
            workspace_id uuid NOT NULL REFERENCES workspaces(id),
            day date NOT NULL,
            embedding_tokens bigint NOT NULL DEFAULT 0,
            PRIMARY KEY (workspace_id, day)
        )
        """
    )


def downgrade() -> None:
    for t in ("usage_daily", "document_chunks", "ingestion_events", "ingestion_jobs"):
        op.execute(f"DROP TABLE {t}")
    op.execute("ALTER TABLE documents DROP CONSTRAINT documents_current_version_fk")
    op.execute("DROP TABLE document_versions")
    op.execute("DROP TABLE documents")
