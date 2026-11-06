"""GitHub transport and owner connection boundaries; no live provider calls or credentials."""

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet

from paw_server.modules.connectors.github import GitHubError, GitHubProvider
from paw_server.modules.connectors.github_service import GitHubService
from tests.conftest import AUTH
from tests.test_auth_integration import GOOGLE, FakeGoogle, csrf, sign_in, sql

REPO = {"id": 23, "full_name": "paw-space/paw-sandbox", "private": True, "default_branch": "main"}
KEY = Fernet.generate_key().decode()


@pytest.fixture
def transport(monkeypatch):
    seen = []
    responses = []

    def handle(request):
        seen.append(request)
        assert responses, request.url
        status, data, headers = responses.pop(0)
        return httpx.Response(status, json=data, headers=headers)

    provider = GitHubProvider(
        12,
        "paw-space",
        "/nonexistent",
        httpx.Client(base_url="https://api.github.com", transport=httpx.MockTransport(handle)),
    )
    monkeypatch.setattr(provider, "_jwt", lambda: "app-secret-marker")
    return provider, seen, responses


def test_tokens_are_downscoped_cached_and_expire(transport):
    p, seen, responses = transport
    exp = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    responses.append((201, {"token": "installation-secret-marker", "expires_at": exp}, {}))
    assert p.token(42, 23) == p.token(42, 23) == "installation-secret-marker"
    assert len(seen) == 1
    body = json.loads(seen[0].content)
    assert body["repository_ids"] == [23] and body["permissions"]["issues"] == "read"
    p._tokens[(42, 23, False)] = ("expired", 0)
    responses.append((201, {"token": "new-token", "expires_at": exp}, {}))
    assert p.token(42, 23) == "new-token"
    responses.append((201, {"token": "write-token", "expires_at": exp}, {}))
    assert p.token(42, 23, write=True) == "write-token"
    assert json.loads(seen[-1].content)["permissions"]["issues"] == "write"


@pytest.mark.parametrize(
    "status,headers,code",
    [
        (403, {"x-ratelimit-remaining": "0"}, "rate_limited"),
        (429, {"retry-after": "30"}, "rate_limited"),
        (403, {}, "forbidden"),
        (401, {}, "revoked"),
        (500, {}, "unavailable"),
    ],
)
def test_provider_errors_never_echo_secret_response(transport, status, headers, code):
    p, _, responses = transport
    responses.append((status, {"message": "SECRET-ERROR-BODY"}, headers))
    with pytest.raises(GitHubError) as err:
        p._request("GET", "/test", "SECRET-TOKEN")
    assert str(err.value) == code and "SECRET" not in str(err.value)


def test_installation_must_belong_to_configured_account_and_app(transport):
    p, _, responses = transport
    for data in (
        {"id": 42, "app_id": 99, "account": {"login": "paw-space"}},
        {"id": 42, "app_id": 12, "account": {"login": "someone-else"}},
        {"id": 42, "app_id": 12, "account": {"login": "paw-space"}, "suspended_at": "now"},
    ):
        responses.append((200, data, {}))
        with pytest.raises(GitHubError, match="installation_mismatch"):
            p.installation()


def test_file_size_is_enforced_on_decoded_content(transport, monkeypatch):
    p, _, responses = transport
    monkeypatch.setattr(p, "token", lambda *a, **kw: "token")
    responses.append(
        (
            200,
            {
                "type": "file",
                "encoding": "base64",
                "size": 1,
                "content": base64.b64encode(b"x" * (128 * 1024 + 1)).decode(),
            },
            {},
        )
    )
    with pytest.raises(GitHubError, match="unsupported_file"):
        p.read(42, REPO, "github_get_file", {"path": "README.md"})


class FakeGitHub:
    app_id = 12
    account = "paw-space"

    def __init__(self):
        self.repos = [REPO.copy()]
        self.installed = True
        self.reads = []
        self.after_read = None

    def installation(self):
        if not self.installed:
            raise GitHubError("not_installed")
        return {"id": 42, "app_id": 12, "account": {"login": self.account}}

    def repositories(self, installation):
        assert installation == 42
        return self.repos.copy()

    def clear_tokens(self):
        pass

    def read(self, installation, repo, tool, args):
        self.reads.append((repo, tool, args))
        if self.after_read:
            self.after_read()
        return {"text": "source-text"}


@pytest.fixture
def gh(env):
    c = env.client(
        connector_key=KEY, github_app_slug="paw-space", github_webhook_secret="webhook-secret"
    )
    fake = FakeGitHub()
    svc = GitHubService(c.app.state.pool, c.app.state.settings, fake)
    c.app.state.github = svc
    yield c, svc, fake
    c.close()


@pytest.mark.integration
def test_connect_owner_auth_encryption_scope_and_revoke(gh, env):
    c, svc, fake = gh
    assert c.post("/v1/integrations/github/connect").status_code == 401
    assert c.post("/v1/integrations/github/connect", headers=AUTH).status_code == 200
    assert (
        c.get("/v1/integrations/github/repositories", headers=AUTH).json()["repositories"][0][
            "full_name"
        ]
        == REPO["full_name"]
    )
    with pytest.raises(GitHubError, match="out_of_scope"):
        svc.read("owner", "github_get_file", {"repository": "someone/private", "path": "secret"})
    assert fake.reads == []
    with pytest.raises(GitHubError, match="out_of_scope"):
        svc.read(
            "owner",
            "github_get_file",
            {"repository": REPO["full_name"], "path": "README.md"},
            scopes=[],
        )
    fake.repos.append({**REPO, "id": 24, "full_name": "paw-space/new-private"})
    assert len(svc.read("owner", "github_list_repos", {})["repositories"]) == 1
    assert svc.read("owner", "github_list_repos", {}, scopes=[]) == {"repositories": []}
    ciphertext = sql(env, "SELECT ciphertext FROM connector_secrets")[0][0]
    assert b"installation_id" not in bytes(ciphertext)
    fake.installed = False
    with pytest.raises(GitHubError, match="revoked"):
        svc.read("owner", "github_list_repos", {})
    assert sql(env, "SELECT count(*) FROM connector_secrets") == [(0,)]
    assert c.get("/v1/integrations/github/repositories", headers=AUTH).status_code == 409


@pytest.mark.integration
def test_repository_removed_between_reads_denied_before_content(gh):
    _, svc, fake = gh
    svc.connect("owner")
    assert (
        svc.read(
            "owner", "github_get_file", {"repository": REPO["full_name"], "path": "README.md"}
        )["text"]
        == "source-text"
    )
    fake.repos = []
    with pytest.raises(GitHubError, match="out_of_scope"):
        svc.read("owner", "github_get_file", {"repository": REPO["full_name"], "path": "README.md"})
    assert len(fake.reads) == 1


@pytest.mark.integration
def test_disconnect_during_read_discards_result(gh):
    _, svc, fake = gh
    svc.connect("owner")
    fake.after_read = lambda: svc.disconnect("owner")
    with pytest.raises(GitHubError, match="revoked"):
        svc.read("owner", "github_get_file", {"repository": REPO["full_name"], "path": "README.md"})


@pytest.mark.integration
def test_callback_is_session_bound_single_use_and_checks_installation(env):
    g = FakeGoogle()
    c = env.client(
        idp=g, auth_mode="google", connector_key=KEY, github_app_slug="paw-space", **GOOGLE
    )
    svc = GitHubService(c.app.state.pool, c.app.state.settings, FakeGitHub())
    c.app.state.github = svc
    sign_in(c, g)

    def start():
        r = c.get("/v1/integrations/github/connect", follow_redirects=False)
        assert r.status_code == 302, r.text
        return parse_qs(urlparse(r.headers["location"]).query)["state"][0]

    def finish(state, installation=42):
        return c.get(
            "/v1/integrations/github/installed",
            params={"state": state, "installation_id": installation},
            follow_redirects=False,
        )

    state = start()
    assert "github_failed" in finish(state, 99).headers["location"]
    assert "expired" in finish(state).headers["location"]
    assert sql(env, "SELECT count(*) FROM connector_connections") == [(0,)]
    state = start()
    assert "expired" in finish(state + "tampered").headers["location"]
    assert "integration_error" not in finish(state).headers["location"]
    assert "expired" in finish(state).headers["location"]
    assert c.post("/v1/integrations/github/connect").status_code == 403  # CSRF
    assert c.post("/v1/integrations/github/connect", headers=csrf(c)).status_code == 200
    state = start()
    sign_in(c, g)  # different browser session identity
    assert "github_failed" in finish(state).headers["location"]
    sql(env, "UPDATE auth_sessions SET authenticated_at=now()-interval '1 day'")
    assert c.post("/v1/integrations/github/connect", headers=csrf(c)).status_code == 403
    c.close()


@pytest.mark.integration
def test_signed_uninstall_webhook_replay_and_forgery(gh, env):
    c, svc, _ = gh
    svc.connect("owner")
    body = json.dumps({"action": "deleted", "installation": {"id": 42}}).encode()
    headers = {
        "x-github-delivery": str(uuid4()),
        "x-github-event": "installation",
        "x-hub-signature-256": "sha256="
        + hmac.new(b"webhook-secret", body, hashlib.sha256).hexdigest(),
    }
    assert (
        c.post(
            "/v1/integrations/github/webhook",
            content=body,
            headers={**headers, "x-hub-signature-256": "sha256=forged"},
        ).status_code
        == 401
    )
    assert sql(env, "SELECT count(*) FROM connector_secrets") == [(1,)]
    assert (
        c.post("/v1/integrations/github/webhook", content=body, headers=headers).status_code == 204
    )
    assert (
        c.post("/v1/integrations/github/webhook", content=body, headers=headers).status_code == 204
    )
    assert sql(env, "SELECT count(*) FROM connector_secrets") == [(0,)]
    assert sql(env, "SELECT count(*) FROM github_webhook_receipts") == [(1,)]


@pytest.mark.parametrize(
    "tool,args,response,path,key",
    [
        (
            "github_get_file",
            {"path": "README.md", "ref": "release/1"},
            {
                "type": "file",
                "encoding": "base64",
                "size": 5,
                "content": "aGVsbG8=",
                "sha": "abc123",
            },
            "/contents/README.md?ref=release%2F1",
            "text",
        ),
        (
            "github_list_prs",
            {},
            [{"number": 1, "title": "PR", "head": {"sha": "abc"}, "base": {"ref": "main"}}],
            "/pulls?state=open&per_page=20",
            "pull_requests",
        ),
        (
            "github_get_pr",
            {"number": 1},
            {"number": 1, "title": "PR", "head": {"sha": "abc"}, "base": {"ref": "main"}},
            "/pulls/1",
            "pull_request",
        ),
        (
            "github_get_checks",
            {"ref": "abc"},
            {"check_runs": [{"id": 1, "name": "test", "conclusion": "success"}]},
            "/commits/abc/check-runs?per_page=20",
            "checks",
        ),
        (
            "github_list_issues",
            {},
            [{"number": 1, "title": "issue"}, {"number": 2, "pull_request": {}}],
            "/issues?state=open&per_page=20",
            "issues",
        ),
    ],
)
def test_read_contracts_use_scoped_tokens_and_preserve_sources(
    transport, monkeypatch, tool, args, response, path, key
):
    provider, seen, responses = transport
    monkeypatch.setattr(
        provider,
        "token",
        lambda installation, repo_id: (
            "repo-token"
            if (installation, repo_id) == (42, 23)
            else pytest.fail("wrong token scope")
        ),
    )
    responses.append((200, response, {}))
    data = provider.read(42, REPO, tool, args)
    assert str(seen[-1].url).endswith("/repos/paw-space/paw-sandbox" + path)
    assert data["repository"] == REPO["full_name"] and key in data
    if tool == "github_get_file":
        assert data["sha"] == "abc123" and data["text"] == "hello"
    if tool == "github_list_issues":
        assert len(data["issues"]) == 1


@pytest.mark.integration
def test_transport_credentials_do_not_enter_persisted_rows_or_logs(gh, transport, env, caplog):
    import logging

    import psycopg
    from psycopg import sql as pgsql

    c, svc, _ = gh
    provider, _, responses = transport
    svc.provider = provider
    exp = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    installation = {"id": 42, "app_id": 12, "account": {"login": "paw-space"}}
    grant = {"token": "installation-secret-marker", "expires_at": exp}
    responses.extend(
        [
            (200, installation, {}),
            (201, grant, {}),
            (200, {"repositories": [REPO]}, {}),
            (200, installation, {}),
            (200, {"repositories": [REPO]}, {}),
            (201, grant, {}),
            (
                200,
                {
                    "type": "file",
                    "encoding": "base64",
                    "size": 5,
                    "content": "aGVsbG8=",
                    "sha": "abc",
                },
                {},
            ),
        ]
    )
    with caplog.at_level(logging.DEBUG):
        assert c.post("/v1/integrations/github/connect", headers=AUTH).status_code == 200
        assert (
            svc.read(
                "owner", "github_get_file", {"repository": REPO["full_name"], "path": "README.md"}
            )["text"]
            == "hello"
        )
    with psycopg.connect(env.url, autocommit=True) as conn:
        tables = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname='public'"
        ).fetchall()
        persisted = "".join(
            str(
                conn.execute(
                    pgsql.SQL("SELECT to_jsonb(t) FROM {} t").format(pgsql.Identifier(table[0]))
                ).fetchall()
            )
            for table in tables
        )
    for marker in ("app-secret-marker", "installation-secret-marker"):
        assert marker not in persisted and marker not in caplog.text
