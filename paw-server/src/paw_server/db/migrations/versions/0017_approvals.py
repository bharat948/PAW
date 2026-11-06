"""Immutable, single-use external-write approvals and durable operation receipts.

Revision ID: 0017
Revises: 0016
"""

from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE approval_requests (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
        invocation_id uuid NOT NULL UNIQUE,
        owner_actor text NOT NULL REFERENCES actors(id),
        actor_id text NOT NULL REFERENCES actors(id),
        session_id uuid NOT NULL REFERENCES agent_sessions(id) ON DELETE CASCADE,
        run_id uuid NOT NULL REFERENCES agent_runs(id) ON DELETE CASCADE,
        context_revision bigint NOT NULL,
        profile_version_id uuid NOT NULL REFERENCES actor_profile_versions(id),
        tool text NOT NULL CHECK (tool='github_create_issue'),
        tool_version integer NOT NULL CHECK (tool_version > 0),
        args jsonb NOT NULL, args_hash text NOT NULL,
        repository text NOT NULL, repository_id bigint NOT NULL,
        connection_id uuid NOT NULL REFERENCES connector_connections(id),
        state text NOT NULL DEFAULT 'PENDING' CHECK (state IN ('PENDING','APPROVED','REJECTED','EXPIRED','INVALIDATED')),
        created_at timestamptz NOT NULL DEFAULT now(),
        expires_at timestamptz NOT NULL DEFAULT now() + interval '24 hours',
        decided_at timestamptz
    )""")
    op.execute("""CREATE TABLE approval_operations (
        approval_id uuid PRIMARY KEY REFERENCES approval_requests(id) ON DELETE CASCADE,
        op_key text NOT NULL UNIQUE,
        state text NOT NULL CHECK (state IN ('STARTED','SUCCEEDED','FAILED','UNKNOWN')),
        result jsonb NOT NULL DEFAULT '{}', error_code text,
        started_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now()
    )""")
    op.execute("""CREATE FUNCTION approval_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        IF current_setting('paw.maintenance',true)='redact' THEN RETURN NEW; END IF;
        IF (to_jsonb(NEW)-'state'-'decided_at') IS DISTINCT FROM (to_jsonb(OLD)-'state'-'decided_at') THEN
            RAISE EXCEPTION 'approval binding is immutable' USING ERRCODE='23514';
        END IF;
        IF NEW.state <> OLD.state AND OLD.state <> 'PENDING' THEN
            RAISE EXCEPTION 'approval already consumed' USING ERRCODE='23514';
        END IF;
        RETURN NEW;
    END $$""")
    op.execute(
        "CREATE TRIGGER approval_immutable BEFORE UPDATE ON approval_requests FOR EACH ROW EXECUTE FUNCTION approval_immutable()"
    )
    op.execute("CREATE INDEX approval_inbox ON approval_requests(owner_actor,created_at DESC)")


def downgrade() -> None:
    op.execute("DROP TABLE approval_operations")
    op.execute("DROP TABLE approval_requests")
    op.execute("DROP FUNCTION approval_immutable()")
