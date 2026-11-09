"""Runtime kind (hermes | voice), immutable once registered, and the inert voice-agent actor.

The voice-agent actor has no profile, runtime or credential until an operator registers one, so
installations that never use voice are unchanged.

Revision ID: 0018
Revises: 0017
"""

from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE agent_runtimes
        ADD COLUMN kind text NOT NULL DEFAULT 'hermes' CHECK (kind IN ('hermes', 'voice'))
    """)
    op.execute("""
        CREATE OR REPLACE FUNCTION runtime_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.actor_id <> OLD.actor_id THEN
                RAISE EXCEPTION 'runtime actor binding is immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.kind <> OLD.kind THEN
                RAISE EXCEPTION 'runtime kind is immutable' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute(
        "INSERT INTO actors (id, kind, display_name) VALUES ('voice-agent', 'agent', 'Voice Agent') "
        "ON CONFLICT (id) DO NOTHING"
    )


def downgrade() -> None:
    op.execute("""
        CREATE OR REPLACE FUNCTION runtime_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.actor_id <> OLD.actor_id THEN
                RAISE EXCEPTION 'runtime actor binding is immutable' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("ALTER TABLE agent_runtimes DROP COLUMN kind")
    # The voice-agent actor is left in place: ledger rows may already reference it.
