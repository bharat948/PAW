"""agent runtime: sessions, runs, append-only run events, lease, rate cards

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE agent_runtimes (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            name text NOT NULL UNIQUE,
            hermes_version text,
            image_digest text,
            status text NOT NULL DEFAULT 'unknown'
                CHECK (status IN ('unknown', 'ready', 'unready', 'down')),
            last_health_at timestamptz,
            reported_hashes jsonb NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_sessions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            owner text NOT NULL,
            workspace_id uuid NOT NULL REFERENCES workspaces(id),
            title text NOT NULL DEFAULT '',
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_runs (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id uuid NOT NULL REFERENCES agent_sessions(id),
            actor_id text NOT NULL,
            runtime_id uuid REFERENCES agent_runtimes(id),
            client_request_id text NOT NULL,
            user_message text NOT NULL,
            state text NOT NULL DEFAULT 'QUEUED' CHECK (state IN (
                'QUEUED', 'DISPATCHING', 'RUNNING', 'CANCELLING',
                'SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED')),
            profile_version integer,
            compiled_hash text,
            assembly_hash text,
            hermes_run_id text,
            requested_model text,
            served_model text,
            tokens_in integer,
            tokens_out integer,
            cache_read integer,
            cache_write integer,
            cost_usd numeric(12, 6),
            rate_card_version text,
            error_code text,
            output text,
            queued_at timestamptz NOT NULL DEFAULT now(),
            started_at timestamptz,
            finished_at timestamptz,
            UNIQUE (session_id, client_request_id),
            CHECK ((state IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED'))
                   = (finished_at IS NOT NULL))
        )
        """
    )
    # at most one non-terminal run per session
    op.execute(
        """
        CREATE UNIQUE INDEX agent_runs_one_active_per_session ON agent_runs (session_id)
        WHERE state IN ('QUEUED', 'DISPATCHING', 'RUNNING', 'CANCELLING')
        """
    )
    op.execute("CREATE INDEX agent_runs_queue ON agent_runs (queued_at) WHERE state = 'QUEUED'")
    # terminal states are immutable, and only legal transitions are allowed
    op.execute(
        """
        CREATE FUNCTION agent_runs_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF OLD.state IN ('SUCCEEDED', 'FAILED', 'CANCELLED', 'INTERRUPTED') THEN
                RAISE EXCEPTION 'agent run % is terminal (%)', OLD.id, OLD.state
                    USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.state <> OLD.state AND NOT (
                (OLD.state = 'QUEUED' AND NEW.state IN ('DISPATCHING', 'CANCELLED')) OR
                (OLD.state = 'DISPATCHING' AND NEW.state IN ('RUNNING', 'FAILED', 'INTERRUPTED')) OR
                (OLD.state = 'RUNNING' AND NEW.state IN
                    ('SUCCEEDED', 'FAILED', 'CANCELLING', 'INTERRUPTED')) OR
                (OLD.state = 'CANCELLING' AND NEW.state IN ('CANCELLED', 'INTERRUPTED'))
            ) THEN
                RAISE EXCEPTION 'illegal agent run transition % -> %', OLD.state, NEW.state
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_runs_guard BEFORE UPDATE ON agent_runs
        FOR EACH ROW EXECUTE FUNCTION agent_runs_guard()
        """
    )
    op.execute(
        """
        CREATE TABLE agent_run_events (
            run_id uuid NOT NULL REFERENCES agent_runs(id),
            seq integer NOT NULL CHECK (seq >= 1),
            type text NOT NULL,
            payload jsonb NOT NULL DEFAULT '{}'::jsonb,
            source_event_id text,
            occurred_at timestamptz NOT NULL DEFAULT now(),
            PRIMARY KEY (run_id, seq)
        )
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX agent_run_events_source ON agent_run_events (run_id, source_event_id) "
        "WHERE source_event_id IS NOT NULL"
    )
    op.execute(
        """
        CREATE FUNCTION agent_run_events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'agent_run_events is append-only' USING ERRCODE = 'check_violation';
        END $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER agent_run_events_append_only BEFORE UPDATE OR DELETE ON agent_run_events
        FOR EACH ROW EXECUTE FUNCTION agent_run_events_append_only()
        """
    )
    # one lease row per runtime: at most one RUNNING run per runtime
    op.execute(
        """
        CREATE TABLE agent_run_leases (
            runtime_id uuid PRIMARY KEY REFERENCES agent_runtimes(id),
            run_id uuid NOT NULL UNIQUE REFERENCES agent_runs(id),
            acquired_at timestamptz NOT NULL DEFAULT now(),
            heartbeat_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE ops_rate_cards (
            version text NOT NULL,
            model text NOT NULL,
            input_per_mtok numeric(12, 6) NOT NULL CHECK (input_per_mtok >= 0),
            output_per_mtok numeric(12, 6) NOT NULL CHECK (output_per_mtok >= 0),
            effective_from date NOT NULL,
            PRIMARY KEY (version, model)
        )
        """
    )


def downgrade() -> None:
    for t in (
        "ops_rate_cards",
        "agent_run_leases",
        "agent_run_events",
        "agent_runs",
        "agent_sessions",
        "agent_runtimes",
    ):
        op.execute(f"DROP TABLE {t} CASCADE")
    op.execute("DROP FUNCTION agent_run_events_append_only()")
    op.execute("DROP FUNCTION agent_runs_guard()")
