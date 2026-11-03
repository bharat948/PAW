"""Profile API: owner-only editing, confirmation for authority changes, step-up, restart signal (002C-R05)."""

import copy

import psycopg
import pytest

from paw_server.api.deps import SESSION_COOKIE
from paw_server.modules.identity.spec import DEFAULT_ADMIN_SPEC
from tests.conftest import AUTH
from tests.test_auth_integration import GOOGLE, FakeGoogle, csrf, sign_in

pytestmark = pytest.mark.integration


def spec(**over) -> dict:
    d = copy.deepcopy(DEFAULT_ADMIN_SPEC.model_dump(mode="json"))
    d.update(over)
    return d


def sql(env, q, *args):
    with psycopg.connect(env.url, autocommit=True) as c:
        cur = c.execute(q, args)
        return cur.fetchall() if cur.description else None


@pytest.fixture
def c(env):
    return env.client()


def test_requires_authentication(c):
    for method, path in [("get", "/v1/agent/profile"), ("post", "/v1/agent/profile/versions"),
                         ("post", "/v1/agent/profile/versions/00000000-0000-0000-0000-000000000000/activate")]:  # fmt: skip
        assert getattr(c, method)(path).status_code == 401, path


def test_a_fresh_install_shows_version_1_active_without_the_worker(c):
    body = c.get("/v1/agent/profile", headers=AUTH).json()
    assert (
        body["actor_id"] == "admin-hermes"
        and body["active"]["version"] == 1
        and body["active"]["status"] == "active"
    )
    assert (
        body["active"]["spec"]["persona"]["title"] == "PAW Admin"
        and body["active"]["compiled_hash"]
    )
    assert "calendar_list_events" in body["known_tools"] and len(body["versions"]) == 1


def test_drafting_a_persona_only_change_needs_no_confirmation_and_changes_nothing_until_activated(
    c,
):
    c.get("/v1/agent/profile", headers=AUTH)
    d = spec(ethos=["Always greet warmly."])
    r = c.post("/v1/agent/profile/versions", json={"spec": d}, headers=AUTH)
    assert r.status_code == 201 and r.json()["version"] == 2 and r.json()["status"] == "draft"
    assert r.json()["changes"] == [] and r.json()["requires_confirmation"] is False
    assert c.get("/v1/agent/profile", headers=AUTH).json()["active"]["version"] == 1
    a = c.post(f"/v1/agent/profile/versions/{r.json()['id']}/activate", json={}, headers=AUTH)
    assert (
        a.status_code == 200 and a.json()["version"] == 2 and a.json()["restart_required"] is False
    )
    assert c.get("/v1/agent/profile", headers=AUTH).json()["active"]["spec"]["ethos"] == [
        "Always greet warmly."
    ]


def test_changing_tools_needs_explicit_confirmation_and_then_asks_for_a_restart(c):
    d = spec(tools={"grants": ["docs_search"]})
    r = c.post("/v1/agent/profile/versions", json={"spec": d}, headers=AUTH).json()
    assert r["changes"] == ["tools"] and r["requires_confirmation"] is True
    url = f"/v1/agent/profile/versions/{r['id']}/activate"
    refused = c.post(url, json={}, headers=AUTH)
    assert (
        refused.status_code == 409
        and refused.json()["code"] == "confirmation_required"
        and "tools" in refused.json()["message"]
    )
    assert c.post(url, json={"confirm": False}, headers=AUTH).status_code == 409
    assert (
        c.get("/v1/agent/profile", headers=AUTH).json()["active"]["version"] == 1
    )  # nothing changed
    ok = c.post(url, json={"confirm": True}, headers=AUTH)
    assert (
        ok.status_code == 200
        and ok.json()["changes"] == ["tools"]
        and ok.json()["restart_required"] is True
    )


def test_budget_changes_need_confirmation_but_no_restart(c):
    d = spec(budgets={"per_run_usd": 1.0, "daily_usd": 5.0})
    r = c.post("/v1/agent/profile/versions", json={"spec": d}, headers=AUTH).json()
    assert r["changes"] == ["budgets"]
    url = f"/v1/agent/profile/versions/{r['id']}/activate"
    assert c.post(url, json={}, headers=AUTH).status_code == 409
    ok = c.post(url, json={"confirm": True}, headers=AUTH).json()
    assert ok["restart_required"] is False


def test_unknown_tools_invalid_specs_and_forged_fields_are_rejected(c, env):
    bad_tool = c.post(
        "/v1/agent/profile/versions",
        json={"spec": spec(tools={"grants": ["delete_everything"]})},
        headers=AUTH,
    )
    assert bad_tool.status_code == 422 and bad_tool.json()["code"] == "unknown_tool"
    assert (
        c.post(
            "/v1/agent/profile/versions", json={"spec": spec(ethos=[])}, headers=AUTH
        ).status_code
        == 422
    )
    assert (
        c.post(
            "/v1/agent/profile/versions", json={"spec": {**spec(), "owner": "me"}}, headers=AUTH
        ).status_code
        == 422
    )
    assert c.post(
        "/v1/agent/profile/versions",
        json={"spec": spec(), "created_by": "admin-hermes"},
        headers=AUTH,
    ).status_code in (201, 422)
    assert {r[0] for r in sql(env, "SELECT created_by FROM actor_profile_versions")} == {
        "owner"
    }  # never client-supplied


def test_activation_errors(c):
    assert (
        c.post(
            "/v1/agent/profile/versions/00000000-0000-0000-0000-000000000000/activate",
            json={},
            headers=AUTH,
        ).status_code
        == 404
    )
    active = c.get("/v1/agent/profile", headers=AUTH).json()["active"]
    again = c.post(f"/v1/agent/profile/versions/{active['id']}/activate", json={}, headers=AUTH)
    assert again.status_code == 409 and again.json()["code"] == "not_activatable"


def test_versions_accumulate_in_order_and_history_stays_readable(c):
    for i in range(3):
        v = c.post(
            "/v1/agent/profile/versions", json={"spec": spec(ethos=[f"Rule {i}"])}, headers=AUTH
        ).json()
        c.post(f"/v1/agent/profile/versions/{v['id']}/activate", json={}, headers=AUTH)
    body = c.get("/v1/agent/profile", headers=AUTH).json()
    assert [(v["version"], v["status"]) for v in body["versions"]] == [
        (4, "active"),
        (3, "retired"),
        (2, "retired"),
        (1, "retired"),
    ]
    assert body["versions"][3]["spec"] == DEFAULT_ADMIN_SPEC.model_dump(mode="json")


# ---- with Google sessions: CSRF and step-up apply to activation ---------------------------------------------------


def google_client(env, **kw):
    g = FakeGoogle()
    cl = env.client(idp=g, auth_mode="google", **GOOGLE, **kw)
    sign_in(cl, g)
    return cl


def test_activation_needs_csrf_with_a_session(env):
    cl = google_client(env)
    v = cl.post(
        "/v1/agent/profile/versions", json={"spec": spec(ethos=["x"])}, headers=csrf(cl)
    ).json()
    url = f"/v1/agent/profile/versions/{v['id']}/activate"
    assert cl.post(url, json={}).status_code == 403
    assert cl.post(url, json={}, headers=csrf(cl)).status_code == 200


def test_activation_needs_a_recent_google_sign_in(env):
    cl = google_client(env, stepup_window_s=0)  # a window of zero: the sign-in is already too old
    v = cl.post(
        "/v1/agent/profile/versions", json={"spec": spec(ethos=["x"])}, headers=csrf(cl)
    ).json()
    r = cl.post(f"/v1/agent/profile/versions/{v['id']}/activate", json={}, headers=csrf(cl))
    assert r.status_code == 403 and r.json()["code"] == "stepup_required"
    assert cl.get("/v1/agent/profile", headers=csrf(cl)).json()["active"]["version"] == 1


def test_the_agent_has_no_credential_that_reaches_these_routes(env):
    cl = env.client()
    assert SESSION_COOKIE not in cl.cookies
    r = cl.post(
        "/v1/agent/profile/versions",
        json={"spec": spec()},
        headers={"Authorization": "Bearer some-runtime-key"},
    )
    assert r.status_code == 401  # a runtime or gateway credential is not an owner credential
