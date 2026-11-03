"""002C schema: actors, immutable profile versions, one active version per actor, immutable compiled output."""

import time

import psycopg
import pytest
from alembic import command
from psycopg.types.json import Jsonb

from paw_server.db.migrations_info import alembic_config

pytestmark = pytest.mark.integration


@pytest.fixture
def conn(db_url):
    command.upgrade(alembic_config(db_url), "head")
    with psycopg.connect(db_url, autocommit=True) as c:
        yield c


def _version(c, version=1, actor="admin-hermes", spec=None, status="draft") -> str:
    act = "now()" if status != "draft" else "NULL"
    return str(
        c.execute(
            f"INSERT INTO actor_profile_versions (actor_id, version, spec, spec_hash, status, created_by, activated_at) "
            f"VALUES (%s, %s, %s, 'h', %s, 'owner', {act}) RETURNING id",
            (actor, version, Jsonb(spec or {"persona": {"title": "x"}}), status),
        ).fetchone()[0]
    )


def _set(c, vid, status):
    act = ", activated_at = now()" if status == "active" else ""
    c.execute(f"UPDATE actor_profile_versions SET status=%s{act} WHERE id=%s", (status, vid))


def test_seeded_actors(conn):
    rows = conn.execute("SELECT id, kind, display_name FROM actors ORDER BY id").fetchall()
    assert rows == [("admin-hermes", "agent", "Admin Hermès"), ("owner", "human", "Bharat")]


def test_actor_kind_and_status_are_constrained(conn):
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("INSERT INTO actors (id, kind, display_name) VALUES ('x', 'robot', 'X')")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE actors SET status = 'gone' WHERE id = 'owner'")


def test_actor_updated_at_moves_on_change_for_the_story3_feed(conn):
    before = conn.execute("SELECT updated_at FROM actors WHERE id='owner'").fetchone()[0]
    time.sleep(0.05)
    conn.execute("UPDATE actors SET display_name = 'Bharat P' WHERE id='owner'")
    assert conn.execute("SELECT updated_at FROM actors WHERE id='owner'").fetchone()[0] > before


def test_downgrade_and_upgrade_roundtrip(db_url):
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0007")
    command.upgrade(cfg, "head")


def test_a_version_needs_a_real_actor_and_creator(conn):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _version(conn, actor="nobody")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "INSERT INTO actor_profile_versions (actor_id, version, spec, spec_hash, created_by) "
            "VALUES ('admin-hermes', 1, '{}', 'h', 'ghost')"
        )


def test_version_numbers_are_unique_per_actor(conn):
    _version(conn, 1)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _version(conn, 1)
    _version(conn, 1, actor="owner")  # another actor has its own numbering


def test_only_one_active_version_per_actor(conn):
    a, b = _version(conn, 1), _version(conn, 2)
    _set(conn, a, "active")
    with pytest.raises(psycopg.errors.UniqueViolation):
        _set(conn, b, "active")
    _set(conn, a, "retired")  # retire first, then the next can be activated
    _set(conn, b, "active")
    assert conn.execute(
        "SELECT count(*) FROM actor_profile_versions WHERE status='active'"
    ).fetchone() == (1,)


def test_each_actor_may_have_its_own_active_version(conn):
    _set(conn, _version(conn, 1), "active")
    _set(conn, _version(conn, 1, actor="owner"), "active")


@pytest.mark.parametrize(
    "assignment",
    ["spec = '{\"persona\": {\"title\": \"changed\"}}'", "spec_hash = 'other'", "version = 9",
     "actor_id = 'owner'", "created_by = 'admin-hermes'", "created_at = now() - interval '1 day'"],
)  # fmt: skip
def test_a_created_version_is_immutable(conn, assignment):
    v = _version(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(f"UPDATE actor_profile_versions SET {assignment} WHERE id=%s", (v,))
    v2 = _version(conn, 2)
    _set(conn, v2, "active")
    with pytest.raises(psycopg.errors.CheckViolation):  # also once active
        conn.execute(f"UPDATE actor_profile_versions SET {assignment} WHERE id=%s", (v2,))


def test_versions_cannot_be_deleted(conn):
    v = _version(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("DELETE FROM actor_profile_versions WHERE id=%s", (v,))


@pytest.mark.parametrize(("src", "dst", "ok"), [
    ("draft", "active", True), ("draft", "retired", True), ("active", "retired", True),
    ("active", "draft", False), ("retired", "active", False), ("retired", "draft", False),
])  # fmt: skip
def test_status_only_moves_forward(conn, src, dst, ok):
    v = _version(conn)
    if src != "draft":
        _set(conn, v, "active")
    if src == "retired":
        _set(conn, v, "retired")
    if ok:
        _set(conn, v, dst)
        assert conn.execute(
            "SELECT status FROM actor_profile_versions WHERE id=%s", (v,)
        ).fetchone() == (dst,)
    else:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE actor_profile_versions SET status=%s WHERE id=%s", (dst, v))


def test_activated_at_must_match_status_and_cannot_be_rewritten(conn):
    with pytest.raises(psycopg.errors.CheckViolation):  # active without an activation time
        conn.execute(
            "INSERT INTO actor_profile_versions (actor_id, version, spec, spec_hash, status, created_by) "
            "VALUES ('admin-hermes', 1, '{}', 'h', 'active', 'owner')"
        )
    v = _version(conn, 2)
    _set(conn, v, "active")
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "UPDATE actor_profile_versions SET activated_at = now() + interval '1 day' WHERE id=%s",
            (v,),
        )


def _compiled(c, vid) -> None:
    c.execute(
        "INSERT INTO actor_compiled_profiles (profile_version_id, instructions_text, config_fragment, "
        "constraints_text, policy, compiled_hash, compiler_version) VALUES (%s, 'i', '{}', 'c', '{}', 'h', '1')",
        (vid,),
    )


def test_compiled_output_is_one_per_version_and_immutable(conn):
    v = _version(conn)
    _compiled(conn, v)
    with pytest.raises(psycopg.errors.UniqueViolation):
        _compiled(conn, v)
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "UPDATE actor_compiled_profiles SET compiled_hash='x' WHERE profile_version_id=%s", (v,)
        )
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("DELETE FROM actor_compiled_profiles WHERE profile_version_id=%s", (v,))
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _compiled(conn, "00000000-0000-0000-0000-000000000000")


def test_runs_and_runtimes_carry_the_executing_actor(conn):
    ws = conn.execute("SELECT id FROM workspaces LIMIT 1").fetchone()[0]

    def new_session():
        return conn.execute(
            "INSERT INTO agent_sessions (owner, workspace_id) VALUES ('owner', %s) RETURNING id",
            (ws,),
        ).fetchone()[0]

    s = new_session()
    run = conn.execute(
        "INSERT INTO agent_runs (session_id, actor_id, client_request_id, user_message) VALUES (%s,'owner','a','x') "
        "RETURNING executing_actor_id, profile_version_id",
        (s,),
    ).fetchone()
    assert run == ("admin-hermes", None)
    assert conn.execute(
        "INSERT INTO agent_runtimes (name) VALUES ('r') RETURNING actor_id"
    ).fetchone() == ("admin-hermes",)
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "INSERT INTO agent_runs (session_id, actor_id, client_request_id, user_message, executing_actor_id) "
            "VALUES (%s,'owner','b','x','ghost')", (new_session(),))  # fmt: skip
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        conn.execute(
            "INSERT INTO agent_runs (session_id, actor_id, client_request_id, user_message) VALUES (%s,'ghost','c','x')",
            (new_session(),),
        )


def test_a_run_can_point_at_its_profile_version_and_it_survives_status_changes(conn):
    v = _version(conn)
    _set(conn, v, "active")
    ws = conn.execute("SELECT id FROM workspaces LIMIT 1").fetchone()[0]
    s = conn.execute(
        "INSERT INTO agent_sessions (owner, workspace_id) VALUES ('owner', %s) RETURNING id", (ws,)
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO agent_runs (session_id, actor_id, client_request_id, user_message, profile_version_id) "
        "VALUES (%s,'owner','a','x',%s)", (s, v))  # fmt: skip
    _set(
        conn, v, "retired"
    )  # the next version is activated later; this run still points at what it used
    assert conn.execute(
        "SELECT status FROM actor_profile_versions WHERE id=%s", (v,)
    ).fetchone() == ("retired",)
