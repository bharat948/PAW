"""connector connections and encrypted secrets (Google Calendar first)

Revision ID: 0007
Revises: 0006
"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE auth_login_attempts DROP CONSTRAINT auth_login_attempts_purpose_check")
    op.execute(
        "ALTER TABLE auth_login_attempts ADD CONSTRAINT auth_login_attempts_purpose_check "
        "CHECK (purpose IN ('login', 'stepup', 'connect_gcal'))"
    )
    op.execute(
        """
        CREATE TABLE connector_connections (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            provider text NOT NULL,
            owner_actor text NOT NULL,
            account_email text NOT NULL,
            scopes text[] NOT NULL,
            status text NOT NULL DEFAULT 'active'
                CHECK (status IN ('active', 'needs_reauth', 'revoked')),
            last_error text,
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now(),
            revoked_at timestamptz,
            CHECK ((status = 'revoked') = (revoked_at IS NOT NULL))
        )
        """
    )
    # one live connection per owner and provider
    op.execute(
        "CREATE UNIQUE INDEX connector_one_live ON connector_connections (owner_actor, provider) "
        "WHERE status <> 'revoked'"
    )
    op.execute(
        """
        CREATE TABLE connector_secrets (
            connection_id uuid PRIMARY KEY REFERENCES connector_connections(id) ON DELETE CASCADE,
            ciphertext bytea NOT NULL,   -- Fernet token; the key lives outside the database
            key_id text NOT NULL,
            updated_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE connector_secrets")
    op.execute("DROP TABLE connector_connections")
    op.execute("DELETE FROM auth_login_attempts WHERE purpose = 'connect_gcal'")
    op.execute("ALTER TABLE auth_login_attempts DROP CONSTRAINT auth_login_attempts_purpose_check")
    op.execute(
        "ALTER TABLE auth_login_attempts ADD CONSTRAINT auth_login_attempts_purpose_check "
        "CHECK (purpose IN ('login', 'stepup'))"
    )
