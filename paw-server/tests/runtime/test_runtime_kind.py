"""Runtime kind (hermes | voice): a voice runtime is a second, isolated runtime beside Admin Hermès.

Slice 1 of the voice runtime: identity, registry and routing only. No telephony is involved; the worker is given a
fake runtime exactly as the other registry tests do.
"""

import sys

import psycopg
import pytest

from paw_server import runtime_registry
from paw_server.modules.gateway.builtin import builtin_specs
from paw_server.modules.identity import service as identity
from paw_server.modules.identity.spec import DEFAULT_ADMIN_SPEC, DEFAULT_VOICE_SPEC
from paw_server.modules.runtime import registry
from paw_server.modules.runtime.agent_worker import AgentWorker
from tests.conftest import AUTH
from tests.runtime.fakes import FakeRuntime, completed, delta
from tests.test_agent_integration import Harness

pytestmark = pytest.mark.integration
VOICE_GATEWAY = "voice-runtime-gateway-" + "g" * 24
VOICE_MODEL = "voice-runtime-model-" + "m" * 24


def register_voice(conn, name="voice"):
    identity.ensure_profile(conn, identity.VOICE, DEFAULT_VOICE_SPEC)
    rid = registry.register(
        conn,
        name,
        identity.VOICE,
        endpoint_url="http://127.0.0.1:9998",
        key_env="PAW_VOICE_KEY",
        kind="voice",
    )
    registry.credential(conn, rid, "gateway", VOICE_GATEWAY)
    registry.credential(conn, rid, "model", VOICE_MODEL)
    return rid


@pytest.fixture
def pair(env):
    h = Harness(env)
    with h.pool.connection() as conn:
        register_voice(conn)
    rt = FakeRuntime()
    worker = AgentWorker(h.pool, rt, env.settings(agent_runtime_name="voice"))
    yield h, worker, rt
    h.c.__exit__(None, None, None)
    h.pool.close()


def enqueue(h, actor):
    response = h.c.post("/v1/agent-sessions", json={"agent_actor_id": actor}, headers=AUTH)
    assert response.status_code == 201, response.text
    return h.send(response.json()["id"])


def test_installations_without_voice_are_unchanged(env):
    h = Harness(env)
    try:
        with h.pool.connection() as conn:
            kinds = conn.execute("SELECT name, kind FROM agent_runtimes").fetchall()
            actor = conn.execute(
                "SELECT kind, status FROM actors WHERE id=%s", (identity.VOICE,)
            ).fetchone()
            profiles = conn.execute(
                "SELECT 1 FROM actor_profile_versions WHERE actor_id=%s", (identity.VOICE,)
            ).fetchall()
            conn.commit()
        assert {r["kind"] for r in kinds} <= {"hermes"}
        assert actor == {"kind": "agent", "status": "active"}
        assert profiles == []  # the actor exists but is inert until an operator opts in
    finally:
        h.c.__exit__(None, None, None)
        h.pool.close()


def test_voice_registration_records_kind_and_the_voice_profile(pair):
    h, _, _ = pair
    with h.pool.connection() as conn:
        row = conn.execute("SELECT * FROM agent_runtimes WHERE name='voice'").fetchone()
        active = identity.get_active(conn, identity.VOICE)
        conn.commit()
    assert row["kind"] == "voice" and row["actor_id"] == identity.VOICE
    assert row["desired_profile_version_id"] == active.version_id
    assert active.spec.runtime.harness == "voice"
    assert active.compiled.policy["grants"] == ["calendar_list_events"]
    assert active.compiled.policy["max_run_seconds"] == 900


def test_default_voice_grants_are_real_gateway_tools_and_nothing_reads_documents():
    known = {s.name for s in builtin_specs()}
    assert set(DEFAULT_VOICE_SPEC.tools.grants) <= known
    assert not {"docs_search", "docs_get_source"} & set(DEFAULT_VOICE_SPEC.tools.grants)


def test_kind_is_fixed_and_must_match_the_actors_profile(pair):
    h, _, _ = pair
    with h.pool.connection() as conn:
        with pytest.raises(registry.RegistryError, match="kind cannot change"):
            registry.register(conn, "voice", identity.VOICE, kind="hermes")
        # no kind given keeps the stored kind, so legacy-style re-registration is harmless
        again = registry.register(conn, "voice", identity.VOICE)
        row = conn.execute("SELECT id, kind FROM agent_runtimes WHERE name='voice'").fetchone()
        assert row["id"] == again and row["kind"] == "voice"
        with pytest.raises(registry.RegistryError, match="harness does not match"):
            registry.register(conn, "bogus-hermes", identity.VOICE, kind="hermes")
        with pytest.raises(registry.RegistryError, match="harness does not match"):
            registry.register(conn, "bogus-voice", identity.ADMIN, kind="voice")
        with pytest.raises(registry.RegistryError, match="invalid runtime kind"):
            registry.register(conn, "weird", identity.VOICE, kind="sip")


def test_the_database_refuses_to_change_kind(pair):
    h, _, _ = pair
    with h.pool.connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation, match="kind is immutable"):
            conn.execute("UPDATE agent_runtimes SET kind='hermes' WHERE name='voice'")
        conn.rollback()
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE agent_runtimes SET kind='sip' WHERE name='voice'")


def test_a_profile_for_the_wrong_harness_is_not_accepted_on_the_other_kind(env):
    h = Harness(env)
    try:
        with h.pool.connection() as conn:
            # Admin's hermes profile cannot back a voice runtime, nor can voice's back a hermes one.
            with pytest.raises(registry.RegistryError, match="harness does not match"):
                registry.register(conn, "x", identity.ADMIN, kind="voice")
            identity.ensure_profile(conn, identity.VOICE, DEFAULT_VOICE_SPEC)
            with pytest.raises(registry.RegistryError, match="harness does not match"):
                registry.register(conn, "y", identity.VOICE)  # defaults to hermes
    finally:
        h.c.__exit__(None, None, None)
        h.pool.close()


def test_there_is_no_adapter_for_voice_yet_and_it_fails_clearly(pair, env):
    h, _, _ = pair
    with h.pool.connection() as conn:
        rid = conn.execute("SELECT id FROM agent_runtimes WHERE name='voice'").fetchone()["id"]
        conn.commit()
        with pytest.raises(registry.RegistryError, match="no adapter for runtime kind 'voice'"):
            registry.adapter(conn, rid, env.settings())


def test_voice_and_admin_work_never_cross(pair):
    h, voice_worker, voice_rt = pair
    call = enqueue(h, identity.VOICE)  # older: Admin's worker must skip it
    chat = enqueue(h, identity.ADMIN)

    voice_rt.script = h.rt.script = lambda _: [delta("ok"), completed("ok")]
    assert h.worker.run_once() is True
    assert [r["run_id"] for r in h.rt.created] == [chat["id"]]
    assert voice_rt.created == []

    assert voice_worker.run_once() is True
    assert [r["run_id"] for r in voice_rt.created] == [call["id"]]
    assert voice_worker.run_once() is False and h.worker.run_once() is False


def test_voice_runs_use_the_voice_profile_instructions(pair):
    h, voice_worker, voice_rt = pair
    enqueue(h, identity.VOICE)
    voice_rt.script = lambda _: [completed("done")]
    assert voice_worker.run_once() is True
    sent = voice_rt.created[0]["instructions"]
    assert "AI assistant calling on behalf of Bharat" in sent
    assert "calendar_list_events" in sent and "docs_search" not in sent


def test_credentials_resolve_to_their_own_runtime_and_purpose(pair):
    h, _, _ = pair
    admin_token = "admin-runtime-gateway-" + "a" * 24
    with h.pool.connection() as conn:
        admin_rt = registry.register(conn, "hermes", identity.ADMIN)
        registry.credential(conn, admin_rt, "gateway", admin_token)
        voice = registry.authenticate(conn, VOICE_GATEWAY, "gateway")
        assert voice is not None and voice.name == "voice" and voice.actor_id == identity.VOICE
        assert registry.authenticate(conn, VOICE_GATEWAY, "model") is None
        assert registry.authenticate(conn, VOICE_MODEL, "gateway") is None
        admin = registry.authenticate(conn, admin_token, "gateway")
        assert admin is not None and admin.actor_id == identity.ADMIN
        registry.stop(conn, voice.id)
        assert registry.authenticate(conn, VOICE_GATEWAY, "gateway") is None
        assert registry.authenticate(conn, admin_token, "gateway") is not None


def test_cli_rejects_a_voice_runtime_for_any_other_actor(monkeypatch):
    argv = [
        "runtime_registry", "register", "--name", "v", "--actor", identity.ADMIN,
        "--url", "http://127.0.0.1:1", "--key-env", "PAW_VOICE_KEY", "--kind", "voice",
    ]  # fmt: skip
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as stop:
        runtime_registry.main()
    assert stop.value.code == 2


def test_voice_spec_is_a_valid_distinct_profile():
    assert DEFAULT_VOICE_SPEC.runtime.harness == "voice"
    assert DEFAULT_ADMIN_SPEC.runtime.harness == "hermes"
    assert DEFAULT_VOICE_SPEC.budgets.per_run_usd <= DEFAULT_VOICE_SPEC.budgets.daily_usd


def test_migration_0018_downgrades_and_upgrades_again(db_url):
    from alembic import command

    from paw_server.db.migrations_info import alembic_config

    cfg = alembic_config(db_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "0017")
    with psycopg.connect(db_url) as conn:
        cols = {
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name='agent_runtimes'"
            )
        }
        assert "kind" not in cols
        # the pre-0018 guard still protects the actor binding (a fresh database has no runtime row yet)
        conn.execute("INSERT INTO agent_runtimes(name, actor_id) VALUES ('probe', 'admin-hermes')")
        with pytest.raises(psycopg.errors.CheckViolation, match="actor binding"):
            conn.execute("UPDATE agent_runtimes SET actor_id='owner' WHERE name='probe'")
        conn.rollback()
    command.upgrade(cfg, "head")
    with psycopg.connect(db_url) as conn:
        assert conn.execute("SELECT count(*) FROM actors WHERE id='voice-agent'").fetchone()[0] == 1
