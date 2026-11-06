"""Actor-bound runtime registry and revocable, purpose-bound credential hashes.

Revision ID: 0015
Revises: 0014
"""

from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE actor_profile_versions ADD CONSTRAINT profile_actor_id UNIQUE (id, actor_id)"
    )
    op.execute("""
        ALTER TABLE agent_runtimes
        ADD COLUMN endpoint_url text,
        ADD COLUMN key_env text,
        ADD COLUMN desired_state text NOT NULL DEFAULT 'running'
            CHECK (desired_state IN ('running', 'stopped')),
        ADD COLUMN desired_profile_version_id uuid,
        ADD CONSTRAINT runtime_actor_id UNIQUE (id, actor_id),
        ADD CONSTRAINT runtime_profile_actor FOREIGN KEY (desired_profile_version_id, actor_id)
            REFERENCES actor_profile_versions(id, actor_id)
    """)
    op.execute("""
        UPDATE agent_runtimes rt SET desired_profile_version_id = p.id
        FROM actor_profile_versions p WHERE p.actor_id = rt.actor_id AND p.status = 'active'
    """)
    op.execute("""
        ALTER TABLE agent_runs ADD CONSTRAINT run_runtime_actor
        FOREIGN KEY (runtime_id, executing_actor_id) REFERENCES agent_runtimes(id, actor_id)
    """)
    op.execute("""
        ALTER TABLE agent_runs ADD CONSTRAINT run_profile_actor
        FOREIGN KEY (profile_version_id, executing_actor_id) REFERENCES actor_profile_versions(id, actor_id)
    """)
    op.execute("""
        CREATE UNIQUE INDEX agent_runs_one_executing_per_actor ON agent_runs(executing_actor_id)
        WHERE state IN ('DISPATCHING','RUNNING','CANCELLING')
    """)
    op.execute("""
        CREATE TABLE runtime_credentials (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            runtime_id uuid NOT NULL REFERENCES agent_runtimes(id),
            purpose text NOT NULL CHECK (purpose IN ('gateway', 'model')),
            token_hash text NOT NULL UNIQUE CHECK (token_hash ~ '^[0-9a-f]{64}$'),
            created_at timestamptz NOT NULL DEFAULT now(),
            revoked_at timestamptz
        )
    """)
    op.execute("""
        CREATE UNIQUE INDEX runtime_credential_live ON runtime_credentials(runtime_id, purpose)
        WHERE revoked_at IS NULL
    """)
    op.execute("""
        CREATE FUNCTION runtime_identity_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.actor_id <> OLD.actor_id THEN
                RAISE EXCEPTION 'runtime actor binding is immutable' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER runtime_identity_guard BEFORE UPDATE ON agent_runtimes
        FOR EACH ROW EXECUTE FUNCTION runtime_identity_guard()
    """)
    op.execute("""
        CREATE FUNCTION runtime_credential_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.id <> OLD.id OR NEW.runtime_id <> OLD.runtime_id OR NEW.purpose <> OLD.purpose
               OR NEW.token_hash <> OLD.token_hash OR NEW.created_at <> OLD.created_at
               OR (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at) THEN
                RAISE EXCEPTION 'credential identity and revocation are immutable'
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$
    """)
    op.execute("""
        CREATE TRIGGER runtime_credential_guard BEFORE UPDATE ON runtime_credentials
        FOR EACH ROW EXECUTE FUNCTION runtime_credential_guard()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER runtime_identity_guard ON agent_runtimes")
    op.execute("DROP FUNCTION runtime_identity_guard()")
    op.execute("DROP TABLE runtime_credentials")
    op.execute("DROP FUNCTION runtime_credential_guard()")
    op.execute("DROP INDEX agent_runs_one_executing_per_actor")
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT run_runtime_actor")
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT run_profile_actor")
    op.execute("""
        ALTER TABLE agent_runtimes DROP CONSTRAINT runtime_profile_actor,
        DROP CONSTRAINT runtime_actor_id, DROP COLUMN endpoint_url, DROP COLUMN key_env,
        DROP COLUMN desired_state, DROP COLUMN desired_profile_version_id
    """)
    op.execute("ALTER TABLE actor_profile_versions DROP CONSTRAINT profile_actor_id")
