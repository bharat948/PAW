"""retrieval telemetry: hashed queries and result ids only (no raw query text)

Revision ID: 0003
Revises: 0002
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE retrieval_events (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            workspace_id uuid NOT NULL REFERENCES workspaces(id),
            actor_id text NOT NULL,
            query_hash text NOT NULL,
            query_chars int NOT NULL,
            top_k int NOT NULL,
            fusion_version text NOT NULL,
            methods text[] NOT NULL,
            degraded text,
            latency_ms real NOT NULL,
            result_ids uuid[] NOT NULL DEFAULT '{}',
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX retrieval_events_ws_created ON retrieval_events (workspace_id, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE retrieval_events")
