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


def render(
    fragment: dict[str, object], provider: str = "openai-api", proxy_url: str = ""
) -> list[str]:
    """With a proxy URL the runtime holds no provider key: it talks to the PAW model proxy as a custom provider
    (the entrypoint adds the proxy token from the environment)."""
    include = ", ".join(str(t) for t in fragment["mcp_include"])  # type: ignore[attr-defined]
    model = f"model.default={fragment['model']};agent.max_turns={fragment['max_iterations']}"
    config = (
        f"model.provider=custom;model.base_url={proxy_url};{model}"
        if proxy_url
        else f"model.provider={provider};{model}"
    )
    lines = [f"PAW_MCP_INCLUDE={include}", f"HERMES_CONFIG_SET={config}"]
    if proxy_url:
        lines.append(f"PAW_MODEL_PROXY_URL={proxy_url}")
    return [*lines, f"PAW_CONFIG_HASH={config_hash(fragment)}"]


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
                active.compiled.config_fragment,
                os.environ.get("PAW_HERMES_PROVIDER", "openai-api"),
                os.environ.get("PAW_MODEL_PROXY_URL", ""),
            )
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
