"""actors, immutable actor profile versions and their compiled output (PAW-002C)

Revision ID: 0008
Revises: 0007
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE actors (
            id text PRIMARY KEY,
            kind text NOT NULL CHECK (kind IN ('human', 'agent')),
            display_name text NOT NULL,
            status text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'disabled')),
            created_at timestamptz NOT NULL DEFAULT now(),
            updated_at timestamptz NOT NULL DEFAULT now()   -- Story 3 reads an actor change feed ordered by this
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION actors_touch() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN NEW.updated_at := now(); RETURN NEW; END $$
        """
    )
    op.execute(
        "CREATE TRIGGER actors_touch BEFORE UPDATE ON actors FOR EACH ROW EXECUTE FUNCTION actors_touch()"
    )
    op.execute(
        "INSERT INTO actors (id, kind, display_name) VALUES "
        "('owner', 'human', 'Bharat'), ('admin-hermes', 'agent', 'Admin Hermès')"
    )

    op.execute(
        """
        CREATE TABLE actor_profile_versions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            actor_id text NOT NULL REFERENCES actors(id),
            version integer NOT NULL CHECK (version >= 1),
            spec jsonb NOT NULL,
            spec_hash text NOT NULL,
            status text NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'active', 'retired')),
            created_by text NOT NULL REFERENCES actors(id),
            created_at timestamptz NOT NULL DEFAULT now(),
            activated_at timestamptz,
            UNIQUE (actor_id, version),
            CHECK ((status <> 'active' OR activated_at IS NOT NULL) AND (status <> 'draft' OR activated_at IS NULL))
        )
        """
    )
    # exactly one active version per actor
    op.execute(
        "CREATE UNIQUE INDEX actor_profile_one_active ON actor_profile_versions (actor_id) "
        "WHERE status = 'active'"
    )
    # the spec is immutable; only the status may move forward (draft -> active -> retired)
    op.execute(
        """
        CREATE FUNCTION actor_profile_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'profile versions cannot be deleted' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.actor_id <> OLD.actor_id OR NEW.version <> OLD.version OR NEW.spec <> OLD.spec
               OR NEW.spec_hash <> OLD.spec_hash OR NEW.created_by <> OLD.created_by
               OR NEW.created_at <> OLD.created_at THEN
                RAISE EXCEPTION 'profile versions are immutable' USING ERRCODE = 'check_violation';
            END IF;
            IF NEW.status <> OLD.status AND NOT (
                (OLD.status = 'draft' AND NEW.status IN ('active', 'retired')) OR
                (OLD.status = 'active' AND NEW.status = 'retired')
            ) THEN
                RAISE EXCEPTION 'illegal profile status change % -> %', OLD.status, NEW.status
                    USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.activated_at IS NOT NULL AND NEW.activated_at IS DISTINCT FROM OLD.activated_at THEN
                RAISE EXCEPTION 'activated_at is immutable' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER actor_profile_guard BEFORE UPDATE OR DELETE ON actor_profile_versions "
        "FOR EACH ROW EXECUTE FUNCTION actor_profile_guard()"
    )

    op.execute(
        """
        CREATE TABLE actor_compiled_profiles (
            profile_version_id uuid PRIMARY KEY REFERENCES actor_profile_versions(id),
            instructions_text text NOT NULL,
            config_fragment jsonb NOT NULL,
            constraints_text text NOT NULL,
            policy jsonb NOT NULL,
            compiled_hash text NOT NULL,
            compiler_version text NOT NULL,
            created_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE FUNCTION actor_compiled_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'compiled profiles are immutable' USING ERRCODE = 'check_violation';
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER actor_compiled_immutable BEFORE UPDATE OR DELETE ON actor_compiled_profiles "
        "FOR EACH ROW EXECUTE FUNCTION actor_compiled_immutable()"
    )

    # Story 4: runtimes and runs know which agent actor they execute
    op.execute(
        "ALTER TABLE agent_runtimes ADD COLUMN actor_id text NOT NULL DEFAULT 'admin-hermes' REFERENCES actors(id)"
    )
    op.execute(
        "ALTER TABLE agent_runs ADD COLUMN executing_actor_id text NOT NULL DEFAULT 'admin-hermes' "
        "REFERENCES actors(id)"
    )
    op.execute(
        "ALTER TABLE agent_runs ADD COLUMN profile_version_id uuid REFERENCES actor_profile_versions(id)"
    )
    op.execute("ALTER TABLE agent_runs ADD FOREIGN KEY (actor_id) REFERENCES actors(id)")


def downgrade() -> None:
    op.execute("ALTER TABLE agent_runs DROP CONSTRAINT agent_runs_actor_id_fkey")
    op.execute("ALTER TABLE agent_runs DROP COLUMN profile_version_id")
    op.execute("ALTER TABLE agent_runs DROP COLUMN executing_actor_id")
    op.execute("ALTER TABLE agent_runtimes DROP COLUMN actor_id")
    op.execute("DROP TABLE actor_compiled_profiles")
    op.execute("DROP TABLE actor_profile_versions")
    op.execute("DROP TABLE actors")
    for f in ("actor_compiled_immutable", "actor_profile_guard", "actors_touch"):
        op.execute(f"DROP FUNCTION {f}()")
