"""Retained fork prefixes and privacy fences.

Revision ID: 0013
Revises: 0012
"""

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None
MODE = "coalesce(current_setting('paw.maintenance', true), '')"


def upgrade() -> None:
    op.execute("ALTER TABLE agent_runs ADD COLUMN cancel_requested_at timestamptz")
    op.execute(
        "ALTER TABLE agent_sessions ADD COLUMN fork_request_id text, ADD COLUMN fork_message_id uuid"
    )
    op.execute(
        "CREATE UNIQUE INDEX ledger_fork_request ON agent_sessions (owner, forked_from_session_id, fork_request_id) WHERE fork_request_id IS NOT NULL"
    )
    op.execute(
        "ALTER TABLE agent_session_messages ADD COLUMN source_message_id uuid, ADD COLUMN source_session_id uuid"
    )
    op.execute(
        "CREATE INDEX ledger_source ON agent_session_messages (source_message_id) WHERE source_message_id IS NOT NULL"
    )
    # Retained prefixes are snapshots of messages, never copied executions.
    op.execute("ALTER TABLE agent_session_messages DROP CONSTRAINT agent_session_messages_check1")
    op.execute(
        "ALTER TABLE agent_session_messages ADD CONSTRAINT ledger_tool_source CHECK "
        "(kind NOT IN ('tool_call','tool_result') OR run_id IS NOT NULL OR source_message_id IS NOT NULL)"
    )
    op.execute(
        "CREATE TABLE agent_run_redactions (run_id uuid PRIMARY KEY REFERENCES agent_runs(id))"
    )
    op.execute("""CREATE FUNCTION ledger_summary_range() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM agent_session_messages WHERE id = NEW.message_id
                AND session_id = NEW.session_id AND kind = 'summary') THEN
                RAISE EXCEPTION 'summary message must belong to its session' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$""")
    op.execute(
        "CREATE TRIGGER ledger_summary_range BEFORE INSERT ON agent_summaries FOR EACH ROW EXECUTE FUNCTION ledger_summary_range()"
    )
    # A purge may detach retained children after their prefix has been materialized.
    op.execute(f"""CREATE OR REPLACE FUNCTION agent_sessions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF {MODE} = 'purge' THEN RETURN OLD; END IF;
                RAISE EXCEPTION 'sessions are deleted through purge only' USING ERRCODE = 'check_violation';
            END IF;
            IF {MODE} = 'purge' AND NEW.forked_from_session_id IS NULL AND NEW.fork_seq IS NULL
               AND NEW.delegated_from_session_id IS NULL AND NEW.delegated_from_run_id IS NULL
               AND NEW.delegated_from_invocation_id IS NULL AND NEW.kind = 'chat' AND NEW.root_session_id = NEW.id
               AND (to_jsonb(NEW) - ARRAY['forked_from_session_id','fork_seq','delegated_from_session_id',
                   'delegated_from_run_id','delegated_from_invocation_id','kind','root_session_id']) =
                   (to_jsonb(OLD) - ARRAY['forked_from_session_id','fork_seq','delegated_from_session_id',
                   'delegated_from_run_id','delegated_from_invocation_id','kind','root_session_id']) THEN
                RETURN NEW;
            END IF;
            IF NEW.owner <> OLD.owner OR NEW.requester_actor_id <> OLD.requester_actor_id
               OR NEW.agent_actor_id <> OLD.agent_actor_id OR NEW.kind <> OLD.kind
               OR NEW.workspace_id <> OLD.workspace_id OR NEW.root_session_id <> OLD.root_session_id
               OR NEW.forked_from_session_id IS DISTINCT FROM OLD.forked_from_session_id
               OR NEW.fork_seq IS DISTINCT FROM OLD.fork_seq
               OR NEW.delegated_from_session_id IS DISTINCT FROM OLD.delegated_from_session_id
               OR NEW.delegated_from_run_id IS DISTINCT FROM OLD.delegated_from_run_id
               OR NEW.delegated_from_invocation_id IS DISTINCT FROM OLD.delegated_from_invocation_id
               OR NEW.created_at <> OLD.created_at THEN
                RAISE EXCEPTION 'session roles and lineage are immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.deleted_at IS NOT NULL AND NEW.deleted_at IS DISTINCT FROM OLD.deleted_at THEN
                RAISE EXCEPTION 'deleted sessions cannot be restored' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$""")
    op.execute(f"""CREATE OR REPLACE FUNCTION gateway_invocations_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND {MODE} = 'purge' THEN RETURN OLD; END IF;
            IF TG_OP = 'UPDATE' AND {MODE} = 'redact' AND NEW.args_redacted = '{{}}'::jsonb AND NEW.error IS NULL
               AND (to_jsonb(NEW) - ARRAY['args_redacted','error']) = (to_jsonb(OLD) - ARRAY['args_redacted','error']) THEN RETURN NEW; END IF;
            RAISE EXCEPTION 'gateway invocations are append-only' USING ERRCODE = 'check_violation';
        END $$""")


def downgrade() -> None:
    op.execute("ALTER TABLE agent_runs DROP COLUMN cancel_requested_at")
    op.execute("DROP TRIGGER ledger_summary_range ON agent_summaries")
    op.execute("DROP FUNCTION ledger_summary_range()")
    op.execute("DROP TABLE agent_run_redactions")
    op.execute(
        "ALTER TABLE agent_sessions DROP COLUMN fork_request_id, DROP COLUMN fork_message_id"
    )
    op.execute("ALTER TABLE agent_session_messages DROP CONSTRAINT ledger_tool_source")
    op.execute(
        "ALTER TABLE agent_session_messages ADD CONSTRAINT agent_session_messages_check1 "
        "CHECK (kind NOT IN ('tool_call','tool_result') OR run_id IS NOT NULL) NOT VALID"
    )
    op.execute(
        "ALTER TABLE agent_session_messages DROP COLUMN source_message_id, DROP COLUMN source_session_id"
    )
    # 0011's downgrade restores all shared guard functions when crossing its boundary.
