"""MCP gateway: tool registry mirror and the append-only audit of every tool decision (PAW-002D)

Revision ID: 0009
Revises: 0008
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE gateway_tools (
            name text PRIMARY KEY CHECK (name ~ '^[a-z][a-z0-9_]{2,63}$'),
            version integer NOT NULL CHECK (version >= 1),
            side_effect_class text NOT NULL
                CHECK (side_effect_class IN ('read', 'internal_write', 'external_write')),
            approval_required boolean NOT NULL DEFAULT false,
            provider text,                                  -- connector the tool needs, e.g. google_calendar
            description text NOT NULL,
            schema_json jsonb NOT NULL,
            enabled boolean NOT NULL DEFAULT true,          -- the owner's kill switch, independent of grants
            updated_at timestamptz NOT NULL DEFAULT now(),
            CHECK (side_effect_class <> 'external_write' OR approval_required)
        )
        """
    )
    op.execute(
        """
        CREATE TABLE gateway_tool_invocations (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            run_id uuid REFERENCES agent_runs(id),          -- null when no run could be resolved (NO_ACTIVE_RUN)
            actor_id text REFERENCES actors(id),
            tool text NOT NULL,                             -- as requested, even if unknown
            tool_version integer,
            args_redacted jsonb NOT NULL DEFAULT '{}'::jsonb,
            decision text NOT NULL CHECK (decision IN ('allowed', 'denied', 'error')),
            reason_code text NOT NULL,
            latency_ms integer,
            result_bytes integer,
            truncated boolean NOT NULL DEFAULT false,
            error text,
            trace_id text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX gateway_invocations_run ON gateway_tool_invocations (run_id, created_at)"
    )
    op.execute(
        """
        CREATE FUNCTION gateway_invocations_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'gateway_tool_invocations is append-only' USING ERRCODE = 'check_violation';
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER gateway_invocations_append_only BEFORE UPDATE OR DELETE ON gateway_tool_invocations "
        "FOR EACH ROW EXECUTE FUNCTION gateway_invocations_append_only()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE gateway_tool_invocations")
    op.execute("DROP FUNCTION gateway_invocations_append_only()")
    op.execute("DROP TABLE gateway_tools")
