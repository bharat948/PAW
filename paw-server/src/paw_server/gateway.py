"""PAW MCP gateway: the only way Admin Hermes can act. Internal listener; never publish this port."""

import logging

import uvicorn

from paw_server.core.config import get_settings
from paw_server.core.logging import configure_logging
from paw_server.db.pool import create_pool
from paw_server.modules.gateway.builtin import builtin_registry
from paw_server.modules.gateway.registry import sync_registry
from paw_server.modules.gateway.server import build_app
from paw_server.modules.gateway.service import GatewayService

log = logging.getLogger("paw.gateway")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if not settings.gateway_token:
        log.error("PAW_GATEWAY_TOKEN is required")
        raise SystemExit(2)
    pool = create_pool(settings)
    pool.open()
    registry = builtin_registry()
    with pool.connection() as conn:
        sync_registry(conn, registry)
    app = build_app(GatewayService(pool, registry, settings), registry, settings.gateway_token)
    log.info(
        "gateway listening", extra={"host": settings.gateway_host, "port": settings.gateway_port}
    )
    uvicorn.run(app, host=settings.gateway_host, port=settings.gateway_port, log_level="warning")


if __name__ == "__main__":
    main()
