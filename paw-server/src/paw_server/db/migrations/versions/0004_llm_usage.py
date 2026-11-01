"""track LLM tokens alongside embedding tokens

Revision ID: 0004
Revises: 0003
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE usage_daily ADD COLUMN llm_tokens bigint NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE usage_daily DROP COLUMN llm_tokens")
