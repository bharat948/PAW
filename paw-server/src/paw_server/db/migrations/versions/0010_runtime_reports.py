"""what the Hermes runtime reports about itself: boot config, per-model-call tool lists, blocked native tool calls

Revision ID: 0010
Revises: 0009
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE runtime_reports (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            runtime_name text NOT NULL,
            kind text NOT NULL CHECK (kind IN ('boot', 'call', 'blocked')),
            hermes_run_id text,                 -- call and blocked reports: the run it happened in
            config_hash text,                   -- boot: hash of the compiled config the container applied
            model text,
            provider text,
            tool_names text[],                  -- call: exactly the tools the model call carried
            tool_name text,                     -- blocked: the native tool that was attempted
            observed_at timestamptz NOT NULL DEFAULT now(),
            CHECK (kind <> 'call' OR (hermes_run_id IS NOT NULL AND tool_names IS NOT NULL)),
            CHECK (kind <> 'blocked' OR tool_name IS NOT NULL),
            CHECK (kind <> 'boot' OR config_hash IS NOT NULL)
        )
        """
    )
    op.execute(
        "CREATE INDEX runtime_reports_run ON runtime_reports (hermes_run_id, observed_at DESC)"
    )
    op.execute(
        "CREATE INDEX runtime_reports_kind ON runtime_reports (runtime_name, kind, observed_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE runtime_reports")
