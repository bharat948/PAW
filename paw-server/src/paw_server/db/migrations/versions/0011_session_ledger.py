"""session ledger (PAW-002B): session roles and lineage, append-only messages, removable content, summaries,
deletion log, and a narrow maintenance mode for "forget" and "purge"

Revision ID: 0011
Revises: 0010

Three roles are kept apart (plan-actor-foundation.md section 7): the data owner (`agent_sessions.owner`), the requester
(`requester_actor_id`: who asks) and the executor (`agent_actor_id`: who answers). Two lineage links are kept apart:
`forked_from_*` carries history up to an edit point, `delegated_from_*` a scoped assignment.

Append-only tables refuse UPDATE and DELETE, except inside a transaction that sets `paw.maintenance`:
  redact  blank content (forget a message): delete a content row, blank run text, replace event payloads
  purge   delete every row of a deleted session after its retention period
This guards against bugs in normal code paths. It is not a defence against someone with direct database access.
"""

import json
from typing import Any

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

MODE = "coalesce(current_setting('paw.maintenance', true), '')"


def upgrade() -> None:
    # ---- actors: a system author for platform events ------------------------------------------------------------
    op.execute("ALTER TABLE actors DROP CONSTRAINT actors_kind_check")
    op.execute(
        "ALTER TABLE actors ADD CONSTRAINT actors_kind_check CHECK (kind IN ('human', 'agent', 'system'))"
    )
    op.execute("INSERT INTO actors (id, kind, display_name) VALUES ('system', 'system', 'PAW')")

    # ---- sessions: roles, lineage, list fields, deletion --------------------------------------------------------
    op.execute(
        """
        ALTER TABLE agent_sessions
            ADD COLUMN requester_actor_id text NOT NULL DEFAULT 'owner' REFERENCES actors(id),
            ADD COLUMN agent_actor_id text NOT NULL DEFAULT 'admin-hermes' REFERENCES actors(id),
            ADD COLUMN kind text NOT NULL DEFAULT 'chat' CHECK (kind IN ('chat', 'delegation')),
            ADD COLUMN forked_from_session_id uuid REFERENCES agent_sessions(id),
            ADD COLUMN fork_seq integer CHECK (fork_seq >= 0),
            ADD COLUMN delegated_from_session_id uuid REFERENCES agent_sessions(id),
            ADD COLUMN delegated_from_run_id uuid REFERENCES agent_runs(id),
            ADD COLUMN delegated_from_invocation_id uuid REFERENCES gateway_tool_invocations(id),
            ADD COLUMN root_session_id uuid REFERENCES agent_sessions(id),
            ADD COLUMN title_source text NOT NULL DEFAULT 'auto' CHECK (title_source IN ('auto', 'user')),
            ADD COLUMN last_activity_at timestamptz NOT NULL DEFAULT now(),
            ADD COLUMN deleted_at timestamptz,
            ADD COLUMN purge_after timestamptz,
            ADD CONSTRAINT agent_sessions_fork_pair CHECK ((forked_from_session_id IS NULL) = (fork_seq IS NULL)),
            ADD CONSTRAINT agent_sessions_kind_lineage CHECK (
                (kind = 'delegation') = (delegated_from_session_id IS NOT NULL)),
            ADD CONSTRAINT agent_sessions_fork_or_delegation CHECK (
                forked_from_session_id IS NULL OR delegated_from_session_id IS NULL),
            ADD CONSTRAINT agent_sessions_delete_pair CHECK ((deleted_at IS NULL) = (purge_after IS NULL)),
            ADD CONSTRAINT agent_sessions_not_own_parent CHECK (
                forked_from_session_id IS DISTINCT FROM id AND delegated_from_session_id IS DISTINCT FROM id)
        """
    )
    op.execute("UPDATE agent_sessions SET root_session_id = id")
    op.execute(
        """
        UPDATE agent_sessions s SET last_activity_at = greatest(s.created_at,
            coalesce((SELECT max(queued_at) FROM agent_runs r WHERE r.session_id = s.id), s.created_at))
        """
    )
    # root of a delegation tree: inherited from the parent; every other session is its own root
    op.execute(
        """
        CREATE FUNCTION agent_sessions_root() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.delegated_from_session_id IS NOT NULL THEN
                SELECT root_session_id INTO NEW.root_session_id FROM agent_sessions
                 WHERE id = NEW.delegated_from_session_id;
            ELSE
                NEW.root_session_id := NEW.id;
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER agent_sessions_root BEFORE INSERT ON agent_sessions "
        "FOR EACH ROW EXECUTE FUNCTION agent_sessions_root()"
    )
    op.execute("ALTER TABLE agent_sessions ALTER COLUMN root_session_id SET NOT NULL")
    op.execute(
        f"""
        CREATE FUNCTION agent_sessions_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF {MODE} = 'purge' THEN RETURN OLD; END IF;
                RAISE EXCEPTION 'sessions are deleted through the purge job only' USING ERRCODE = 'check_violation';
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
                RAISE EXCEPTION 'a deleted session cannot be restored or re-dated' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER agent_sessions_guard BEFORE UPDATE OR DELETE ON agent_sessions "
        "FOR EACH ROW EXECUTE FUNCTION agent_sessions_guard()"
    )
    op.execute(
        "CREATE INDEX agent_sessions_list ON agent_sessions (owner, last_activity_at DESC) "
        "WHERE deleted_at IS NULL AND kind = 'chat'"
    )
    op.execute("CREATE INDEX agent_sessions_root_idx ON agent_sessions (root_session_id)")

    # ---- the ledger --------------------------------------------------------------------------------------------
    op.execute(
        """
        CREATE TABLE agent_session_messages (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id uuid NOT NULL REFERENCES agent_sessions(id),
            seq integer NOT NULL CHECK (seq >= 1),
            kind text NOT NULL CHECK (kind IN (
                'user', 'assistant', 'tool_call', 'tool_result', 'platform_event', 'summary', 'delegation')),
            author_actor_id text NOT NULL REFERENCES actors(id),
            run_id uuid REFERENCES agent_runs(id),
            tool_invocation_id uuid REFERENCES gateway_tool_invocations(id),
            status text NOT NULL DEFAULT 'complete' CHECK (status IN ('complete', 'unfinished')),
            token_estimate integer NOT NULL DEFAULT 0 CHECK (token_estimate >= 0),
            created_at timestamptz NOT NULL DEFAULT now(),
            UNIQUE (session_id, seq),
            CHECK (status = 'complete' OR kind = 'assistant'),
            CHECK (kind NOT IN ('tool_call', 'tool_result') OR run_id IS NOT NULL)
        )
        """
    )
    op.execute("CREATE INDEX agent_session_messages_run ON agent_session_messages (run_id)")
    op.execute(
        "CREATE UNIQUE INDEX ledger_run_message ON agent_session_messages (run_id, kind) "
        "WHERE run_id IS NOT NULL AND kind IN ('user', 'assistant')"
    )
    op.execute(
        "CREATE UNIQUE INDEX ledger_invocation_message ON agent_session_messages (tool_invocation_id, kind) "
        "WHERE tool_invocation_id IS NOT NULL AND kind IN ('tool_call', 'tool_result')"
    )
    op.execute(
        """
        CREATE TABLE agent_message_contents (
            message_id uuid PRIMARY KEY REFERENCES agent_session_messages(id),
            body text NOT NULL DEFAULT '',
            data jsonb NOT NULL DEFAULT '{}'::jsonb
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_message_redactions (
            message_id uuid PRIMARY KEY REFERENCES agent_session_messages(id),
            redacted_by text NOT NULL REFERENCES actors(id),
            reason text NOT NULL DEFAULT 'forgotten',
            redacted_at timestamptz NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        """
        CREATE TABLE agent_summaries (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id uuid NOT NULL REFERENCES agent_sessions(id),
            from_seq integer NOT NULL CHECK (from_seq >= 1),
            to_seq integer NOT NULL,
            message_id uuid NOT NULL UNIQUE REFERENCES agent_session_messages(id),
            model text NOT NULL,
            prompt_version text NOT NULL,
            charged_actor_id text NOT NULL REFERENCES actors(id),
            tokens_in integer,
            tokens_out integer,
            created_at timestamptz NOT NULL DEFAULT now(),
            invalidated_at timestamptz,
            CHECK (to_seq >= from_seq)
        )
        """
    )
    op.execute("CREATE INDEX agent_summaries_session ON agent_summaries (session_id, to_seq DESC)")
    # no foreign keys: this log must outlive the rows it describes, and is re-applied after a restore from backup
    op.execute(
        """
        CREATE TABLE data_deletions (
            id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            scope text NOT NULL CHECK (scope IN ('session', 'message')),
            session_id uuid NOT NULL,
            message_id uuid,
            include_delegated boolean NOT NULL DEFAULT false,
            requested_by text NOT NULL,
            requested_at timestamptz NOT NULL DEFAULT now(),
            purge_after timestamptz,
            purged_at timestamptz,
            CHECK ((scope = 'message') = (message_id IS NOT NULL))
        )
        """
    )

    # ---- append-only rules with the maintenance exceptions -----------------------------------------------------
    op.execute(
        f"""
        CREATE FUNCTION ledger_messages_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND {MODE} = 'purge' THEN RETURN OLD; END IF;
            RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = 'check_violation';
        END $$
        """
    )
    for t in ("agent_session_messages", "agent_message_redactions"):
        op.execute(
            f"CREATE TRIGGER {t}_guard BEFORE UPDATE OR DELETE ON {t} "
            f"FOR EACH ROW EXECUTE FUNCTION ledger_messages_guard()"
        )
    op.execute(
        f"""
        CREATE FUNCTION ledger_contents_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF EXISTS (SELECT 1 FROM agent_message_redactions WHERE message_id = NEW.message_id) THEN
                    RAISE EXCEPTION 'forgotten content cannot be restored' USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' AND {MODE} IN ('redact', 'purge') THEN RETURN OLD; END IF;
            RAISE EXCEPTION 'message content is never edited; forget it instead' USING ERRCODE = 'check_violation';
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER agent_message_contents_guard BEFORE INSERT OR UPDATE OR DELETE ON agent_message_contents "
        "FOR EACH ROW EXECUTE FUNCTION ledger_contents_guard()"
    )
    op.execute(
        f"""
        CREATE FUNCTION agent_summaries_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF {MODE} = 'purge' THEN RETURN OLD; END IF;
                RAISE EXCEPTION 'summaries are never deleted, only invalidated' USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.invalidated_at IS NOT NULL OR NEW.invalidated_at IS NULL
               OR (to_jsonb(NEW) - 'invalidated_at') <> (to_jsonb(OLD) - 'invalidated_at') THEN
                RAISE EXCEPTION 'a summary can only be invalidated, once' USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END $$
        """
    )
    op.execute(
        "CREATE TRIGGER agent_summaries_guard BEFORE UPDATE OR DELETE ON agent_summaries "
        "FOR EACH ROW EXECUTE FUNCTION agent_summaries_guard()"
    )

    # run events (0006): forget may replace a payload with {"redacted": true}; purge may delete
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION agent_run_events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND {MODE} = 'purge' THEN RETURN OLD; END IF;
            IF TG_OP = 'UPDATE' AND {MODE} = 'redact' AND NEW.payload = '{{"redacted": true}}'::jsonb
               AND (to_jsonb(NEW) - 'payload') = (to_jsonb(OLD) - 'payload') THEN
                RETURN NEW;
            END IF;
            RAISE EXCEPTION 'agent_run_events is append-only' USING ERRCODE = 'check_violation';
        END $$
        """
    )
    # runs (0006): a terminal run may only have its text blanked (redact); runs are deleted only by purge
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION agent_runs_guard() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                IF {MODE} = 'purge' THEN RETURN OLD; END IF;
                RAISE EXCEPTION 'agent runs are deleted through the purge job only' USING ERRCODE = 'check_violation';
            END IF;
            IF {MODE} = 'redact' THEN
                IF (to_jsonb(NEW) - 'user_message' - 'output') = (to_jsonb(OLD) - 'user_message' - 'output')
                   AND (NEW.user_message = OLD.user_message OR NEW.user_message = '')
                   AND (NEW.output IS NOT DISTINCT FROM OLD.output OR NEW.output IS NULL) THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'redact may only blank run text' USING ERRCODE = 'check_violation';
            END IF;
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
    op.execute("DROP TRIGGER agent_runs_guard ON agent_runs")
    op.execute(
        "CREATE TRIGGER agent_runs_guard BEFORE UPDATE OR DELETE ON agent_runs "
        "FOR EACH ROW EXECUTE FUNCTION agent_runs_guard()"
    )
    # gateway audit (0009): purge removes a deleted session's tool calls too
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION gateway_invocations_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'DELETE' AND {MODE} = 'purge' THEN RETURN OLD; END IF;
            RAISE EXCEPTION 'gateway_tool_invocations is append-only' USING ERRCODE = 'check_violation';
        END $$
        """
    )

    # ---- backfill: earlier chats become ledger history, so nothing you said is lost when the assembler takes over --
    conn = op.get_bind()
    runs = conn.exec_driver_sql(
        "SELECT id, session_id, actor_id, executing_actor_id, user_message, output, state, queued_at, finished_at "
        "FROM agent_runs ORDER BY session_id, queued_at, id"
    ).fetchall()
    seq: dict[object, int] = {}
    for r in runs:
        (
            run_id,
            session_id,
            requester,
            executor,
            message,
            output,
            state,
            queued_at,
            finished_at,
        ) = r
        rows: list[tuple[Any, ...]] = [
            ("user", requester, message, "complete", queued_at, None, {})
        ]
        calls = conn.exec_driver_sql(
            "SELECT id, tool, args_redacted, decision, reason_code, created_at "
            "FROM gateway_tool_invocations WHERE run_id = %(r)s ORDER BY created_at, id",
            {"r": run_id},
        ).fetchall()
        for inv, tool, args, decision, reason, at in calls:
            rows.append(
                (
                    "tool_call",
                    executor,
                    tool,
                    "complete",
                    at,
                    inv,
                    {"tool": tool, "arguments": args, "historical": True},
                )
            )
            rows.append(
                (
                    "tool_result",
                    executor,
                    "Historical result body unavailable.",
                    "complete",
                    at,
                    inv,
                    {"tool": tool, "decision": decision, "reason": reason, "unavailable": True},
                )
            )
        if output:
            done = "complete" if state == "SUCCEEDED" else "unfinished"
            rows.append(("assistant", executor, output, done, finished_at or queued_at, None, {}))
        for kind, author, body, status, at, invocation, data in rows:
            seq[session_id] = seq.get(session_id, 0) + 1
            mid = conn.exec_driver_sql(
                "INSERT INTO agent_session_messages (session_id, seq, kind, author_actor_id, run_id, status, "
                "token_estimate, created_at, tool_invocation_id) "
                "VALUES (%(s)s, %(q)s, %(k)s, %(a)s, %(r)s, %(st)s, %(t)s, %(at)s, %(inv)s) "
                "RETURNING id",
                {"s": session_id, "q": seq[session_id], "k": kind, "a": author, "r": run_id, "st": status,
                 "t": max(1, -(-len(body) // 4)), "at": at, "inv": invocation},
            ).scalar()  # fmt: skip
            conn.exec_driver_sql(
                "INSERT INTO agent_message_contents (message_id, body, data) VALUES (%(m)s, %(b)s, %(d)s::jsonb)",
                {"m": mid, "b": body, "d": json.dumps(data)},
            )
    # sessions created before titles existed get one from their first message
    op.execute(
        """
        UPDATE agent_sessions s SET title = left(regexp_replace(c.body, '\\s+', ' ', 'g'), 60)
          FROM agent_session_messages m JOIN agent_message_contents c ON c.message_id = m.id
         WHERE m.session_id = s.id AND m.seq = 1 AND s.title = ''
        """
    )


def downgrade() -> None:
    # restore the 0006 / 0009 trigger functions
    op.execute(
        """
        CREATE OR REPLACE FUNCTION gateway_invocations_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'gateway_tool_invocations is append-only' USING ERRCODE = 'check_violation';
        END $$
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION agent_run_events_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'agent_run_events is append-only' USING ERRCODE = 'check_violation';
        END $$
        """
    )
    op.execute("DROP TRIGGER agent_runs_guard ON agent_runs")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION agent_runs_guard() RETURNS trigger LANGUAGE plpgsql AS $$
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
        "CREATE TRIGGER agent_runs_guard BEFORE UPDATE ON agent_runs FOR EACH ROW EXECUTE FUNCTION agent_runs_guard()"
    )
    for t in ("data_deletions", "agent_summaries", "agent_message_redactions", "agent_message_contents",
              "agent_session_messages"):  # fmt: skip
        op.execute(f"DROP TABLE {t}")
    for f in ("agent_summaries_guard", "ledger_contents_guard", "ledger_messages_guard"):
        op.execute(f"DROP FUNCTION {f}()")
    op.execute("DROP TRIGGER agent_sessions_guard ON agent_sessions")
    op.execute("DROP TRIGGER agent_sessions_root ON agent_sessions")
    op.execute("DROP FUNCTION agent_sessions_guard()")
    op.execute("DROP FUNCTION agent_sessions_root()")
    op.execute("DROP INDEX agent_sessions_list")
    op.execute("DROP INDEX agent_sessions_root_idx")
    op.execute(
        """
        ALTER TABLE agent_sessions
            DROP CONSTRAINT agent_sessions_fork_pair, DROP CONSTRAINT agent_sessions_kind_lineage,
            DROP CONSTRAINT agent_sessions_fork_or_delegation, DROP CONSTRAINT agent_sessions_delete_pair,
            DROP CONSTRAINT agent_sessions_not_own_parent,
            DROP COLUMN requester_actor_id, DROP COLUMN agent_actor_id, DROP COLUMN kind,
            DROP COLUMN forked_from_session_id, DROP COLUMN fork_seq, DROP COLUMN delegated_from_session_id,
            DROP COLUMN delegated_from_run_id, DROP COLUMN delegated_from_invocation_id, DROP COLUMN root_session_id,
            DROP COLUMN title_source, DROP COLUMN last_activity_at, DROP COLUMN deleted_at, DROP COLUMN purge_after
        """
    )
    op.execute("DELETE FROM actors WHERE id = 'system'")
    op.execute("ALTER TABLE actors DROP CONSTRAINT actors_kind_check")
    op.execute(
        "ALTER TABLE actors ADD CONSTRAINT actors_kind_check CHECK (kind IN ('human', 'agent'))"
    )
