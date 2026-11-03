"""Profile service + worker binding against PostgreSQL (002C-R01, R02, R05)."""

import copy
from uuid import UUID

import psycopg
import pytest

from paw_server.db.pool import create_pool
from paw_server.modules.identity import service as svc
from paw_server.modules.identity.compiler import compile_profile
from paw_server.modules.identity.spec import DEFAULT_ADMIN_SPEC, ActorProfileSpec
from paw_server.modules.runtime import lifecycle as lc

pytestmark = pytest.mark.integration


def variant(**sections) -> ActorProfileSpec:
    d = copy.deepcopy(DEFAULT_ADMIN_SPEC.model_dump(mode="json"))
    for k, v in sections.items():
        d[k] = v
    return ActorProfileSpec.model_validate(d)


@pytest.fixture
def pool(env):
    from alembic import command

    from paw_server.db.migrations_info import alembic_config

    command.upgrade(alembic_config(env.url), "head")
    p = create_pool(env.settings())
    p.open()
    yield p
    p.close()


@pytest.fixture
def conn(pool):
    with pool.connection() as c:
        yield c


def sql(env, q, *args):
    with psycopg.connect(env.url, autocommit=True) as c:
        cur = c.execute(q, args)
        return cur.fetchall() if cur.description else None


def test_default_profile_is_created_once_and_active(conn, env):
    svc.ensure_default_profile(conn)
    svc.ensure_default_profile(conn)
    versions = svc.list_versions(conn, svc.ADMIN)
    assert [(v["version"], v["status"]) for v in versions] == [(1, "active")]
    a = svc.get_active(conn, svc.ADMIN)
    assert a.spec == DEFAULT_ADMIN_SPEC and a.version == 1
    assert a.compiled == compile_profile(svc.ADMIN, 1, DEFAULT_ADMIN_SPEC)
    assert "calendar_list_events" in a.compiled.policy["grants"]


def test_ensure_default_never_touches_existing_history(conn):
    svc.ensure_default_profile(conn)
    v = svc.create_version(
        conn, svc.ADMIN, variant(budgets={"per_run_usd": 0.2, "daily_usd": 1}), "owner"
    )
    svc.activate(conn, svc.ADMIN, v["id"], "owner")
    svc.ensure_default_profile(conn)
    assert [x["version"] for x in svc.list_versions(conn, svc.ADMIN)] == [2, 1]
    assert svc.get_active(conn, svc.ADMIN).version == 2


def test_new_versions_are_drafts_with_contiguous_numbers_and_compiled_output(conn):
    svc.ensure_default_profile(conn)
    a = svc.create_version(conn, svc.ADMIN, variant(ethos=["Be kind."]), "owner")
    b = svc.create_version(conn, svc.ADMIN, variant(ethos=["Be brief."]), "owner")
    assert (a["version"], a["status"], b["version"]) == (2, "draft", 3)
    assert svc.get_active(conn, svc.ADMIN).version == 1  # drafts change nothing
    rows = {v["version"]: v for v in svc.list_versions(conn, svc.ADMIN)}
    assert (
        rows[2]["compiled_hash"]
        and rows[3]["compiled_hash"]
        and rows[2]["compiled_hash"] != rows[3]["compiled_hash"]
    )


def test_activation_retires_the_previous_version_and_only_one_is_active(conn):
    svc.ensure_default_profile(conn)
    v2 = svc.create_version(conn, svc.ADMIN, variant(ethos=["Be kind."]), "owner")
    active = svc.activate(conn, svc.ADMIN, v2["id"], "owner")
    assert active.version == 2 and active.spec.ethos == ["Be kind."]
    assert [(v["version"], v["status"]) for v in svc.list_versions(conn, svc.ADMIN)] == [
        (2, "active"),
        (1, "retired"),
    ]


@pytest.mark.parametrize("actor", ["admin-hermes", "ghost"])
def test_agents_and_unknown_actors_can_neither_create_nor_activate(conn, actor):
    svc.ensure_default_profile(conn)
    with pytest.raises(svc.ProfileError) as e:
        svc.create_version(conn, svc.ADMIN, variant(ethos=["I grant myself powers."]), actor)
    assert e.value.code == "agent_not_allowed"
    v = svc.create_version(conn, svc.ADMIN, variant(ethos=["ok"]), "owner")
    with pytest.raises(svc.ProfileError) as e:
        svc.activate(conn, svc.ADMIN, v["id"], actor)
    assert e.value.code == "agent_not_allowed"
    assert svc.get_active(conn, svc.ADMIN).version == 1


def test_activation_errors(conn):
    svc.ensure_default_profile(conn)
    v2 = svc.create_version(conn, svc.ADMIN, variant(ethos=["x"]), "owner")
    svc.activate(conn, svc.ADMIN, v2["id"], "owner")
    for vid, code in [(v2["id"], "not_draft"), (UUID(int=0), "unknown_version")]:
        with pytest.raises(svc.ProfileError) as e:
            svc.activate(conn, svc.ADMIN, vid, "owner")
        assert e.value.code == code
    other = svc.create_version(conn, svc.ADMIN, variant(ethos=["y"]), "owner")
    with pytest.raises(svc.ProfileError) as e:
        svc.activate(
            conn, "owner", other["id"], "owner"
        )  # a version of one actor cannot activate for another
    assert e.value.code == "wrong_actor"
    with pytest.raises(svc.ProfileError) as e:
        svc.create_version(conn, "nobody", variant(), "owner")
    assert e.value.code == "no_such_actor"


def test_an_activated_version_is_immutable_in_the_database(conn, env):
    svc.ensure_default_profile(conn)
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(env, "UPDATE actor_profile_versions SET spec = '{}' WHERE status = 'active'")


def test_authority_changes_lists_exactly_what_needs_confirmation():
    base = DEFAULT_ADMIN_SPEC
    assert svc.authority_changes(base, variant(ethos=["different ethos"])) == []
    assert (
        svc.authority_changes(base, variant(persona={**base.persona.model_dump(), "title": "New"}))
        == []
    )
    assert svc.authority_changes(base, variant(tools={"grants": ["docs_search"]})) == ["tools"]
    assert (
        svc.authority_changes(base, variant(tools={"grants": list(reversed(base.tools.grants))}))
        == []
    )
    assert svc.authority_changes(base, variant(budgets={"per_run_usd": 1, "daily_usd": 5})) == [
        "budgets"
    ]
    changed = variant(runtime={"model": "gpt-4o", "max_iterations": 25, "max_run_seconds": 600})
    assert svc.authority_changes(base, changed) == ["runtime"]


# ---- the worker uses the active version and records it on every run ----------------------------------------------


@pytest.fixture
def harness(env):
    from tests.test_agent_integration import Harness

    h = Harness(env)
    yield h
    h.pool.close()


def test_a_run_records_its_profile_version_and_sends_the_compiled_instructions(harness, env):
    run = harness.send(harness.session())
    harness.worker.run_once()
    row = sql(
        env,
        "SELECT profile_version, compiled_hash, profile_version_id FROM agent_runs WHERE id = %s",
        run["id"],
    )[0]
    c = compile_profile(svc.ADMIN, 1, DEFAULT_ADMIN_SPEC)
    assert row[0] == 1 and row[1] == c.compiled_hash and row[2] is not None
    assert harness.rt.created[0]["instructions"] == c.run_instructions


def test_activating_a_new_version_changes_the_next_run_but_never_an_earlier_one(harness, env):
    s = harness.session()
    first = harness.send(s, message="one")
    harness.worker.run_once()
    with harness.pool.connection() as conn:
        v2 = svc.create_version(
            conn, svc.ADMIN, variant(ethos=["Always answer in French."]), "owner"
        )
        svc.activate(conn, svc.ADMIN, v2["id"], "owner")
    second = harness.send(s, message="two")
    harness.worker.run_once()
    versions = dict(sql(env, "SELECT id::text, profile_version FROM agent_runs"))
    assert (versions[first["id"]], versions[second["id"]]) == (1, 2)
    assert (
        "French" in harness.rt.created[1]["instructions"]
        and "French" not in harness.rt.created[0]["instructions"]
    )
    hashes = dict(sql(env, "SELECT id::text, compiled_hash FROM agent_runs"))
    assert hashes[first["id"]] != hashes[second["id"]]


def test_an_in_flight_run_keeps_its_version_when_activation_happens_mid_run(harness, env):
    run = harness.send(harness.session())

    def activate_midway(i):
        if i == 1:
            with harness.pool.connection() as conn:
                v = svc.create_version(
                    conn, svc.ADMIN, variant(ethos=["Changed mid-run."]), "owner"
                )
                svc.activate(conn, svc.ADMIN, v["id"], "owner")

    harness.rt.between_events = activate_midway
    harness.worker.run_once()
    assert sql(env, "SELECT profile_version, state FROM agent_runs WHERE id = %s", run["id"]) == [
        (1, "SUCCEEDED")
    ]
    with harness.pool.connection() as conn:
        assert (
            svc.get_active(conn, svc.ADMIN).version == 2
        )  # the change did take effect, for later runs


def test_a_run_binds_the_profile_only_once(harness, env):
    run = harness.send(harness.session())
    harness.worker.run_once()
    with harness.pool.connection() as conn:
        lc.bind_profile(
            conn, UUID(run["id"]), profile_version_id=UUID(int=1), version=99, compiled_hash="x"
        )
    assert sql(env, "SELECT profile_version FROM agent_runs WHERE id = %s", run["id"]) == [(1,)]


def test_no_active_profile_means_the_run_fails_instead_of_running_unconfigured(harness, env):
    sql(env, "UPDATE actor_profile_versions SET status = 'retired'")
    run = harness.send(harness.session())
    harness.worker.run_once()
    r = harness.run(run["id"])
    assert (r["state"], r["error_code"]) == (
        "FAILED",
        "no_active_profile",
    ) and harness.rt.created == []
