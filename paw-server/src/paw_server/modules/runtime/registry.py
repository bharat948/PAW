"""Operator-owned runtime identities. Inbound tokens are hashed; outbound secrets stay in the environment.

This module never creates actors or containers. A worker is assigned an existing runtime;
the gateway derives its assignment from a live credential, not a request body.
"""

import hashlib
import os
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

from paw_server.core.config import Settings
from paw_server.modules.ingestion.jobs import Conn
from paw_server.modules.runtime.hermes_adapter import HermesAdapter


class RegistryError(ValueError):
    pass


@dataclass(frozen=True)
class RuntimeIdentity:
    id: UUID
    name: str
    actor_id: str
    credential_id: UUID
    purpose: str


def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def register(
    conn: Conn,
    name: str,
    actor: str,
    *,
    endpoint_url: str | None = None,
    key_env: str | None = None,
) -> UUID:
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", name):
        raise RegistryError("invalid runtime name")
    if endpoint_url is not None:
        url = urlsplit(endpoint_url)
        if (
            url.scheme not in ("http", "https")
            or not url.hostname
            or url.username
            or url.password
            or url.query
            or url.fragment
        ):
            raise RegistryError("invalid runtime endpoint")
    if key_env is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", key_env):
        raise RegistryError("invalid secret environment reference")
    with conn.transaction():
        a = conn.execute(
            "SELECT kind, status FROM actors WHERE id=%s FOR UPDATE", (actor,)
        ).fetchone()
        if not a or a["kind"] != "agent" or a["status"] != "active":
            raise RegistryError("active agent required")
        old = conn.execute(
            "SELECT * FROM agent_runtimes WHERE name=%s FOR UPDATE", (name,)
        ).fetchone()
        if old and old["actor_id"] != actor:
            raise RegistryError("runtime already belongs to another actor")
        if old and (
            (endpoint_url is not None and old["endpoint_url"] != endpoint_url)
            or (key_env is not None and old["key_env"] != key_env)
        ):
            if conn.execute(
                "SELECT 1 FROM agent_run_leases WHERE runtime_id=%s", (old["id"],)
            ).fetchone():
                raise RegistryError("drain runtime before changing its endpoint")
        p = conn.execute(
            "SELECT id FROM actor_profile_versions WHERE actor_id=%s AND status='active'", (actor,)
        ).fetchone()
        row = conn.execute(
            "INSERT INTO agent_runtimes(name,actor_id,endpoint_url,key_env,desired_profile_version_id) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT(name) DO UPDATE SET "
            "endpoint_url=coalesce(excluded.endpoint_url,agent_runtimes.endpoint_url), "
            "key_env=coalesce(excluded.key_env,agent_runtimes.key_env), "
            "desired_profile_version_id=excluded.desired_profile_version_id RETURNING id",
            (name, actor, endpoint_url, key_env, p["id"] if p else None),
        ).fetchone()
        assert row is not None
        return row["id"]  # type: ignore[no-any-return]


def credential(
    conn: Conn, runtime_id: UUID, purpose: str, token: str, *, rotate: bool = False
) -> None:
    """Idempotent bootstrap never revives a revoked hash. Rotation requires a new token."""
    if purpose not in ("gateway", "model") or len(token) < 24 or len(token) > 1024:
        raise RegistryError("invalid runtime credential")
    with conn.transaction():
        conn.execute("SELECT 1 FROM agent_runtimes WHERE id=%s FOR UPDATE", (runtime_id,))
        old = conn.execute(
            "SELECT * FROM runtime_credentials WHERE token_hash=%s", (digest(token),)
        ).fetchone()
        if old:
            if old["runtime_id"] != runtime_id or old["purpose"] != purpose:
                raise RegistryError("credential already assigned")
            if old["revoked_at"] is not None and rotate:
                raise RegistryError("use a fresh token for rotation")
            return
        live = conn.execute(
            "SELECT id FROM runtime_credentials WHERE runtime_id=%s AND purpose=%s AND revoked_at IS NULL",
            (runtime_id, purpose),
        ).fetchone()
        if live and not rotate:
            raise RegistryError("explicit credential rotation required")
        if live:
            conn.execute(
                "UPDATE runtime_credentials SET revoked_at=now() WHERE id=%s", (live["id"],)
            )
        conn.execute(
            "INSERT INTO runtime_credentials(runtime_id,purpose,token_hash) VALUES (%s,%s,%s)",
            (runtime_id, purpose, digest(token)),
        )


def authenticate(conn: Conn, token: str, purpose: str) -> RuntimeIdentity | None:
    row = conn.execute(
        "SELECT rt.id, rt.name, rt.actor_id, c.id AS credential_id, c.purpose "
        "FROM runtime_credentials c JOIN agent_runtimes rt ON rt.id=c.runtime_id "
        "JOIN actors a ON a.id=rt.actor_id WHERE c.token_hash=%s AND c.purpose=%s "
        "AND c.revoked_at IS NULL AND rt.desired_state='running' AND a.status='active' AND a.kind='agent'",
        (digest(token), purpose),
    ).fetchone()
    conn.commit()
    return RuntimeIdentity(**row) if row else None


def valid(conn: Conn, runtime: RuntimeIdentity) -> bool:
    row = conn.execute(
        "SELECT 1 FROM runtime_credentials c JOIN agent_runtimes rt ON rt.id=c.runtime_id "
        "JOIN actors a ON a.id=rt.actor_id WHERE c.id=%s AND c.runtime_id=%s AND c.purpose=%s "
        "AND c.revoked_at IS NULL AND rt.desired_state='running' AND a.status='active' AND a.kind='agent'",
        (runtime.credential_id, runtime.id, runtime.purpose),
    ).fetchone()
    return row is not None


def stop(conn: Conn, runtime_id: UUID) -> None:
    with conn.transaction():
        conn.execute(
            "UPDATE agent_runtimes SET desired_state='stopped',status='down' WHERE id=%s",
            (runtime_id,),
        )
        conn.execute(
            "UPDATE runtime_credentials SET revoked_at=now() WHERE runtime_id=%s AND revoked_at IS NULL",
            (runtime_id,),
        )


def bootstrap(conn: Conn, settings: Settings) -> UUID:
    """Only the legacy Admin configuration is imported; other runtimes must be provisioned explicitly."""
    rid = register(
        conn,
        settings.gateway_runtime,
        "admin-hermes",
        endpoint_url=settings.hermes_url or None,
        key_env="PAW_HERMES_KEY" if settings.hermes_key else None,
    )
    for purpose, token in (
        ("gateway", settings.gateway_token),
        ("model", settings.model_proxy_token),
    ):
        if token:
            credential(conn, rid, purpose, token)
    return rid


def adapter(conn: Conn, runtime_id: UUID, settings: Settings) -> HermesAdapter:
    row = conn.execute("SELECT * FROM agent_runtimes WHERE id=%s", (runtime_id,)).fetchone()
    conn.commit()
    if not row or not row["endpoint_url"] or not row["key_env"]:
        raise RegistryError("runtime endpoint is not configured")
    key = (
        settings.hermes_key
        if row["actor_id"] == "admin-hermes" and row["key_env"] == "PAW_HERMES_KEY"
        else os.environ.get(row["key_env"], "")
    )
    if not key:
        raise RegistryError("runtime secret is unavailable")
    return HermesAdapter(row["endpoint_url"], key)


def for_run(conn: Conn, run_id: UUID, settings: Settings, admin: Any = None) -> Any:
    row = conn.execute(
        "SELECT rt.id, rt.actor_id FROM agent_runs r JOIN agent_runtimes rt ON rt.id=r.runtime_id WHERE r.id=%s",
        (run_id,),
    ).fetchone()
    conn.commit()
    if not row:
        raise RegistryError("run has no runtime")
    # Dependency injection for the existing Admin browser/recovery harness.
    if admin is not None and row["actor_id"] == "admin-hermes":
        return admin
    return adapter(conn, row["id"], settings)
