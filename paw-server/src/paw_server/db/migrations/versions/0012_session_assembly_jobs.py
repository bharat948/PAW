"""Assembly metadata, redaction revisions and persistent summary jobs.

Revision ID: 0012
Revises: 0011
"""

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""CREATE TABLE gateway_tool_attempts (
        id uuid PRIMARY KEY, run_id uuid NOT NULL REFERENCES agent_runs(id),
        context_revision integer NOT NULL, created_at timestamptz NOT NULL DEFAULT now())""")
    op.execute(
        "INSERT INTO gateway_tool_attempts (id, run_id, context_revision, created_at) "
        "SELECT id, run_id, 0, created_at FROM gateway_tool_invocations WHERE run_id IS NOT NULL"
    )
    op.execute(
        "ALTER TABLE agent_session_messages DROP CONSTRAINT agent_session_messages_tool_invocation_id_fkey"
    )
    op.execute(
        "ALTER TABLE agent_session_messages ADD CONSTRAINT ledger_attempt_fkey "
        "FOREIGN KEY (tool_invocation_id) REFERENCES gateway_tool_attempts(id)"
    )
    op.execute("ALTER TABLE agent_sessions ADD COLUMN context_revision integer NOT NULL DEFAULT 0")
    op.execute(
        "ALTER TABLE agent_runs ADD COLUMN assembler_version text, ADD COLUMN assembly_meta jsonb NOT NULL DEFAULT '{}'::jsonb"
    )
    op.execute("""CREATE TABLE agent_session_jobs (
        session_id uuid PRIMARY KEY REFERENCES agent_sessions(id),
        state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','working','done','failed')),
        attempts integer NOT NULL DEFAULT 0, available_at timestamptz NOT NULL DEFAULT now(),
        locked_at timestamptz, error_code text, source_hash text,
        updated_at timestamptz NOT NULL DEFAULT now(), claim_token uuid)""")

    op.execute(
        "CREATE UNIQUE INDEX agent_summary_one_active ON agent_summaries (session_id) WHERE invalidated_at IS NULL"
    )
    op.execute("""CREATE TABLE agent_summary_usage (
        id uuid PRIMARY KEY DEFAULT gen_random_uuid(),session_id uuid NOT NULL REFERENCES agent_sessions(id),
        charged_actor_id text NOT NULL REFERENCES actors(id),model text NOT NULL,tokens_in integer,tokens_out integer,
        outcome text NOT NULL,created_at timestamptz NOT NULL DEFAULT now())""")


def downgrade() -> None:
    op.execute("DROP TABLE agent_summary_usage")
    op.execute("DROP INDEX agent_summary_one_active")
    op.execute("DROP TABLE agent_session_jobs")
    op.execute("ALTER TABLE agent_runs DROP COLUMN assembler_version, DROP COLUMN assembly_meta")
    op.execute("ALTER TABLE agent_sessions DROP COLUMN context_revision")
    op.execute("ALTER TABLE agent_session_messages DROP CONSTRAINT ledger_attempt_fkey")
    op.execute(
        "ALTER TABLE agent_session_messages ADD CONSTRAINT agent_session_messages_tool_invocation_id_fkey "
        "FOREIGN KEY (tool_invocation_id) REFERENCES gateway_tool_invocations(id) NOT VALID"
    )
    op.execute("DROP TABLE gateway_tool_attempts")
