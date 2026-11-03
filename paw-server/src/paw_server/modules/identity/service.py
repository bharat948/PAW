"""Profile versions in PostgreSQL. Versions are immutable (a trigger enforces it); creating one compiles it, and
activation only flips which compiled version is current. Only a human actor can create or activate versions
(002C-R05): there is no route by which an agent can reach this code, and the service refuses agent ids as well."""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from paw_server.modules.identity.compiler import (
    COMPILER_VERSION,
    Compiled,
    compile_profile,
    spec_hash,
)
from paw_server.modules.identity.spec import DEFAULT_ADMIN_SPEC, ActorProfileSpec
from paw_server.modules.ingestion.jobs import Conn

ADMIN = "admin-hermes"


class ProfileError(Exception):
    """`code`: agent_not_allowed · unknown_version · not_draft · wrong_actor · no_such_actor."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class ActiveProfile:
    version_id: UUID
    actor_id: str
    version: int
    spec: ActorProfileSpec
    compiled: Compiled


def _require_human(conn: Conn, actor_id: str) -> None:
    row = conn.execute("SELECT kind, status FROM actors WHERE id = %s", (actor_id,)).fetchone()
    if row is None or row["kind"] != "human" or row["status"] != "active":
        raise ProfileError("agent_not_allowed")


def _compiled_from(row: dict[str, Any]) -> Compiled:
    return Compiled(
        instructions_text=row["instructions_text"],
        constraints_text=row["constraints_text"],
        config_fragment=row["config_fragment"],
        policy=row["policy"],
        compiled_hash=row["compiled_hash"],
        compiler_version=row["compiler_version"],
    )


def create_version(
    conn: Conn, actor_id: str, spec: ActorProfileSpec, created_by: str
) -> dict[str, Any]:
    """A new draft, compiled immediately so activation is only a flip. Version numbers are contiguous per actor."""
    with conn.transaction():
        _require_human(conn, created_by)
        if (
            conn.execute("SELECT 1 FROM actors WHERE id = %s FOR UPDATE", (actor_id,)).fetchone()
            is None
        ):
            raise ProfileError("no_such_actor")
        nxt = conn.execute(
            "SELECT coalesce(max(version), 0) + 1 AS v FROM actor_profile_versions WHERE actor_id = %s",
            (actor_id,),
        ).fetchone()
        assert nxt is not None
        version: int = nxt["v"]
        row = conn.execute(
            "INSERT INTO actor_profile_versions (actor_id, version, spec, spec_hash, created_by) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING id, version, status, created_at",
            (actor_id, version, Jsonb(spec.model_dump(mode="json")), spec_hash(spec), created_by),
        ).fetchone()
        assert row is not None
        c = compile_profile(actor_id, version, spec)
        conn.execute(
            "INSERT INTO actor_compiled_profiles (profile_version_id, instructions_text, config_fragment, "
            "constraints_text, policy, compiled_hash, compiler_version) VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (
                row["id"], c.instructions_text, Jsonb(c.config_fragment), c.constraints_text,
                Jsonb(c.policy), c.compiled_hash, COMPILER_VERSION,
            ),
        )  # fmt: skip
    return row


def activate(conn: Conn, actor_id: str, version_id: UUID, by: str) -> ActiveProfile:
    """Retire the current version and activate this draft, atomically. In-flight runs keep the version they bound at
    claim time, so nothing already running changes."""
    with conn.transaction():
        _require_human(conn, by)
        conn.execute("SELECT 1 FROM actors WHERE id = %s FOR UPDATE", (actor_id,))
        target = conn.execute(
            "SELECT actor_id, status FROM actor_profile_versions WHERE id = %s FOR UPDATE",
            (version_id,),
        ).fetchone()
        if target is None:
            raise ProfileError("unknown_version")
        if target["actor_id"] != actor_id:
            raise ProfileError("wrong_actor")
        if target["status"] != "draft":
            raise ProfileError("not_draft")
        conn.execute(
            "UPDATE actor_profile_versions SET status = 'retired' WHERE actor_id = %s AND status = 'active'",
            (actor_id,),
        )
        conn.execute(
            "UPDATE actor_profile_versions SET status = 'active', activated_at = now() WHERE id = %s",
            (version_id,),
        )
    active = get_active(conn, actor_id)
    assert active is not None
    return active


def get_active(conn: Conn, actor_id: str) -> ActiveProfile | None:
    row = conn.execute(
        "SELECT v.id, v.actor_id, v.version, v.spec, c.* FROM actor_profile_versions v "
        "JOIN actor_compiled_profiles c ON c.profile_version_id = v.id "
        "WHERE v.actor_id = %s AND v.status = 'active'",
        (actor_id,),
    ).fetchone()
    conn.commit()
    if row is None:
        return None
    return ActiveProfile(
        version_id=row["id"],
        actor_id=row["actor_id"],
        version=row["version"],
        spec=ActorProfileSpec.model_validate(row["spec"]),
        compiled=_compiled_from(row),
    )


def list_versions(conn: Conn, actor_id: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT v.id, v.version, v.status, v.spec, v.spec_hash, v.created_by, v.created_at, v.activated_at, "
        "c.compiled_hash FROM actor_profile_versions v "
        "LEFT JOIN actor_compiled_profiles c ON c.profile_version_id = v.id "
        "WHERE v.actor_id = %s ORDER BY v.version DESC",
        (actor_id,),
    ).fetchall()
    conn.commit()
    return rows


def ensure_default_profile(conn: Conn) -> None:
    """First start: Admin Hermes gets version 1 (the default spec), activated. Never touches an existing history."""
    if conn.execute(
        "SELECT 1 FROM actor_profile_versions WHERE actor_id = %s LIMIT 1", (ADMIN,)
    ).fetchone():
        conn.commit()
        return
    conn.commit()
    try:
        v = create_version(conn, ADMIN, DEFAULT_ADMIN_SPEC, created_by="owner")
        activate(conn, ADMIN, v["id"], by="owner")
    except Exception:
        conn.rollback()
        if get_active(conn, ADMIN) is None and not list_versions(conn, ADMIN):
            raise  # lost a race to another process is fine; anything else is not


def authority_changes(old: ActorProfileSpec, new: ActorProfileSpec) -> list[str]:
    """What a change touches that needs an explicit confirmation: tools, budgets, model and run limits."""
    changed: list[str] = []
    if sorted(old.tools.grants) != sorted(new.tools.grants):
        changed.append("tools")
    if old.budgets != new.budgets:
        changed.append("budgets")
    if old.runtime != new.runtime:
        changed.append("runtime")
    return changed
