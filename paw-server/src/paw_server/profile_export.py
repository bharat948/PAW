"""Print the Hermes container settings compiled from the active Admin Hermes profile, as env lines:

    uv run python -m paw_server.profile_export > hermes.generated.env

The container reads them at start (entrypoint.sh) and reports PAW_CONFIG_HASH back to the gateway, so a profile change
that alters the model, turn limit or tool list shows as "restart required" until the container is restarted.
"""

import os
import sys

from paw_server.core.config import get_settings
from paw_server.db.pool import create_pool
from paw_server.modules.gateway.attestation import config_hash
from paw_server.modules.identity import service as identity


def render(fragment: dict[str, object], provider: str = "openai-api") -> list[str]:
    include = ", ".join(str(t) for t in fragment["mcp_include"])  # type: ignore[attr-defined]
    return [
        f"PAW_MCP_INCLUDE={include}",
        f"HERMES_CONFIG_SET=model.provider={provider};model.default={fragment['model']};agent.max_turns={fragment['max_iterations']}",
        f"PAW_CONFIG_HASH={config_hash(fragment)}",
    ]


def main() -> int:
    settings = get_settings()
    pool = create_pool(settings)
    pool.open()
    with pool.connection() as conn:
        identity.ensure_default_profile(conn)
        active = identity.get_active(conn, identity.ADMIN)
    pool.close()
    if active is None:
        print("no active profile", file=sys.stderr)
        return 1
    print(
        "\n".join(
            render(
                active.compiled.config_fragment, os.environ.get("PAW_HERMES_PROVIDER", "openai-api")
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
