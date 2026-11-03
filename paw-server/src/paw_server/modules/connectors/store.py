"""Connections and their encrypted secrets. Plaintext tokens exist only in memory, never in rows or logs."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from cryptography.fernet import Fernet, InvalidToken

from paw_server.modules.ingestion.jobs import Conn

KEY_ID = "k1"


@dataclass
class Connection:
    id: UUID
    provider: str
    account_email: str
    scopes: list[str]
    status: str
    created_at: datetime


def seal(key: str, payload: dict[str, Any]) -> bytes:
    return Fernet(key.encode()).encrypt(json.dumps(payload).encode())


def unseal(key: str, blob: bytes) -> dict[str, Any] | None:
    try:
        data: dict[str, Any] = json.loads(Fernet(key.encode()).decrypt(bytes(blob)))
        return data
    except (InvalidToken, ValueError):
        return None  # wrong or rotated key: treated as "needs reconnect", never a crash


def get_live(conn: Conn, owner: str, provider: str) -> Connection | None:
    row = conn.execute(
        "SELECT id, provider, account_email, scopes, status, created_at FROM connector_connections "
        "WHERE owner_actor = %s AND provider = %s AND status <> 'revoked'",
        (owner, provider),
    ).fetchone()
    conn.commit()
    return Connection(**row) if row else None


def save_connection(
    conn: Conn,
    key: str,
    *,
    owner: str,
    provider: str,
    email: str,
    scopes: list[str],
    secrets: dict[str, Any],
) -> UUID:
    """Replaces any live connection for this owner and provider, atomically."""
    conn.execute(
        "UPDATE connector_connections SET status = 'revoked', revoked_at = now(), updated_at = now() "
        "WHERE owner_actor = %s AND provider = %s AND status <> 'revoked'",
        (owner, provider),
    )
    row = conn.execute(
        "INSERT INTO connector_connections (provider, owner_actor, account_email, scopes) "
        "VALUES (%s, %s, %s, %s) RETURNING id",
        (provider, owner, email, scopes),
    ).fetchone()
    assert row is not None
    conn.execute(
        "INSERT INTO connector_secrets (connection_id, ciphertext, key_id) VALUES (%s, %s, %s)",
        (row["id"], seal(key, secrets), KEY_ID),
    )
    conn.commit()
    new_id: UUID = row["id"]
    return new_id


def load_secrets(conn: Conn, key: str, connection_id: UUID) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT ciphertext FROM connector_secrets WHERE connection_id = %s", (connection_id,)
    ).fetchone()
    conn.commit()
    return unseal(key, row["ciphertext"]) if row else None


def update_secrets(conn: Conn, key: str, connection_id: UUID, secrets: dict[str, Any]) -> None:
    conn.execute(
        "UPDATE connector_secrets SET ciphertext = %s, updated_at = now() WHERE connection_id = %s",
        (seal(key, secrets), connection_id),
    )
    conn.commit()


def set_status(conn: Conn, connection_id: UUID, status: str, error: str | None = None) -> None:
    conn.execute(
        "UPDATE connector_connections SET status = %s, last_error = %s, updated_at = now(), "
        "revoked_at = CASE WHEN %s = 'revoked' THEN now() END WHERE id = %s",
        (status, error, status, connection_id),
    )
    if status == "revoked":
        conn.execute("DELETE FROM connector_secrets WHERE connection_id = %s", (connection_id,))
    conn.commit()
