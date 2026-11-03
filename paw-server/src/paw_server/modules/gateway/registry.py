"""The tool registry: every tool Hermes could ever call, with a versioned strict schema and a side-effect class.

Rules enforced when a tool is registered (a violation is a programming error and stops startup):
- the input schema is an object with `additionalProperties: false` (unknown arguments are rejected);
- no argument may be named like an identity field (002D-R04): who is calling, and for which run, is never taken
  from the model;
- `external_write` tools always require approval.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError
from psycopg.types.json import Jsonb

from paw_server.modules.ingestion.jobs import Conn

NAME = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
SIDE_EFFECTS = ("read", "internal_write", "external_write")
FORBIDDEN_ARGS = frozenset(
    {
        "actor", "actor_id", "agent", "agent_id", "user", "user_id", "owner", "owner_id", "requester",
        "workspace", "workspace_id", "tenant", "tenant_id", "run", "run_id", "session", "session_id",
        "invocation_id", "tool_invocation_id", "trace_id", "profile", "profile_version", "credential", "token",
    }
)  # fmt: skip

Handler = Callable[..., dict[str, Any]]


class RegistryError(Exception):
    pass


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    side_effect_class: str = "read"
    version: int = 1
    approval_required: bool = False
    provider: str | None = None  # connector that must be connected, e.g. "google_calendar"
    handler: Handler | None = field(default=None, compare=False)

    def __post_init__(self) -> None:
        if not NAME.match(self.name):
            raise RegistryError(f"bad tool name {self.name!r}")
        if self.side_effect_class not in SIDE_EFFECTS:
            raise RegistryError(
                f"{self.name}: unknown side-effect class {self.side_effect_class!r}"
            )
        if self.version < 1 or not self.description.strip():
            raise RegistryError(f"{self.name}: needs a version >= 1 and a description")
        if self.side_effect_class == "external_write" and not self.approval_required:
            raise RegistryError(f"{self.name}: external writes must require approval")
        s = self.input_schema
        try:
            Draft202012Validator.check_schema(s)
        except SchemaError as exc:
            raise RegistryError(f"{self.name}: invalid schema: {exc.message}") from exc
        if s.get("type") != "object" or s.get("additionalProperties") is not False:
            raise RegistryError(
                f"{self.name}: input schema must be an object with additionalProperties: false"
            )
        _check_property_names(self.name, s)

    @property
    def validator(self) -> Draft202012Validator:
        return Draft202012Validator(self.input_schema)


def _check_property_names(tool: str, schema: Any) -> None:
    """Identity-like names are rejected anywhere in the schema, however deeply nested."""
    if isinstance(schema, dict):
        for key, sub in schema.items():
            if key == "properties" and isinstance(sub, dict):
                bad = FORBIDDEN_ARGS & {p.lower() for p in sub}
                if bad:
                    raise RegistryError(
                        f"{tool}: identity-like argument names are not allowed: {sorted(bad)}"
                    )
            _check_property_names(tool, sub)
    elif isinstance(schema, list):
        for item in schema:
            _check_property_names(tool, item)


def validate_arguments(spec: ToolSpec, args: Any) -> list[str]:
    """Problems with `args`, as short messages that never echo the offending values. Empty = valid.
    Unknown or identity-like arguments are always a problem."""
    if not isinstance(args, dict):
        return ["arguments must be an object"]
    problems: list[str] = []
    forged = sorted(k for k in args if isinstance(k, str) and k.lower() in FORBIDDEN_ARGS)
    if forged:
        problems.append(f"forbidden identity field(s): {', '.join(forged)}")
    for err in sorted(spec.validator.iter_errors(args), key=lambda e: list(map(str, e.path))):
        where = ".".join(map(str, err.absolute_path)) or "arguments"
        if err.validator == "additionalProperties":
            extra = sorted(set(args) - set(spec.input_schema.get("properties", {})))
            problems.append(f"unknown argument(s): {', '.join(extra)}")
        else:
            problems.append(f"{where}: {err.validator} check failed")
    return list(dict.fromkeys(problems))


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec] | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        for s in specs or []:
            self.register(s)

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._tools:
            raise RegistryError(f"duplicate tool {spec.name!r}")
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __len__(self) -> int:
        return len(self._tools)


def sync_registry(conn: Conn, registry: ToolRegistry) -> None:
    """Mirror the code registry into PostgreSQL. New and changed tools are written; `enabled` (the owner's switch) is
    never overwritten; tools that no longer exist in code are disabled, never deleted."""
    with conn.transaction():
        for name in registry.names():
            t = registry.get(name)
            assert t is not None
            conn.execute(
                "INSERT INTO gateway_tools (name, version, side_effect_class, approval_required, provider, "
                "description, schema_json) VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT (name) DO UPDATE SET version = excluded.version, "
                "side_effect_class = excluded.side_effect_class, approval_required = excluded.approval_required, "
                "provider = excluded.provider, description = excluded.description, "
                "schema_json = excluded.schema_json, updated_at = now()",
                (t.name, t.version, t.side_effect_class, t.approval_required, t.provider, t.description,
                 Jsonb(t.input_schema)),
            )  # fmt: skip
        conn.execute(
            "UPDATE gateway_tools SET enabled = false, updated_at = now() WHERE NOT (name = ANY(%s)) AND enabled",
            (registry.names(),),
        )


def enabled_tools(conn: Conn) -> set[str]:
    rows = conn.execute("SELECT name FROM gateway_tools WHERE enabled").fetchall()
    conn.commit()
    return {r["name"] for r in rows}
