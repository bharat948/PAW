"""baseline: pgvector extension, workspaces, default owner workspace

Revision ID: 0001
Revises:
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        """
        CREATE TABLE workspaces (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            slug text NOT NULL UNIQUE,
            owner_actor_id text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            status text NOT NULL DEFAULT 'active'
        )
        """
    )
    op.execute("INSERT INTO workspaces (slug, owner_actor_id) VALUES ('default', 'owner')")


def downgrade() -> None:
    op.execute("DROP TABLE workspaces")
