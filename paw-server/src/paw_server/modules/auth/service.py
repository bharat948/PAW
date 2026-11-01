"""Server-side login sessions. The browser holds only an opaque random cookie; we store its sha256.

Sessions are revocable (logout, expiry, idle timeout) and carry a CSRF token for cookie-authenticated writes.
`authenticated_at` is the last real Google sign-in and drives the step-up window for sensitive actions.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from paw_server.core.config import Settings
from paw_server.modules.ingestion.jobs import Conn

ATTEMPT_TTL_S = 600
OWNER = "owner"


def _h(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def safe_return_to(value: str | None) -> str:
    """Only same-site relative paths: blocks open redirects such as //evil.com or https://evil.com."""
    v = (value or "").strip()
    if not v.startswith("/") or v.startswith("//") or "\\" in v or len(v) > 200:
        return "/"
    if any(ord(c) < 32 for c in v) or "://" in v:
        return "/"
    return v


@dataclass
class Session:
    id: UUID
    email: str
    csrf_token: str
    expires_at: datetime
    authenticated_at: datetime

    def stepup_valid(self, settings: Settings, now: datetime) -> bool:
        return (now - self.authenticated_at).total_seconds() <= settings.stepup_window_s


@dataclass
class Attempt:
    nonce: str
    code_verifier: str
    purpose: str
    return_to: str
    session_id: UUID | None


def start_attempt(
    conn: Conn, *, purpose: str, return_to: str, session_id: UUID | None, verifier: str
) -> tuple[str, str]:
    """Returns (state, nonce). The state is single use and expires after ATTEMPT_TTL_S."""
    state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO auth_login_attempts (state_hash, nonce, code_verifier, purpose, return_to, session_id) "
        "VALUES (%s, %s, %s, %s, %s, %s)",
        (_h(state), nonce, verifier, purpose, safe_return_to(return_to), session_id),
    )
    conn.execute(
        "DELETE FROM auth_login_attempts WHERE created_at < now() - interval '1 day'"
    )  # housekeeping
    conn.commit()
    return state, nonce


def consume_attempt(conn: Conn, state: str) -> Attempt | None:
    """Atomic: a state works once and only while fresh. A replay or unknown state returns None."""
    row = conn.execute(
        "UPDATE auth_login_attempts SET used_at = now() WHERE state_hash = %s AND used_at IS NULL "
        "AND created_at > now() - make_interval(secs => %s) "
        "RETURNING nonce, code_verifier, purpose, return_to, session_id",
        (_h(state), ATTEMPT_TTL_S),
    ).fetchone()
    conn.commit()
    return Attempt(**row) if row else None


def create_session(conn: Conn, settings: Settings, email: str) -> tuple[str, Session]:
    token, csrf = secrets.token_urlsafe(48), secrets.token_urlsafe(32)
    row = conn.execute(
        "INSERT INTO auth_sessions (token_hash, csrf_token, actor_id, email, expires_at, authenticated_at) "
        "VALUES (%s, %s, %s, %s, now() + make_interval(secs => %s), now()) "
        "RETURNING id, email, csrf_token, expires_at, authenticated_at",
        (_h(token), csrf, OWNER, email, settings.session_ttl_s),
    ).fetchone()
    conn.commit()
    assert row is not None
    return token, Session(**row)


def get_session(conn: Conn, settings: Settings, token: str) -> Session | None:
    """Valid = not revoked, not past its absolute expiry, not idle too long. Touches last_seen_at."""
    row = conn.execute(
        "UPDATE auth_sessions SET last_seen_at = now() WHERE token_hash = %s AND revoked_at IS NULL "
        "AND expires_at > now() AND last_seen_at > now() - make_interval(secs => %s) "
        "RETURNING id, email, csrf_token, expires_at, authenticated_at",
        (_h(token), settings.session_idle_s),
    ).fetchone()
    conn.commit()
    return Session(**row) if row else None


def revoke(conn: Conn, session_id: UUID) -> None:
    conn.execute(
        "UPDATE auth_sessions SET revoked_at = now() WHERE id = %s AND revoked_at IS NULL",
        (session_id,),
    )
    conn.commit()


def mark_stepup(conn: Conn, session_id: UUID) -> bool:
    row = conn.execute(
        "UPDATE auth_sessions SET authenticated_at = now() WHERE id = %s AND revoked_at IS NULL "
        "AND expires_at > now() RETURNING id",
        (session_id,),
    ).fetchone()
    conn.commit()
    return row is not None
