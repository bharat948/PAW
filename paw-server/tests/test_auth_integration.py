"""Google sign-in, sessions, CSRF, step-up and token modes against a real PostgreSQL (integration)."""

import base64
import hashlib
from urllib.parse import parse_qs, urlparse

import psycopg
import pytest

from paw_server.api.deps import SESSION_COOKIE, StepUpActor
from paw_server.modules.auth.oidc import Identity, IdentityError
from tests.conftest import AUTH, upload

pytestmark = pytest.mark.integration

OWNER = "owner@example.org"
WEB = "http://localhost:5173"
GOOGLE = dict(
    google_client_id="cid.apps.googleusercontent.com",
    google_client_secret="not-a-real-secret",
    allowed_emails=f"{OWNER}, second@example.org",
)


class FakeGoogle:
    """Stands in for Google. Enforces what Google would: PKCE verifier must match the challenge, nonce is echoed."""

    def __init__(self) -> None:
        self.issued: dict[str, dict] = {}
        self.email = OWNER
        self.fail: str | None = None

    def authorize_url(self, *, redirect_uri, state, nonce, code_challenge, prompt):
        self.issued[state] = dict(
            nonce=nonce, challenge=code_challenge, prompt=prompt, redirect_uri=redirect_uri
        )
        return f"https://accounts.google.test/auth?state={state}&prompt={prompt}"

    def identity_from_code(self, *, code, redirect_uri, code_verifier, nonce):
        if self.fail:
            raise IdentityError(self.fail)
        issued = next((v for v in self.issued.values() if v["nonce"] == nonce), None)
        assert issued, "the server sent a nonce Google never issued"
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest())
            .rstrip(b"=")
            .decode()
        )
        assert challenge == issued["challenge"], "PKCE verifier does not match the challenge"
        assert redirect_uri == issued["redirect_uri"]
        return Identity(email=self.email, subject="sub-1")


@pytest.fixture
def g():
    return FakeGoogle()


def make(env, g, **kw):
    kw = {"auth_mode": "google", **GOOGLE, **kw}
    return env.client(idp=g, **kw)


def sign_in(c, g, return_to="/#/ask", email=OWNER):
    g.email = email
    r = c.get("/v1/auth/google/start", params={"return_to": return_to}, follow_redirects=False)
    assert r.status_code == 302, r.text
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    return c.get(
        "/v1/auth/google/callback", params={"code": "ok", "state": state}, follow_redirects=False
    ), state


def csrf(c) -> dict:
    return {"X-CSRF-Token": c.get("/v1/auth/me").json()["csrf_token"]}


def sql(env, q, *args):
    with psycopg.connect(env.url, autocommit=True) as conn:
        cur = conn.execute(q, args)
        return cur.fetchall() if cur.description else None


# ---- config / start ------------------------------------------------------------------------------------------


def test_config_reports_modes_without_secrets(env, g):
    c = make(env, g)
    body = c.get("/v1/auth/config").json()
    assert body == {"mode": "google", "google": True}
    assert "secret" not in c.get("/v1/auth/config").text.lower()
    assert env.client().get("/v1/auth/config").json() == {"mode": "token", "google": False}


def test_start_redirects_to_google_with_state_and_select_account(env, g):
    c = make(env, g)
    r = c.get("/v1/auth/google/start", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith(
        "https://accounts.google.test/auth"
    )
    (issued,) = g.issued.values()
    assert issued["prompt"] == "select_account"
    assert issued["redirect_uri"] == "http://localhost:8000/v1/auth/google/callback"


def test_google_endpoints_are_404_when_google_is_off(env):
    c = env.client()
    assert c.get("/v1/auth/google/start", follow_redirects=False).status_code == 404
    assert c.get("/v1/auth/google/callback", follow_redirects=False).status_code == 404


# ---- the happy path ------------------------------------------------------------------------------------------


def test_full_sign_in_sets_a_hardened_cookie_and_unlocks_the_api(env, g):
    c = make(env, g)
    assert c.get("/v1/documents").status_code == 401
    r, _ = sign_in(c, g, return_to="/#/ask")
    assert r.status_code == 302 and r.headers["location"] == f"{WEB}/#/ask"
    cookie = r.headers["set-cookie"].lower()
    assert f"{SESSION_COOKIE}=" in cookie and "httponly" in cookie and "samesite=lax" in cookie
    assert (
        "secure" not in cookie
    )  # plain http on localhost; Secure is set when api_public_url is https
    assert c.get("/v1/documents").status_code == 200
    me = c.get("/v1/auth/me").json()
    assert (
        me["mode"] == "session"
        and me["email"] == OWNER
        and me["stepup_valid"] is True
        and me["csrf_token"]
    )


def test_cookie_is_secure_over_https(env, g):
    c = make(
        env, g, api_public_url="https://paw.example.org/api", web_url="https://paw.example.org"
    )
    r, _ = sign_in(c, g)
    assert "secure" in r.headers["set-cookie"].lower()


def test_session_cookie_is_stored_only_as_a_hash(env, g):
    c = make(env, g)
    sign_in(c, g)
    raw = c.cookies.get(SESSION_COOKIE)
    ((h, email),) = sql(env, "select token_hash, email from auth_sessions")
    assert bytes(h) == hashlib.sha256(raw.encode()).digest() and email == OWNER
    assert raw not in str(sql(env, "select * from auth_sessions")) and raw.encode() != bytes(h)


# ---- things that must NOT sign you in ------------------------------------------------------------------------


def test_account_not_on_the_allow_list_gets_no_session(env, g):
    c = make(env, g)
    r, _ = sign_in(c, g, email="stranger@example.org")
    assert "auth_error=not_allowed" in r.headers["location"] and "set-cookie" not in r.headers
    assert c.get("/v1/documents").status_code == 401
    assert sql(env, "select count(*) from auth_sessions") == [(0,)]


def test_allow_list_is_case_insensitive(env, g):
    c = make(env, g)
    r, _ = sign_in(c, g, email="OWNER@EXAMPLE.ORG")
    assert "auth_error" not in r.headers["location"] and "set-cookie" in r.headers


@pytest.mark.parametrize(
    "reason", ["invalid_id_token", "nonce_mismatch", "email_not_verified", "keys_unavailable"]
)
def test_unverifiable_google_response_gets_no_session(env, g, reason):
    c = make(env, g)
    g.fail = reason
    r, _ = sign_in(c, g)
    assert "auth_error=failed" in r.headers["location"] and "set-cookie" not in r.headers


def test_state_works_once_only(env, g):
    c = make(env, g)
    _, state = sign_in(c, g)
    c.cookies.clear()
    r = c.get(
        "/v1/auth/google/callback", params={"code": "ok", "state": state}, follow_redirects=False
    )
    assert "auth_error=expired" in r.headers["location"] and "set-cookie" not in r.headers


def test_unknown_or_forged_state_is_rejected(env, g):
    c = make(env, g)
    r = c.get(
        "/v1/auth/google/callback", params={"code": "ok", "state": "forged"}, follow_redirects=False
    )
    assert "auth_error=expired" in r.headers["location"] and "set-cookie" not in r.headers


def test_expired_state_is_rejected(env, g):
    c = make(env, g)
    r = c.get("/v1/auth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    sql(env, "update auth_login_attempts set created_at = now() - interval '11 minutes'")
    r = c.get(
        "/v1/auth/google/callback", params={"code": "ok", "state": state}, follow_redirects=False
    )
    assert "auth_error=expired" in r.headers["location"]


def test_user_cancelling_at_google_is_handled(env, g):
    c = make(env, g)
    r = c.get("/v1/auth/google/callback", params={"error": "access_denied"}, follow_redirects=False)
    assert "auth_error=cancelled" in r.headers["location"] and "set-cookie" not in r.headers


@pytest.mark.parametrize("evil", ["//evil.example", "https://evil.example/x", "/\\evil.example"])
def test_return_to_cannot_redirect_off_site(env, g, evil):
    c = make(env, g)
    r, _ = sign_in(c, g, return_to=evil)
    assert r.headers["location"] == f"{WEB}/"


# ---- CSRF, logout, expiry ------------------------------------------------------------------------------------


def test_cookie_writes_need_the_csrf_token(env, g):
    c = make(env, g)
    sign_in(c, g)
    assert upload(
        c, "a.txt", b"hello world", "text/plain", headers={"Authorization": ""}
    ).status_code in (401, 403)
    no = c.post("/v1/documents", files={"file": ("a.txt", b"hello world", "text/plain")})
    assert no.status_code == 403 and no.json()["code"] == "csrf_failed"
    bad = c.post(
        "/v1/documents",
        files={"file": ("a.txt", b"hello world", "text/plain")},
        headers={"X-CSRF-Token": "x"},
    )
    assert bad.status_code == 403
    ok = c.post(
        "/v1/documents", files={"file": ("a.txt", b"hello world", "text/plain")}, headers=csrf(c)
    )
    assert ok.status_code == 202


def test_logout_revokes_the_session_server_side(env, g):
    c = make(env, g)
    sign_in(c, g)
    stolen = c.cookies.get(SESSION_COOKIE)
    assert c.post("/v1/auth/logout", headers=csrf(c)).status_code == 204
    c2 = make(env, g)
    c2.cookies.set(SESSION_COOKIE, stolen)
    assert c2.get("/v1/documents").status_code == 401  # a copied cookie is dead too
    assert sql(env, "select revoked_at is not null from auth_sessions") == [(True,)]


def test_logout_needs_csrf(env, g):
    c = make(env, g)
    sign_in(c, g)
    assert c.post("/v1/auth/logout").status_code == 403
    assert c.get("/v1/documents").status_code == 200


def test_session_expires(env, g):
    c = make(env, g)
    sign_in(c, g)
    sql(env, "update auth_sessions set expires_at = now() - interval '1 second'")
    assert c.get("/v1/documents").status_code == 401


def test_idle_session_expires(env, g):
    c = make(env, g, session_idle_s=60)
    sign_in(c, g)
    sql(env, "update auth_sessions set last_seen_at = now() - interval '2 minutes'")
    assert c.get("/v1/documents").status_code == 401


def test_activity_keeps_a_session_alive(env, g):
    c = make(env, g, session_idle_s=60)
    sign_in(c, g)
    sql(env, "update auth_sessions set last_seen_at = now() - interval '50 seconds'")
    assert c.get("/v1/documents").status_code == 200
    sql(env, "update auth_sessions set last_seen_at = last_seen_at")  # touched by the request above
    assert sql(env, "select last_seen_at > now() - interval '10 seconds' from auth_sessions") == [
        (True,)
    ]


# ---- step-up -------------------------------------------------------------------------------------------------


def with_sensitive_route(c):
    """A route guarded by the step-up dependency, as 002F's approval endpoint will be."""

    def sensitive(actor: StepUpActor) -> dict[str, bool]:
        return {"ok": True}

    c.app.add_api_route("/_test/sensitive", sensitive, methods=["GET"])
    return c


def test_stale_sign_in_must_step_up_then_passes(env, g):
    c = with_sensitive_route(make(env, g, stepup_window_s=3600))
    sign_in(c, g)
    assert c.get("/_test/sensitive").status_code == 200
    sql(env, "update auth_sessions set authenticated_at = now() - interval '2 hours'")
    r = c.get("/_test/sensitive")
    assert r.status_code == 403 and r.json()["code"] == "stepup_required"
    assert c.get("/v1/auth/me").json()["stepup_valid"] is False

    s = c.get(
        "/v1/auth/google/start",
        params={"purpose": "stepup", "return_to": "/#/approvals"},
        follow_redirects=False,
    )
    assert s.status_code == 302
    (issued,) = [
        v for v in g.issued.values() if v["prompt"] == "login"
    ]  # Google is forced to re-authenticate
    state = [k for k, v in g.issued.items() if v is issued][0]
    back = c.get(
        "/v1/auth/google/callback", params={"code": "ok", "state": state}, follow_redirects=False
    )
    assert back.headers["location"] == f"{WEB}/#/approvals"
    assert c.get("/_test/sensitive").status_code == 200
    assert sql(env, "select count(*) from auth_sessions") == [(1,)]  # same session, not a new one


def test_step_up_needs_an_existing_session(env, g):
    c = make(env, g)
    assert (
        c.get(
            "/v1/auth/google/start", params={"purpose": "stepup"}, follow_redirects=False
        ).status_code
        == 401
    )


def test_step_up_by_a_different_account_is_refused(env, g):
    c = with_sensitive_route(make(env, g, stepup_window_s=3600))
    sign_in(c, g)
    sql(env, "update auth_sessions set authenticated_at = now() - interval '2 hours'")
    c.get("/v1/auth/google/start", params={"purpose": "stepup"}, follow_redirects=False)
    state = [k for k, v in g.issued.items() if v["prompt"] == "login"][0]
    g.email = "second@example.org"  # allow-listed, but not the account that holds this session
    back = c.get(
        "/v1/auth/google/callback", params={"code": "ok", "state": state}, follow_redirects=False
    )
    assert "auth_error=failed" in back.headers["location"]
    assert c.get("/_test/sensitive").status_code == 403


# ---- modes ---------------------------------------------------------------------------------------------------


def test_google_mode_rejects_the_dev_token(env, g):
    c = make(env, g)
    assert c.get("/v1/documents", headers=AUTH).status_code == 401


def test_both_mode_accepts_token_or_cookie(env, g):
    c = make(env, g, auth_mode="both")
    assert c.get("/v1/documents", headers=AUTH).status_code == 200
    assert (
        upload(c, "a.txt", b"hello world", "text/plain").status_code == 202
    )  # token writes need no CSRF
    sign_in(c, g)
    assert c.get("/v1/documents").status_code == 200


def test_token_mode_ignores_session_cookies(env, g):
    c = env.client()
    c.cookies.set(SESSION_COOKIE, "anything")
    assert c.get("/v1/documents").status_code == 401
    assert c.get("/v1/documents", headers=AUTH).status_code == 200
    assert c.get("/v1/auth/me", headers=AUTH).json() == {"mode": "token"}


def test_wrong_bearer_is_401_even_with_a_valid_cookie(env, g):
    c = make(env, g, auth_mode="both")
    sign_in(c, g)
    assert c.get("/v1/documents", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_stepup_passes_for_the_dev_token(env, g):
    c = with_sensitive_route(make(env, g, auth_mode="both"))
    assert c.get("/_test/sensitive", headers=AUTH).status_code == 200


# ---- browser plumbing ----------------------------------------------------------------------------------------


def test_cors_allows_credentials_and_csrf_header_for_the_web_origin(env, g):
    c = make(env, g, cors_origins=WEB)
    r = c.options(
        "/v1/documents",
        headers={
            "Origin": WEB,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "x-csrf-token",
        },
    )
    assert r.headers["access-control-allow-credentials"] == "true"
    assert r.headers["access-control-allow-origin"] == WEB
    assert "x-csrf-token" in r.headers["access-control-allow-headers"].lower()
    other = c.options(
        "/v1/documents",
        headers={"Origin": "http://evil.example", "Access-Control-Request-Method": "POST"},
    )
    assert "access-control-allow-origin" not in other.headers
