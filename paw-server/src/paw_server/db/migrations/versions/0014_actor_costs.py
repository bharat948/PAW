"""A1 immutable priced attempts, settlements and daily authorization locks."""

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE ops_rate_cards ADD COLUMN cached_input_per_mtok numeric(12,6), ADD COLUMN context_tokens integer, ADD COLUMN max_output_tokens integer, ADD COLUMN served_models text[]"
    )
    # Official model page verified 2026-11-04. Standard text pricing; no batch/flex/tools.
    op.execute("""INSERT INTO ops_rate_cards
        (version,model,input_per_mtok,output_per_mtok,effective_from,cached_input_per_mtok,context_tokens,max_output_tokens,served_models)
        VALUES ('openai-2026-11-04','gpt-4o-mini',0.15,0.60,'2026-11-04',0.075,128000,16384,
                ARRAY['gpt-4o-mini','gpt-4o-mini-2024-07-18'])""")
    op.execute("ALTER TABLE agent_runs ADD COLUMN deadline_at timestamptz")
    op.execute("""CREATE TABLE actor_cost_attempts (
        id uuid PRIMARY KEY, workspace_id uuid NOT NULL, owner_id text NOT NULL,
        actor_id text NOT NULL, session_id uuid NOT NULL, run_id uuid,
        profile_version_id uuid NOT NULL, kind text NOT NULL CHECK(kind IN ('run','summary')),
        budget_day date NOT NULL, requested_model text NOT NULL, rate_version text NOT NULL,
        input_bound integer NOT NULL CHECK(input_bound>0), output_bound integer NOT NULL CHECK(output_bound>0),
        reserved_usd numeric(18,9) NOT NULL CHECK(reserved_usd>0),
        authorized_at timestamptz NOT NULL DEFAULT now(),
        FOREIGN KEY(rate_version,requested_model) REFERENCES ops_rate_cards(version,model),
        CHECK ((kind='run') = (run_id IS NOT NULL))
    )""")
    # No content or credentials; retained independently of conversation purge for budget integrity.
    op.execute("""CREATE TABLE actor_cost_results (
        attempt_id uuid PRIMARY KEY REFERENCES actor_cost_attempts(id),
        state text NOT NULL DEFAULT 'reserved' CHECK(state IN ('reserved','forwarded','uncertain','settled','released')),
        cost_usd numeric(18,9) CHECK(cost_usd>=0), served_model text,
        tokens_in integer CHECK(tokens_in>=0), tokens_out integer CHECK(tokens_out>=0),
        cached_tokens integer CHECK(cached_tokens>=0), updated_at timestamptz NOT NULL DEFAULT now(),
        CHECK ((state='settled') = (cost_usd IS NOT NULL))
    )""")
    op.execute(
        "CREATE INDEX actor_cost_day ON actor_cost_attempts(workspace_id,budget_day,actor_id)"
    )
    op.execute("CREATE INDEX actor_cost_run ON actor_cost_attempts(run_id)")
    op.execute("""CREATE TABLE actor_budget_days (
        workspace_id uuid NOT NULL, budget_day date NOT NULL,
        PRIMARY KEY(workspace_id,budget_day)
    )""")
    op.execute("""CREATE FUNCTION actor_cost_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'priced evidence is immutable' USING ERRCODE='check_violation'; END $$""")
    for table in ("actor_cost_attempts", "ops_rate_cards"):
        op.execute(
            f"CREATE TRIGGER cost_immutable BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION actor_cost_immutable()"
        )
    op.execute("""CREATE FUNCTION actor_cost_result_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP='DELETE' OR OLD.state IN ('settled','released') OR NEW.attempt_id <> OLD.attempt_id OR NOT (
                (OLD.state='reserved' AND NEW.state IN ('forwarded','released')) OR
                (OLD.state='forwarded' AND NEW.state IN ('uncertain','settled')) OR
                (OLD.state='uncertain' AND NEW.state='settled')) THEN
                RAISE EXCEPTION 'invalid cost transition' USING ERRCODE='check_violation';
            END IF;
            RETURN NEW;
        END $$""")
    op.execute(
        "CREATE TRIGGER cost_result_guard BEFORE UPDATE OR DELETE ON actor_cost_results FOR EACH ROW EXECUTE FUNCTION actor_cost_result_guard()"
    )


def downgrade() -> None:
    op.execute("DROP TABLE actor_budget_days, actor_cost_results, actor_cost_attempts")
    op.execute("DROP FUNCTION actor_cost_result_guard()")
    op.execute("DROP TRIGGER cost_immutable ON ops_rate_cards")
    op.execute("DROP FUNCTION actor_cost_immutable()")
    op.execute("DELETE FROM ops_rate_cards WHERE version='openai-2026-11-04'")
    op.execute("ALTER TABLE agent_runs DROP COLUMN deadline_at")
    op.execute(
        "ALTER TABLE ops_rate_cards DROP COLUMN cached_input_per_mtok, DROP COLUMN context_tokens, DROP COLUMN max_output_tokens, DROP COLUMN served_models"
    )
