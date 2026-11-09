"""Local operator provisioning: python -m paw_server.runtime_registry --help.

Secrets are read from environment variables, never command arguments or output.
"""

import argparse
import json
import os

from paw_server.core.config import get_settings
from paw_server.db.pool import create_pool
from paw_server.modules.identity import service as identity
from paw_server.modules.identity.spec import DEFAULT_VOICE_SPEC
from paw_server.modules.runtime import registry


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["list", "register", "rotate", "revoke", "stop", "start"]
    )
    parser.add_argument("--name")
    parser.add_argument("--actor")
    parser.add_argument(
        "--kind",
        choices=list(registry.KINDS),
        help="runtime kind, fixed at first registration (default hermes)",
    )
    parser.add_argument("--url")
    parser.add_argument("--key-env")
    parser.add_argument("--purpose", choices=["gateway", "model"])
    parser.add_argument("--token-env")
    args = parser.parse_args()
    if args.command != "list" and not args.name:
        parser.error("--name is required")
    if args.command == "register" and not all((args.actor, args.url, args.key_env)):
        parser.error("register requires --actor, --url and --key-env")
    if args.kind == "voice" and args.actor != identity.VOICE:
        parser.error(f"a voice runtime belongs to the {identity.VOICE} actor")
    if args.command in ("rotate", "revoke") and not args.purpose:
        parser.error("--purpose is required")
    token = os.environ.get(args.token_env or "", "")
    if args.command == "rotate" and not token:
        parser.error("rotate requires --token-env naming a populated environment variable")
    settings = get_settings()
    with create_pool(settings) as pool, pool.connection() as conn:
        with conn.transaction():
            if args.command == "register":
                if (
                    args.kind == "voice"
                ):  # opt-in: the actor has no profile until a voice runtime is wanted
                    identity.ensure_profile(conn, identity.VOICE, DEFAULT_VOICE_SPEC)
                registry.register(
                    conn,
                    args.name,
                    args.actor,
                    endpoint_url=args.url,
                    key_env=args.key_env,
                    kind=args.kind,
                )
            elif args.command != "list":
                row = conn.execute(
                    "SELECT id FROM agent_runtimes WHERE name=%s FOR UPDATE", (args.name,)
                ).fetchone()
                if not row:
                    raise registry.RegistryError("unknown runtime")
                rid = row["id"]
                if args.command == "rotate":
                    registry.credential(conn, rid, args.purpose, token, rotate=True)
                elif args.command == "revoke":
                    conn.execute(
                        "UPDATE runtime_credentials SET revoked_at=now() WHERE runtime_id=%s AND purpose=%s AND revoked_at IS NULL",
                        (rid, args.purpose),
                    )
                elif args.command == "stop":
                    registry.stop(conn, rid)
                elif args.command == "start":
                    conn.execute(
                        "UPDATE agent_runtimes SET desired_state='running',status='unknown' WHERE id=%s",
                        (rid,),
                    )
        rows = conn.execute(
            "SELECT rt.id,rt.name,rt.kind,rt.actor_id,rt.desired_state,rt.status,rt.desired_profile_version_id,rt.last_health_at, "
            "ARRAY(SELECT purpose FROM runtime_credentials c WHERE c.runtime_id=rt.id AND c.revoked_at IS NULL ORDER BY purpose) AS credentials "
            "FROM agent_runtimes rt ORDER BY rt.name"
        ).fetchall()
        print(json.dumps(rows, default=str, indent=2))


if __name__ == "__main__":
    main()
