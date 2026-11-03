"""Google Calendar connector: connect flow, encrypted secrets, refresh, revoke, failure modes (integration)."""

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from cryptography.fernet import Fernet

from paw_server.modules.connectors import google_calendar as gc
from paw_server.modules.connectors import store
from paw_server.modules.connectors.google_calendar import ConnectorError, Grant
from tests.test_auth_integration import GOOGLE, OWNER, FakeGoogle, csrf, sign_in, sql

pytestmark = pytest.mark.integration
KEY = Fernet.generate_key().decode()
SECRET_MARKERS = ("refresh-secret-123", "access-secret-456")


class FakeCalendar:
    def __init__(self) -> None:
        self.issued: dict[str, dict] = {}
        self.email = "cal@example.org"
        self.fail_exchange: str | None = None
        self.fail_refresh: str | None = None
        self.fail_events: str | None = None
        self.expires_in = 3600
        self.refreshes = 0
        self.revoked: list[str] = []
        self.seen_tokens: list[str] = []

    def authorize_url(self, *, redirect_uri, state, nonce, code_challenge):
        self.issued[state] = dict(nonce=nonce, challenge=code_challenge, redirect_uri=redirect_uri)
        return f"https://accounts.google.test/auth?state={state}"

    def exchange_code(self, *, code, redirect_uri, code_verifier, nonce):
        if self.fail_exchange:
            raise ConnectorError(self.fail_exchange)
        issued = next(v for v in self.issued.values() if v["nonce"] == nonce)
        chal = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode()).digest()).rstrip(
            b"="
        )
        assert chal.decode() == issued["challenge"]
        assert redirect_uri == issued["redirect_uri"]
        return Grant(
            email=self.email,
            refresh_token=SECRET_MARKERS[0],
            access_token=SECRET_MARKERS[1],
            expires_at=datetime.now(UTC) + timedelta(seconds=self.expires_in),
            scopes=gc.SCOPES,
        )

    def refresh(self, refresh_token):
        if self.fail_refresh:
            raise ConnectorError(self.fail_refresh)
        self.refreshes += 1
        return f"fresh-token-{self.refreshes}", datetime.now(UTC) + timedelta(hours=1)

    def list_events(self, access_token, *, time_min, time_max, limit):
        self.seen_tokens.append(access_token)
        if self.fail_events:
            raise ConnectorError(self.fail_events)
        return [{"id": "e1", "title": "Standup", "start": "2026-11-04T09:00:00Z"}][:limit]

    def revoke(self, token):
        self.revoked.append(token)


@pytest.fixture
def cal():
    return FakeCalendar()


def make(env, cal, **kw):
    kw = {"auth_mode": "google", **GOOGLE, "connector_key": KEY, **kw}
    g = FakeGoogle()
    c = env.client(idp=g, calendar=cal, **kw)
    sign_in(c, g)
    return c


def connect(c, cal, return_to="/#/integrations"):
    r = c.get(
        "/v1/integrations/google-calendar/connect",
        params={"return_to": return_to},
        follow_redirects=False,
    )
    assert r.status_code == 302, r.text
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    return c.get(
        "/v1/integrations/google-calendar/callback",
        params={"code": "ok", "state": state},
        follow_redirects=False,
    ), state


def test_catalog_before_and_after_connect(env, cal):
    c = make(env, cal)
    items = {i["id"]: i for i in c.get("/v1/integrations").json()["integrations"]}
    assert items["google_calendar"]["state"] == "available"
    assert items["documents"]["state"] == "connected"
    r, _ = connect(c, cal)
    assert r.status_code == 302 and r.headers["location"] == "http://localhost:5173/#/integrations"
    items = {i["id"]: i for i in c.get("/v1/integrations").json()["integrations"]}
    assert items["google_calendar"]["state"] == "connected"
    assert items["google_calendar"]["account_email"] == "cal@example.org"
    assert items["google_calendar"]["tools"] == [{"name": "calendar_list_events", "access": "read"}]


def test_unavailable_without_connector_key(env):
    c = env.client(idp=FakeGoogle(), auth_mode="google", **GOOGLE)  # no calendar provider
    sign_in(c, c.app.state.idp)
    item = [
        i for i in c.get("/v1/integrations").json()["integrations"] if i["id"] == "google_calendar"
    ][0]
    assert item["state"] == "unavailable"
    assert (
        c.get("/v1/integrations/google-calendar/connect", follow_redirects=False).status_code == 404
    )


def test_connect_requires_a_signed_in_session(env, cal):
    c = env.client(idp=FakeGoogle(), calendar=cal, auth_mode="google", connector_key=KEY, **GOOGLE)
    assert (
        c.get("/v1/integrations/google-calendar/connect", follow_redirects=False).status_code == 401
    )
    assert c.get("/v1/integrations").status_code == 401
    assert c.get("/v1/integrations/google-calendar/events").status_code == 401


def test_tokens_are_encrypted_at_rest_and_not_logged(env, cal, caplog):
    c = make(env, cal)
    connect(c, cal)
    (blob,) = sql(env, "SELECT ciphertext FROM connector_secrets")[0]
    raw = bytes(blob)
    assert not any(m.encode() in raw for m in SECRET_MARKERS)
    dump = " ".join(
        str(r)
        for t in ("connector_connections", "auth_login_attempts")
        for r in sql(env, f"SELECT * FROM {t}")
    )
    assert not any(m in dump for m in SECRET_MARKERS)
    assert store.unseal(KEY, raw)["refresh_token"] == SECRET_MARKERS[0]
    assert store.unseal(Fernet.generate_key().decode(), raw) is None  # wrong key never decrypts
    assert not any(m in caplog.text for m in SECRET_MARKERS)


def test_state_is_single_use_and_must_be_the_connect_purpose(env, cal):
    c = make(env, cal)
    r, state = connect(c, cal)
    again = c.get(
        "/v1/integrations/google-calendar/callback",
        params={"code": "ok", "state": state},
        follow_redirects=False,
    )
    assert "integration_error=expired" in again.headers["location"]
    bogus = c.get(
        "/v1/integrations/google-calendar/callback",
        params={"code": "ok", "state": "nope"},
        follow_redirects=False,
    )
    assert "integration_error=expired" in bogus.headers["location"]
    # a login-purpose state cannot be replayed into the connect callback
    sess_state = parse_qs(
        urlparse(c.get("/v1/auth/google/start", follow_redirects=False).headers["location"]).query
    )["state"][0]
    swapped = c.get(
        "/v1/integrations/google-calendar/callback",
        params={"code": "ok", "state": sess_state},
        follow_redirects=False,
    )
    assert "integration_error=expired" in swapped.headers["location"]


def test_other_browser_cannot_finish_my_connect(env, cal):
    a = make(env, cal)
    r = a.get("/v1/integrations/google-calendar/connect", follow_redirects=False)
    state = parse_qs(urlparse(r.headers["location"]).query)["state"][0]
    b = make(env, cal)  # a different signed-in session
    res = b.get(
        "/v1/integrations/google-calendar/callback",
        params={"code": "ok", "state": state},
        follow_redirects=False,
    )
    assert "integration_error=failed" in res.headers["location"]
    assert sql(env, "SELECT count(*) FROM connector_connections") == [(0,)]


@pytest.mark.parametrize(
    ("code", "expected"),
    [("missing_scope", "missing_scope"), ("rejected", "failed"), ("unreachable", "failed")],
)
def test_exchange_failures_redirect_with_a_safe_error(env, cal, code, expected):
    c = make(env, cal)
    cal.fail_exchange = code
    r, _ = connect(c, cal)
    assert f"integration_error={expected}" in r.headers["location"]
    assert sql(env, "SELECT count(*) FROM connector_connections") == [(0,)]


def test_user_cancelling_consent(env, cal):
    c = make(env, cal)
    r = c.get(
        "/v1/integrations/google-calendar/callback",
        params={"error": "access_denied"},
        follow_redirects=False,
    )
    assert "integration_error=cancelled" in r.headers["location"]


def test_events_use_cached_token_then_refresh_when_expired(env, cal):
    c = make(env, cal)
    connect(c, cal)
    body = c.get("/v1/integrations/google-calendar/events").json()
    assert body["events"][0]["title"] == "Standup" and body["account_email"] == "cal@example.org"
    assert cal.seen_tokens == [SECRET_MARKERS[1]] and cal.refreshes == 0
    cal.expires_in = 1  # a token inside the 60 s margin counts as expired
    connect(c, cal)
    c.get("/v1/integrations/google-calendar/events")
    assert cal.refreshes == 1 and cal.seen_tokens[-1] == "fresh-token-1"
    c.get("/v1/integrations/google-calendar/events")
    assert (
        cal.refreshes == 1 and cal.seen_tokens[-1] == "fresh-token-1"
    )  # refreshed token is cached


def test_google_revoking_access_marks_needs_reauth(env, cal):
    c = make(env, cal)
    cal.expires_in = 1
    connect(c, cal)
    cal.fail_refresh = "revoked"
    r = c.get("/v1/integrations/google-calendar/events")
    assert r.status_code == 409 and r.json()["code"] == "needs_reauth"
    item = [
        i for i in c.get("/v1/integrations").json()["integrations"] if i["id"] == "google_calendar"
    ][0]
    assert item["state"] == "needs_reauth"
    assert c.get("/v1/integrations/google-calendar/events").json()["code"] == "needs_reauth"
    cal.fail_refresh = None
    connect(c, cal)  # reconnecting replaces the old connection
    assert c.get("/v1/integrations/google-calendar/events").status_code == 200
    assert sql(env, "SELECT status FROM connector_connections ORDER BY created_at") == [
        ("revoked",),
        ("active",),
    ]


def test_calendar_outage_is_503_not_a_disconnect(env, cal):
    c = make(env, cal)
    connect(c, cal)
    cal.fail_events = "unreachable"
    r = c.get("/v1/integrations/google-calendar/events")
    assert r.status_code == 503
    assert sql(env, "SELECT status FROM connector_connections") == [("active",)]


def test_unreadable_secret_asks_to_reconnect(env, cal):
    c = make(env, cal)
    connect(c, cal)
    sql(env, "UPDATE connector_secrets SET ciphertext = '\\x00'")
    r = c.get("/v1/integrations/google-calendar/events")
    assert r.status_code == 409 and r.json()["code"] == "needs_reauth"


def test_events_before_connecting(env, cal):
    c = make(env, cal)
    assert c.get("/v1/integrations/google-calendar/events").json()["code"] == "not_connected"


def test_disconnect_revokes_at_google_and_deletes_secrets(env, cal):
    c = make(env, cal)
    connect(c, cal)
    assert c.delete("/v1/integrations/google-calendar", headers=csrf(c)).status_code == 204
    assert cal.revoked == [SECRET_MARKERS[0]]
    assert sql(env, "SELECT count(*) FROM connector_secrets") == [(0,)]
    assert sql(env, "SELECT status FROM connector_connections") == [("revoked",)]
    assert c.get("/v1/integrations/google-calendar/events").json()["code"] == "not_connected"
    assert c.delete("/v1/integrations/google-calendar", headers=csrf(c)).status_code == 404


def test_disconnect_needs_csrf(env, cal):
    c = make(env, cal)
    connect(c, cal)
    assert c.delete("/v1/integrations/google-calendar").status_code == 403
    assert sql(env, "SELECT status FROM connector_connections") == [("active",)]


def test_login_session_is_independent_of_the_calendar_connection(env, cal):
    c = make(env, cal)
    connect(c, cal)
    c.delete("/v1/integrations/google-calendar", headers=csrf(c))
    assert c.get("/v1/auth/me").json()["email"] == OWNER


# ---- the real provider against a mocked Google ----------------------------------------------------------------


class StubIdentity:
    def verify_id_token(self, token, nonce):
        from paw_server.modules.auth.oidc import Identity

        return Identity(email="cal@example.org", subject="s")


def provider(handler):
    return gc.GoogleCalendarProvider(
        "cid",
        "sec",
        identity=StubIdentity(),
        http=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_authorize_url_requests_readonly_offline_access_with_pkce():
    q = parse_qs(
        urlparse(
            provider(lambda r: httpx.Response(500)).authorize_url(
                redirect_uri="http://x/cb", state="s", nonce="n", code_challenge="c"
            )
        ).query
    )
    assert q["scope"] == ["openid email https://www.googleapis.com/auth/calendar.readonly"]
    assert q["access_type"] == ["offline"] and q["prompt"] == ["consent"]
    assert q["code_challenge_method"] == ["S256"]


def _token_body(**kw):
    return {
        "access_token": "a",
        "refresh_token": "r",
        "expires_in": 3600,
        "id_token": "i",
        "scope": " ".join(gc.SCOPES),
        **kw,
    }


def test_exchange_rejects_a_grant_without_calendar_scope_or_refresh_token():
    p = provider(lambda r: httpx.Response(200, json=_token_body(scope="openid email")))
    with pytest.raises(ConnectorError) as e:
        p.exchange_code(code="c", redirect_uri="u", code_verifier="v", nonce="n")
    assert e.value.code == "missing_scope"
    body = _token_body()
    del body["refresh_token"]
    p = provider(lambda r: httpx.Response(200, json=body))
    with pytest.raises(ConnectorError) as e:
        p.exchange_code(code="c", redirect_uri="u", code_verifier="v", nonce="n")
    assert e.value.code == "rejected"


def test_exchange_success_and_refresh_invalid_grant():
    p = provider(lambda r: httpx.Response(200, json=_token_body()))
    g = p.exchange_code(code="c", redirect_uri="u", code_verifier="v", nonce="n")
    assert g.email == "cal@example.org" and g.refresh_token == "r"
    p = provider(lambda r: httpx.Response(400, json={"error": "invalid_grant"}))
    with pytest.raises(ConnectorError) as e:
        p.refresh("r")
    assert e.value.code == "revoked"


def test_list_events_shapes_and_minimises_output():
    seen = {}

    def handler(r: httpx.Request):
        seen["auth"] = r.headers["authorization"]
        seen["q"] = dict(r.url.params)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "1",
                        "summary": "Lunch",
                        "start": {"dateTime": "2026-11-04T12:00:00Z"},
                        "end": {"dateTime": "2026-11-04T13:00:00Z"},
                        "description": "SECRET AGENDA",
                        "attendees": [{"email": "x@y"}],
                        "htmlLink": "l",
                    },
                    {"id": "2", "start": {"date": "2026-11-05"}, "end": {"date": "2026-11-06"}},
                ]
            },
        )

    now = datetime.now(UTC)
    items = provider(handler).list_events(
        "tok", time_min=now, time_max=now + timedelta(days=1), limit=5
    )
    assert seen["auth"] == "Bearer tok" and seen["q"]["singleEvents"] == "true"
    assert (
        items[0]["title"] == "Lunch"
        and "SECRET AGENDA" not in str(items)
        and "attendees" not in items[0]
    )
    assert items[1]["all_day"] is True and items[1]["title"] == "(no title)"


@pytest.mark.parametrize(
    ("status", "code"), [(401, "revoked"), (403, "revoked"), (500, "rejected")]
)
def test_list_events_error_mapping(status, code):
    now = datetime.now(UTC)
    with pytest.raises(ConnectorError) as e:
        provider(lambda r: httpx.Response(status)).list_events(
            "t", time_min=now, time_max=now, limit=1
        )
    assert e.value.code == code


def test_errors_land_on_the_integrations_page_with_the_error_in_the_query(env, cal):
    c = make(env, cal)
    r = c.get(
        "/v1/integrations/google-calendar/callback",
        params={"error": "access_denied"},
        follow_redirects=False,
    )
    assert (
        r.headers["location"] == "http://localhost:5173/?integration_error=cancelled#/integrations"
    )
