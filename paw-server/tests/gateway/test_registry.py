"""Tool registry rules (002D-R04) and its PostgreSQL mirror and audit table."""

import copy

import psycopg
import pytest
from alembic import command

from paw_server.db.migrations_info import alembic_config
from paw_server.db.pool import create_pool
from paw_server.modules.gateway.builtin import builtin_registry, builtin_specs
from paw_server.modules.gateway.registry import (
    FORBIDDEN_ARGS,
    RegistryError,
    ToolRegistry,
    ToolSpec,
    enabled_tools,
    sync_registry,
    validate_arguments,
)

OBJ = {"type": "object", "properties": {"q": {"type": "string"}}, "additionalProperties": False}


def spec(**kw) -> ToolSpec:
    return ToolSpec(**{"name": "my_tool", "description": "d", "input_schema": OBJ, **kw})


# ---- registration rules ------------------------------------------------------------------------------------------


def test_the_builtin_registry_is_valid_and_complete():
    r = builtin_registry()
    assert r.names() == [
        "calendar_list_events",
        "docs_get_source",
        "docs_search",
        "paw_run_status",
        "paw_system_status",
    ]
    assert all(s.side_effect_class == "read" and not s.approval_required for s in builtin_specs())
    assert r.get("calendar_list_events").provider == "google_calendar"


@pytest.mark.parametrize("name", ["", "A", "ab", "Bad-Name", "1abc", "x" * 65, "has space"])
def test_bad_names_are_refused(name):
    with pytest.raises(RegistryError):
        spec(name=name)


def test_schema_must_be_a_closed_object():
    for schema in ({"type": "object", "properties": {}}, {"type": "object", "properties": {}, "additionalProperties": True},
                   {"type": "string"}, {"properties": {}, "additionalProperties": False}):  # fmt: skip
        with pytest.raises(RegistryError):
            spec(input_schema=schema)
    with pytest.raises(RegistryError):
        spec(
            input_schema={
                "type": "object",
                "properties": {"x": {"type": "nonsense"}},
                "additionalProperties": False,
            }
        )


@pytest.mark.parametrize("field", sorted(FORBIDDEN_ARGS))
def test_identity_like_argument_names_cannot_be_declared(field):
    schema = {
        "type": "object",
        "properties": {field: {"type": "string"}},
        "additionalProperties": False,
    }
    with pytest.raises(RegistryError, match="identity-like"):
        spec(input_schema=schema)


def test_identity_names_are_caught_when_nested_or_differently_cased():
    nested = {"type": "object", "additionalProperties": False, "properties": {
        "filter": {"type": "object", "additionalProperties": False, "properties": {"Workspace_ID": {"type": "string"}}}}}  # fmt: skip
    with pytest.raises(RegistryError):
        spec(input_schema=nested)


def test_external_writes_must_require_approval():
    with pytest.raises(RegistryError):
        spec(side_effect_class="external_write")
    assert spec(side_effect_class="external_write", approval_required=True).approval_required
    assert spec(
        side_effect_class="internal_write", approval_required=True
    )  # approval flag is independent of class


def test_other_field_rules():
    with pytest.raises(RegistryError):
        spec(side_effect_class="admin")
    with pytest.raises(RegistryError):
        spec(version=0)
    with pytest.raises(RegistryError):
        spec(description=" ")


def test_duplicate_registration_is_refused():
    r = ToolRegistry([spec()])
    with pytest.raises(RegistryError):
        r.register(spec())
    assert "my_tool" in r and len(r) == 1 and r.get("nope") is None


# ---- argument validation (identity never comes from the model) ---------------------------------------------------


@pytest.fixture
def docs_search():
    return builtin_registry().get("docs_search")


def test_valid_arguments_pass(docs_search):
    assert validate_arguments(docs_search, {"query": "invoices"}) == []
    assert validate_arguments(docs_search, {"query": "invoices", "top_k": 10}) == []


@pytest.mark.parametrize(
    "args",
    [{}, {"query": ""}, {"query": "x" * 501}, {"query": 5}, {"query": "ok", "top_k": 0}, {"query": "ok", "top_k": 11},
     {"query": "ok", "top_k": "3"}, {"query": "ok", "top_k": 2.5}, {"query": "ok", "extra": 1}, "a string", ["list"], None],
)  # fmt: skip
def test_invalid_arguments_are_rejected(docs_search, args):
    assert validate_arguments(docs_search, args)


@pytest.mark.parametrize(
    "field", ["workspace_id", "actor_id", "run_id", "owner", "Session_ID", "requester"]
)
def test_forged_identity_arguments_are_rejected_by_name(docs_search, field):
    problems = validate_arguments(docs_search, {"query": "x", field: "someone-else"})
    assert any("forbidden identity" in p for p in problems)


def test_problem_messages_never_echo_argument_values(docs_search):
    secret = "SECRET-VALUE-123"
    problems = validate_arguments(
        docs_search, {"query": 5, "top_k": secret, "workspace_id": secret, "x": secret}
    )
    assert problems and secret not in " ".join(problems)


def test_no_argument_tools_reject_any_argument():
    t = builtin_registry().get("paw_system_status")
    assert validate_arguments(t, {}) == []
    assert validate_arguments(t, {"verbose": True})


def test_calendar_bounds():
    t = builtin_registry().get("calendar_list_events")
    assert (
        validate_arguments(t, {}) == [] and validate_arguments(t, {"days": 31, "limit": 50}) == []
    )
    for bad in ({"days": 0}, {"days": 32}, {"limit": 51}, {"days": "7"}):
        assert validate_arguments(t, bad)


def test_chunk_id_must_look_like_an_id():
    t = builtin_registry().get("docs_get_source")
    assert validate_arguments(t, {"chunk_id": "123e4567-e89b-12d3-a456-426614174000"}) == []
    for bad in ("", "../../etc/passwd", "x" * 36, "short"):
        assert validate_arguments(t, {"chunk_id": bad})


# ---- the PostgreSQL mirror and audit table -----------------------------------------------------------------------


@pytest.fixture
def conn(db_url):
    command.upgrade(alembic_config(db_url), "head")
    with psycopg.connect(db_url, autocommit=True) as c:
        yield c


@pytest.fixture
def pool(db_url):
    from paw_server.core.config import Settings

    command.upgrade(alembic_config(db_url), "head")
    p = create_pool(Settings(database_url=db_url, owner_token="t" * 20, env="ci"))  # type: ignore[arg-type]
    p.open()
    yield p
    p.close()


@pytest.mark.integration
def test_sync_writes_every_tool_and_is_idempotent(pool, conn):
    reg = builtin_registry()
    with pool.connection() as c:
        sync_registry(c, reg)
        sync_registry(c, reg)
        assert enabled_tools(c) == set(reg.names())
    rows = conn.execute(
        "SELECT name, side_effect_class, provider FROM gateway_tools ORDER BY name"
    ).fetchall()
    assert len(rows) == 5 and ("calendar_list_events", "read", "google_calendar") in rows


@pytest.mark.integration
def test_sync_updates_a_changed_schema_but_never_overrides_the_owners_switch(pool, conn):
    reg = builtin_registry()
    with pool.connection() as c:
        sync_registry(c, reg)
    conn.execute("UPDATE gateway_tools SET enabled = false WHERE name = 'docs_search'")
    changed = ToolRegistry(
        [
            s
            if s.name != "docs_search"
            else ToolSpec(**{**s.__dict__, "version": 2, "description": "new text"})
            for s in builtin_specs()
        ]
    )
    with pool.connection() as c:
        sync_registry(c, changed)
        assert "docs_search" not in enabled_tools(c)  # still off
    assert conn.execute(
        "SELECT version, description FROM gateway_tools WHERE name='docs_search'"
    ).fetchone() == (2, "new text")


@pytest.mark.integration
def test_tools_removed_from_code_are_disabled_not_deleted(pool, conn):
    with pool.connection() as c:
        sync_registry(c, builtin_registry())
        sync_registry(c, ToolRegistry([builtin_registry().get("docs_search")]))
        assert enabled_tools(c) == {"docs_search"}
    assert conn.execute("SELECT count(*) FROM gateway_tools").fetchone() == (5,)


@pytest.mark.integration
def test_registry_table_constraints(conn):
    ins = "INSERT INTO gateway_tools (name, version, side_effect_class, approval_required, description, schema_json) VALUES (%s,%s,%s,%s,'d','{}')"
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(ins, ("some_tool", 1, "external_write", False))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(ins, ("some_tool", 1, "root", False))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(ins, ("BadName", 1, "read", False))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(ins, ("some_tool", 0, "read", False))


def _invocation(conn, **kw) -> str:
    cols = {
        "tool": "docs_search",
        "decision": "denied",
        "reason_code": "NOT_GRANTED",
        "trace_id": "t1",
        **kw,
    }
    names = ", ".join(cols)
    ph = ", ".join(["%s"] * len(cols))
    return str(
        conn.execute(
            f"INSERT INTO gateway_tool_invocations ({names}) VALUES ({ph}) RETURNING id",
            list(cols.values()),
        ).fetchone()[0]
    )


@pytest.mark.integration
def test_invocations_record_denials_even_without_a_run_and_are_append_only(conn):
    i = _invocation(conn, reason_code="NO_ACTIVE_RUN")  # no run, no actor: still audited
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("UPDATE gateway_tool_invocations SET reason_code='OK' WHERE id=%s", (i,))
    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute("DELETE FROM gateway_tool_invocations WHERE id=%s", (i,))
    with pytest.raises(psycopg.errors.CheckViolation):
        _invocation(conn, decision="maybe")
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        _invocation(conn, actor_id="ghost")


@pytest.mark.integration
def test_downgrade_roundtrip(db_url):
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0008")
    command.upgrade(cfg, "head")


def test_specs_are_independent_copies():
    a = copy.deepcopy(builtin_specs()[0])
    assert a.name == builtin_specs()[0].name
