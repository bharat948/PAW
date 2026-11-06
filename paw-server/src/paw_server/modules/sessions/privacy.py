"""Owner-controlled forks, redaction and deletion with durable replay receipts."""

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from paw_server.modules.ingestion.jobs import Conn
from paw_server.modules.sessions import service as ledger
from paw_server.modules.sessions.journal import Journal, Receipt

ACTIVE = ("QUEUED", "DISPATCHING", "RUNNING", "CANCELLING")


def descendants(conn: Conn, session_id: UUID, *, forks: bool = False) -> list[UUID]:
    if forks:
        query = "s.forked_from_session_id = t.id OR s.delegated_from_session_id = t.id"
    else:
        query = "s.delegated_from_session_id = t.id"
    return [
        r["id"]
        for r in conn.execute(
            "WITH RECURSIVE tree AS (SELECT id FROM agent_sessions WHERE id=%s UNION "
            f"SELECT s.id FROM agent_sessions s JOIN tree t ON {query}) SELECT id FROM tree ORDER BY id",
            (session_id,),
        ).fetchall()
    ]


def fork(
    conn: Conn, session_id: UUID, message_id: UUID, owner: str, workspace_id: UUID
) -> dict[str, Any]:
    parent = ledger.lock(conn, session_id)
    if parent["owner"] != owner or parent["workspace_id"] != workspace_id or parent["deleted_at"]:
        raise ValueError("session_missing")
    messages = ledger.messages(conn, session_id)
    pivot = next(
        (
            r
            for r in messages
            if r["id"] == message_id and r["kind"] == "user" and not r["forgotten"]
        ),
        None,
    )
    if pivot is None:
        raise ValueError("message_missing")
    row = conn.execute(
        "INSERT INTO agent_sessions (owner,workspace_id,requester_actor_id,agent_actor_id,title,title_source,forked_from_session_id,fork_seq) "
        "VALUES (%s,%s,%s,%s,%s,'user',%s,%s) RETURNING *",
        (
            owner,
            workspace_id,
            parent["requester_actor_id"],
            parent["agent_actor_id"],
            parent["title"] + " (fork)",
            session_id,
            pivot["seq"] - 1,
        ),
    ).fetchone()
    assert row is not None
    # A fork boundary exists in metadata; recording an extra message would precede the replacement input.
    return row


def _redact_ids(conn: Conn, ids: list[UUID], owner: str) -> None:
    if not ids:
        return
    conn.execute("DELETE FROM agent_message_contents WHERE message_id=ANY(%s)", (ids,))
    conn.execute(
        "INSERT INTO agent_message_redactions (message_id,redacted_by) SELECT id,%s FROM agent_session_messages "
        "WHERE id=ANY(%s) ON CONFLICT DO NOTHING",
        (owner, ids),
    )


def _hide_active(conn: Conn, session_ids: list[UUID]) -> list[str]:
    from paw_server.modules.runtime import lifecycle as lc

    runtime_ids = []
    for r in conn.execute(
        "SELECT * FROM agent_runs WHERE session_id=ANY(%s) AND state=ANY(%s) ORDER BY id FOR UPDATE",
        (session_ids, list(ACTIVE)),
    ).fetchall():
        conn.execute(
            "INSERT INTO agent_run_redactions (run_id) VALUES (%s) ON CONFLICT DO NOTHING",
            (r["id"],),
        )
        if r["state"] == "QUEUED":
            lc.finish(conn, r["id"], "CANCELLED")
        elif r["state"] == "RUNNING":
            conn.execute(
                "UPDATE agent_runs SET state='CANCELLING',cancel_requested_at=now() WHERE id=%s",
                (r["id"],),
            )
            lc._insert_event(conn, r["id"], "run.state", {"state": "CANCELLING"}, None)
        if r["hermes_run_id"]:
            runtime_ids.append(str(r["id"]))
    return runtime_ids


def apply(conn: Conn, receipt: Receipt, *, force: bool = False) -> list[str]:
    """Replay is idempotent. The receipt outlives its target rows and every database backup."""
    if (
        not force
        and conn.execute("SELECT 1 FROM data_deletions WHERE id=%s", (receipt.id,)).fetchone()
    ):
        return []
    target = conn.execute(
        "SELECT * FROM agent_sessions WHERE id=%s", (receipt.session_id,)
    ).fetchone()
    # Original session may already be purged; materialized fork copies still carry its source IDs.
    if target and (
        target["owner"] != receipt.requested_by or target["workspace_id"] != receipt.workspace_id
    ):
        raise ValueError("journal_scope_mismatch")
    if receipt.scope == "session":
        sessions = (
            descendants(conn, receipt.session_id)
            if receipt.include_delegated
            else [receipt.session_id]
        )
    else:
        origins = conn.execute(
            "SELECT m.id,m.session_id,m.run_id,coalesce(m.source_message_id,m.id) AS source_id FROM agent_session_messages m "
            "JOIN agent_sessions s ON s.id=m.session_id WHERE s.owner=%s AND s.workspace_id=%s AND (m.id=%s OR m.source_message_id=%s)",
            (receipt.requested_by, receipt.workspace_id, receipt.message_id, receipt.message_id),
        ).fetchall()
        source_ids = list({r["source_id"] for r in origins} | {receipt.message_id})
        # Remove known copies in the same run as well: run text, deltas, tool arguments and results.
        runs = list({r["run_id"] for r in origins if r["run_id"]} | set(receipt.run_ids))
        source_runs = [str(run_id) for run_id in runs]
        runs = [
            r["id"]
            for r in conn.execute(
                "SELECT r.id FROM agent_runs r JOIN agent_sessions s ON s.id=r.session_id WHERE r.id=ANY(%s) AND s.owner=%s AND s.workspace_id=%s",
                (runs, receipt.requested_by, receipt.workspace_id),
            ).fetchall()
        ]
        affected = conn.execute(
            "SELECT m.id,m.session_id,m.run_id,coalesce(m.source_message_id,m.id) AS source_id "
            "FROM agent_session_messages m JOIN agent_sessions s ON s.id=m.session_id "
            "LEFT JOIN agent_message_contents c ON c.message_id=m.id "
            "WHERE s.owner=%s AND s.workspace_id=%s AND "
            "(m.id=ANY(%s) OR m.source_message_id=ANY(%s) OR m.run_id=ANY(%s) OR c.data->>'source_run_id'=ANY(%s))",
            (receipt.requested_by, receipt.workspace_id, source_ids, source_ids, runs, source_runs),
        ).fetchall()
        source_ids = list({r["source_id"] for r in affected} | set(source_ids))
        affected = conn.execute(
            "SELECT m.id,m.session_id,m.run_id FROM agent_session_messages m JOIN agent_sessions s ON s.id=m.session_id "
            "WHERE s.owner=%s AND s.workspace_id=%s AND (m.id=ANY(%s) OR m.source_message_id=ANY(%s) OR m.run_id=ANY(%s))",
            (receipt.requested_by, receipt.workspace_id, source_ids, source_ids, runs),
        ).fetchall()
        sessions = sorted({r["session_id"] for r in affected}, key=str)
        # Referencing forks must have their summaries and running context invalidated too.
        for sid in list(sessions):
            sessions = list(set(sessions) | set(descendants(conn, sid, forks=True)))
    sessions = sorted(set(sessions), key=str)
    existing = conn.execute(
        "SELECT * FROM agent_sessions WHERE id=ANY(%s) AND owner=%s AND workspace_id=%s ORDER BY id FOR UPDATE",
        (sessions, receipt.requested_by, receipt.workspace_id),
    ).fetchall()
    sessions = [s["id"] for s in existing]
    runtime_ids = _hide_active(conn, sessions)
    conn.execute("SET LOCAL paw.maintenance = 'redact'")
    if receipt.scope == "message":
        _redact_ids(conn, [r["id"] for r in affected], receipt.requested_by)
        runs = [
            r["id"]
            for r in conn.execute("SELECT id FROM agent_runs WHERE id=ANY(%s)", (runs,)).fetchall()
        ]
        for run in runs:
            conn.execute(
                "INSERT INTO agent_run_redactions (run_id) VALUES (%s) ON CONFLICT DO NOTHING",
                (run,),
            )
            conn.execute("UPDATE agent_runs SET user_message='',output=NULL WHERE id=%s", (run,))
            conn.execute(
                "UPDATE agent_run_events SET payload='{"
                + '"redacted": true'
                + "}'::jsonb WHERE run_id=%s",
                (run,),
            )
            conn.execute(
                "UPDATE gateway_tool_invocations SET args_redacted='{}'::jsonb,error=NULL WHERE run_id=%s",
                (run,),
            )
        # A first-message title is a known copy too; custom titles are kept only when unrelated.
        conn.execute("UPDATE agent_sessions SET title='Conversation' WHERE id=ANY(%s)", (sessions,))
    else:
        conn.execute(
            "UPDATE agent_sessions SET deleted_at=%s,purge_after=%s::timestamptz + interval '30 days' "
            "WHERE id=ANY(%s) AND deleted_at IS NULL",
            (receipt.requested_at, receipt.requested_at, sessions),
        )
    summaries = conn.execute(
        "SELECT message_id FROM agent_summaries WHERE session_id=ANY(%s) AND invalidated_at IS NULL",
        (sessions,),
    ).fetchall()
    _redact_ids(conn, [s["message_id"] for s in summaries], receipt.requested_by)
    conn.execute(
        "UPDATE agent_summaries SET invalidated_at=now() WHERE session_id=ANY(%s) AND invalidated_at IS NULL",
        (sessions,),
    )
    conn.execute(
        "UPDATE approval_operations SET result='{}'::jsonb WHERE approval_id IN (SELECT id FROM approval_requests WHERE session_id=ANY(%s))",
        (sessions,),
    )
    conn.execute(
        "UPDATE approval_requests SET args='{}'::jsonb,state=CASE WHEN state='PENDING' THEN 'INVALIDATED' ELSE state END WHERE session_id=ANY(%s)",
        (sessions,),
    )
    conn.execute(
        "UPDATE agent_sessions SET context_revision=context_revision+1 WHERE id=ANY(%s)",
        (sessions,),
    )
    conn.execute(
        "INSERT INTO data_deletions (id,scope,session_id,message_id,include_delegated,requested_by,requested_at,purge_after) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,CASE WHEN %s='session' THEN %s::timestamptz + interval '30 days' ELSE NULL END) ON CONFLICT DO NOTHING",
        (
            receipt.id,
            receipt.scope,
            receipt.session_id,
            receipt.message_id,
            receipt.include_delegated,
            receipt.requested_by,
            receipt.requested_at,
            receipt.scope,
            receipt.requested_at,
        ),
    )
    conn.execute("SET LOCAL paw.maintenance = ''")
    return runtime_ids


def request(
    conn: Conn,
    journal: Journal,
    *,
    session_id: UUID,
    owner: str,
    workspace_id: UUID,
    message_id: UUID | None = None,
    include_delegated: bool = False,
) -> tuple[Receipt, list[str]]:
    with journal.guard(), conn.transaction():
        conn.execute("SELECT id FROM agent_sessions ORDER BY id FOR UPDATE").fetchall()
        s = ledger.lock(conn, session_id)
        if s["owner"] != owner or s["workspace_id"] != workspace_id or s["deleted_at"]:
            raise ValueError("session_missing")
        source_id = message_id
        run_ids = []
        if message_id is not None:
            msg = next(
                (m for m in ledger.messages(conn, session_id) if m["id"] == message_id), None
            )
            if msg is None:
                raise ValueError("message_missing")
            source_id = msg.get("source_message_id") or msg["id"]
            source_run = msg.get("run_id") or (msg.get("data") or {}).get("source_run_id")
            if source_run:
                run_ids = [UUID(str(source_run))]
        receipt = Receipt(
            id=uuid4(),
            scope="message" if message_id else "session",
            session_id=session_id,
            workspace_id=workspace_id,
            message_id=source_id,
            run_ids=run_ids,
            requested_by=owner,
            requested_at=datetime.now(UTC),
            include_delegated=include_delegated,
        )
        journal.write(
            receipt
        )  # durable before DB commit; a crash replays it before the next read/dispatch
        runtime_ids = apply(conn, receipt)
    return receipt, runtime_ids


def replay(conn: Conn, journal: Journal, *, force: bool = False) -> int:
    if not journal.path.exists():
        return 0
    count = 0
    with journal.guard(), conn.transaction():
        conn.execute("SELECT id FROM agent_sessions ORDER BY id FOR UPDATE").fetchall()
        for receipt in journal.receipts():
            if (
                force
                or not conn.execute(
                    "SELECT 1 FROM data_deletions WHERE id=%s", (receipt.id,)
                ).fetchone()
            ):
                apply(conn, receipt, force=force)
                count += 1
    return count


def materialize(conn: Conn, child: dict[str, Any]) -> None:
    if child["forked_from_session_id"] is not None:
        prefix = ledger.messages(conn, child["forked_from_session_id"], through=child["fork_seq"])
        for row in prefix:
            mid = uuid4()
            conn.execute(
                "INSERT INTO agent_session_messages (id,session_id,seq,kind,author_actor_id,status,token_estimate,created_at,source_message_id,source_session_id) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    mid,
                    child["id"],
                    row["seq"],
                    row["kind"],
                    row["author_actor_id"],
                    row["status"],
                    row["token_estimate"],
                    row["created_at"],
                    row.get("source_message_id") or row["id"],
                    row.get("source_session_id") or row["session_id"],
                ),
            )
            if row["forgotten"] or row["body"] is None:
                conn.execute(
                    "INSERT INTO agent_message_redactions (message_id,redacted_by) VALUES (%s,%s)",
                    (mid, child["owner"]),
                )
            else:
                conn.execute(
                    "INSERT INTO agent_message_contents (message_id,body,data) VALUES (%s,%s,%s)",
                    (
                        mid,
                        row["body"],
                        Jsonb(
                            {
                                **(row["data"] or {}),
                                "source_run_id": str(row["run_id"])
                                if row["run_id"]
                                else (row["data"] or {}).get("source_run_id"),
                            }
                        ),
                    ),
                )
    conn.execute(
        "UPDATE agent_sessions SET forked_from_session_id=NULL,fork_seq=NULL,delegated_from_session_id=NULL,"
        "delegated_from_run_id=NULL,delegated_from_invocation_id=NULL,kind='chat',root_session_id=id WHERE id=%s",
        (child["id"],),
    )


def purge_due(conn: Conn) -> int:
    with conn.transaction():
        conn.execute("SELECT id FROM agent_sessions ORDER BY id FOR UPDATE").fetchall()
        due = conn.execute(
            "SELECT * FROM agent_sessions s WHERE purge_after <= now() AND NOT EXISTS "
            "(SELECT 1 FROM agent_runs r WHERE r.session_id=s.id AND r.state=ANY(%s)) ORDER BY id FOR UPDATE",
            (list(ACTIVE),),
        ).fetchall()
        if not due:
            return 0
        # Lock all remaining sessions before run/event locks, including retained descendants.
        conn.execute("SELECT id FROM agent_sessions ORDER BY id FOR UPDATE").fetchall()
        conn.execute("SET LOCAL paw.maintenance = 'purge'")
        ids = [r["id"] for r in due]
        for child in conn.execute(
            "SELECT * FROM agent_sessions WHERE forked_from_session_id=ANY(%s) OR delegated_from_session_id=ANY(%s) OR root_session_id=ANY(%s) ORDER BY id",
            (ids, ids, ids),
        ).fetchall():
            if child["id"] not in ids:
                materialize(conn, child)
        # Detach due children as well; FK order must not leave cyclic root references.
        conn.execute(
            "UPDATE agent_sessions SET forked_from_session_id=NULL,fork_seq=NULL,delegated_from_session_id=NULL,"
            "delegated_from_run_id=NULL,delegated_from_invocation_id=NULL,kind='chat',root_session_id=id WHERE id=ANY(%s)",
            (ids,),
        )
        mids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM agent_session_messages WHERE session_id=ANY(%s)", (ids,)
            ).fetchall()
        ]
        runs = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM agent_runs WHERE session_id=ANY(%s)", (ids,)
            ).fetchall()
        ]
        conn.execute("DELETE FROM agent_message_contents WHERE message_id=ANY(%s)", (mids,))
        conn.execute("DELETE FROM agent_message_redactions WHERE message_id=ANY(%s)", (mids,))
        conn.execute("DELETE FROM agent_summaries WHERE session_id=ANY(%s)", (ids,))
        conn.execute("DELETE FROM agent_session_messages WHERE session_id=ANY(%s)", (ids,))
        conn.execute("DELETE FROM agent_summary_usage WHERE session_id=ANY(%s)", (ids,))
        conn.execute("DELETE FROM agent_session_jobs WHERE session_id=ANY(%s)", (ids,))
        conn.execute(
            "DELETE FROM runtime_reports report USING agent_runs r, agent_runtimes rt "
            "WHERE r.id=ANY(%s) AND rt.id=r.runtime_id AND report.runtime_name=rt.name "
            "AND (report.hermes_run_id=r.hermes_run_id OR report.hermes_run_id=r.id::text)",
            (runs,),
        )
        conn.execute("DELETE FROM agent_run_redactions WHERE run_id=ANY(%s)", (runs,))
        conn.execute("DELETE FROM gateway_tool_invocations WHERE run_id=ANY(%s)", (runs,))
        conn.execute("DELETE FROM gateway_tool_attempts WHERE run_id=ANY(%s)", (runs,))
        conn.execute("DELETE FROM agent_run_events WHERE run_id=ANY(%s)", (runs,))
        conn.execute("DELETE FROM agent_run_leases WHERE run_id=ANY(%s)", (runs,))
        conn.execute("DELETE FROM agent_runs WHERE session_id=ANY(%s)", (ids,))
        conn.execute("DELETE FROM agent_sessions WHERE id=ANY(%s)", (ids,))
        conn.execute(
            "UPDATE data_deletions SET purged_at=now() WHERE scope='session' AND session_id=ANY(%s)",
            (ids,),
        )
        return len(ids)
