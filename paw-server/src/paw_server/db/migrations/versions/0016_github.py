"""GitHub installation connection attempts.

Revision ID: 0016
Revises: 0015
"""

from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE auth_login_attempts DROP CONSTRAINT auth_login_attempts_purpose_check")
    op.execute(
        "ALTER TABLE auth_login_attempts ADD CONSTRAINT auth_login_attempts_purpose_check "
        "CHECK (purpose IN ('login','stepup','connect_gcal','connect_github'))"
    )
    op.execute("""CREATE TABLE github_webhook_receipts (
        delivery_id text PRIMARY KEY, received_at timestamptz NOT NULL DEFAULT now()
    )""")


def downgrade() -> None:
    op.execute("DROP TABLE github_webhook_receipts")
    op.execute("DELETE FROM auth_login_attempts WHERE purpose='connect_github'")
    op.execute("ALTER TABLE auth_login_attempts DROP CONSTRAINT auth_login_attempts_purpose_check")
    op.execute(
        "ALTER TABLE auth_login_attempts ADD CONSTRAINT auth_login_attempts_purpose_check "
        "CHECK (purpose IN ('login','stepup','connect_gcal'))"
    )
