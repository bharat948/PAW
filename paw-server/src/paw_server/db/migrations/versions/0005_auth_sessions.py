"""server-side login sessions and one-time OAuth login attempts

Revision ID: 0005
Revises: 0004
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE auth_sessions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            token_hash bytea NOT NULL UNIQUE,      -- sha256 of the cookie value; the cookie itself is never stored
            csrf_token text NOT NULL,
            actor_id text NOT NULL,
            email text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now(),
            last_seen_at timestamptz NOT NULL DEFAULT now(),
            expires_at timestamptz NOT NULL,
            authenticated_at timestamptz NOT NULL, -- last real Google sign-in (step-up recency)
            revoked_at timestamptz
        )
        """
    )
    op.execute(
        """
        CREATE TABLE auth_login_attempts (
            state_hash bytea PRIMARY KEY,          -- sha256 of the OAuth state; single use
            nonce text NOT NULL,
            code_verifier text NOT NULL,           -- PKCE
            purpose text NOT NULL CHECK (purpose IN ('login', 'stepup')),
            return_to text NOT NULL,
            session_id uuid REFERENCES auth_sessions(id) ON DELETE CASCADE,
            created_at timestamptz NOT NULL DEFAULT now(),
            used_at timestamptz
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE auth_login_attempts")
    op.execute("DROP TABLE auth_sessions")
