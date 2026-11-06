"""Exact approval payload, replay/expiry, restart and uncertain-write recovery."""

import copy

import psycopg
import pytest

from paw_server.modules.approvals.service import ApprovalError, ApprovalService
from paw_server.modules.connectors.github import GitHubError
from paw_server.modules.connectors.github_service import GitHubService
from paw_server.modules.gateway.builtin import builtin_registry
from paw_server.modules.gateway.registry import sync_registry
from paw_server.modules.gateway.service import GatewayService
from paw_server.modules.identity import service as identity
from paw_server.modules.identity.spec import DEFAULT_ADMIN_SPEC, ActorProfileSpec
from tests.conftest import AUTH, threads
from tests.gateway.test_service import start_run
from tests.test_agent_integration import Harness
from tests.test_auth_integration import sql
from tests.test_github import KEY, REPO, FakeGitHub

pytestmark = pytest.mark.integration
ARGS = {"repository": REPO["full_name"], "title": "Exact title", "body": "Exact body"}


class IssueGitHub(FakeGitHub):
    def __init__(self):
        super().__init__()
        self.posts = []
        self.issues = {}
        self.timeout = False

    def create_issue(self, installation, repo, args, operation):
        self.posts.append(copy.deepcopy(args))
        issue = {
            "number": len(self.posts),
            "url": "https://github.com/paw-space/paw-sandbox/issues/1",
        }
        self.issues[operation] = issue
        if self.timeout:
            raise GitHubError("unavailable")
        return issue

    def find_issue(self, installation, repo, operation):
        return self.issues.get(operation)


@pytest.fixture
def approvals(env):
    h = Harness(
        env,
        connector_key=KEY,
        github_sandbox_repository=REPO["full_name"],
        github_app_slug="paw-space",
    )
    spec = DEFAULT_ADMIN_SPEC.model_dump(mode="json")
    spec["tools"]["grants"].extend(["github_create_issue", "github_list_repos", "github_get_file"])
    spec["tools"]["resource_scopes"] = {
        "github_create_issue": [REPO["full_name"]],
        "github_get_file": [REPO["full_name"]],
        "github_list_repos": [REPO["full_name"]],
    }
    with h.pool.connection() as conn:
        v = identity.create_version(
            conn, "admin-hermes", ActorProfileSpec.model_validate(spec), "owner"
        )
        identity.activate(conn, "admin-hermes", v["id"], "owner")
        sync_registry(conn, builtin_registry())
    fake = IssueGitHub()
    github = GitHubService(h.pool, h.settings, fake)
    h.c.app.state.github = github
    github.connect("owner")
    svc = ApprovalService(h.pool, github)
    g = GatewayService(h.pool, builtin_registry(), h.settings, approvals=svc)
    start_run(h)
    out = g.handle_call("github_create_issue", ARGS)
    assert out.reason == "APPROVAL_REQUIRED", out
    assert not out.is_error and "PENDING" in out.text
    assert fake.posts == []
    row = svc.inbox("owner")[0]
    yield h, svc, fake, row
    h.c.close()
    h.pool.close()


def test_only_stored_payload_executes_once_and_cannot_be_mutated(approvals, env):
    h, svc, fake, row = approvals
    with pytest.raises(ApprovalError, match="payload_changed"):
        svc.decide("owner", row["id"], "0" * 64, True)
    with pytest.raises(psycopg.errors.CheckViolation):
        sql(env, "UPDATE approval_requests SET args='{}'::jsonb WHERE id=%s", row["id"])
    body = {"args_hash": row["args_hash"], "approve": True, "args": {**ARGS, "title": "mutated"}}
    assert h.c.post(f"/v1/approvals/{row['id']}/decide", headers=AUTH, json=body).status_code == 422
    result = h.c.post(
        f"/v1/approvals/{row['id']}/decide",
        headers=AUTH,
        json={"args_hash": row["args_hash"], "approve": True},
    )
    assert result.status_code == 200, result.text
    assert result.json()["state"] == "SUCCEEDED"
    assert fake.posts == [ARGS]
    with pytest.raises(ApprovalError, match="already_used"):
        svc.decide("owner", row["id"], row["args_hash"], True)
    assert len(fake.posts) == 1


def test_timeout_after_creation_reconciles_without_second_post(approvals):
    h, svc, fake, row = approvals
    fake.timeout = True
    assert svc.decide("owner", row["id"], row["args_hash"], True)["state"] == "UNKNOWN"
    restarted = ApprovalService(h.pool, svc.github)
    assert restarted.reconcile("owner", row["id"])["state"] == "SUCCEEDED"
    assert len(fake.posts) == 1


def test_two_owner_clicks_cannot_create_two_issues(approvals):
    _, svc, fake, row = approvals

    def click():
        try:
            return svc.decide("owner", row["id"], row["args_hash"], True)["state"]
        except ApprovalError as exc:
            return str(exc)

    assert sorted(threads([click, click])) == ["SUCCEEDED", "already_used"]
    assert fake.posts == [ARGS]


@pytest.mark.parametrize(
    "change",
    ["revoke", "disable_tool", "disable_actor", "profile_revoke", "forget", "expire", "reject"],
)
def test_pending_approval_is_fenced_by_changed_authority(approvals, env, change):
    _, svc, fake, row = approvals
    if change == "revoke":
        svc.github.disconnect("owner")
    elif change == "disable_tool":
        sql(env, "UPDATE gateway_tools SET enabled=false WHERE name='github_create_issue'")
    elif change == "disable_actor":
        sql(env, "UPDATE actors SET status='disabled' WHERE id='admin-hermes'")
    elif change == "profile_revoke":
        h = approvals[0]
        with h.pool.connection() as conn:
            active = identity.get_active(conn, "admin-hermes")
            spec = active.spec.model_copy(deep=True)
            spec.tools.grants.remove("github_create_issue")
            version = identity.create_version(conn, "admin-hermes", spec, "owner")
            identity.activate(conn, "admin-hermes", version["id"], "owner")
    elif change == "forget":
        sql(
            env,
            "UPDATE agent_sessions SET context_revision=context_revision+1 WHERE id=%s",
            row["session_id"],
        )
    elif change == "expire":
        with psycopg.connect(env.url, autocommit=True) as conn:
            with conn.transaction():
                conn.execute("SET LOCAL paw.maintenance='redact'")
                conn.execute(
                    "UPDATE approval_requests SET expires_at=now()-interval '1 second' WHERE id=%s",
                    (row["id"],),
                )
    result = svc.decide("owner", row["id"], row["args_hash"], change != "reject")
    assert result["state"] in ("FAILED", "INVALIDATED", "EXPIRED", "REJECTED")
    assert fake.posts == []


def test_wrong_owner_cannot_consume_or_view_approval(approvals):
    _, svc, fake, row = approvals
    assert svc.inbox("another-owner") == []
    with pytest.raises(ApprovalError, match="not_found"):
        svc.decide("another-owner", row["id"], row["args_hash"], True)
    assert fake.posts == []


def test_privacy_deletion_removes_approval_payload_and_prevents_write(approvals, env):
    h, svc, fake, row = approvals
    response = h.c.post(
        f"/v1/agent-sessions/{row['session_id']}/delete",
        headers=AUTH,
        json={"include_delegated": False},
    )
    assert response.status_code == 200, response.text
    assert svc.inbox("owner") == []
    assert sql(env, "SELECT args,state FROM approval_requests") == [({}, "INVALIDATED")]
    with pytest.raises(ApprovalError, match="already_used"):
        svc.decide("owner", row["id"], row["args_hash"], True)
    assert fake.posts == []


def test_gateway_passes_actor_repository_scope_to_connector(approvals):
    from paw_server.modules.gateway.github_tools import github_specs
    from paw_server.modules.gateway.registry import ToolRegistry

    h, svc, fake, _ = approvals
    g = GatewayService(h.pool, ToolRegistry(github_specs(svc.github)), h.settings)
    allowed = g.handle_call(
        "github_get_file", {"repository": REPO["full_name"], "path": "README.md"}
    )
    assert allowed.reason == "OK", allowed
    denied = g.handle_call(
        "github_get_file", {"repository": "someone/guessed-private", "path": "README.md"}
    )
    assert denied.reason == "OUT_OF_SCOPE"
    assert len(fake.reads) == 1
    repos = g.handle_call("github_list_repos", {})
    assert repos.reason == "OK" and REPO["full_name"] in repos.text
