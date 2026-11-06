"""PAW MCP gateway: the only way Admin Hermes can act. Internal listener; never publish this port."""

import logging

import uvicorn

from paw_server.core.config import get_settings
from paw_server.core.logging import configure_logging
from paw_server.db.pool import create_pool
from paw_server.modules.approvals.service import ApprovalService
from paw_server.modules.auth.oidc import GoogleIdentityProvider
from paw_server.modules.connectors.calendar_service import CalendarService
from paw_server.modules.connectors.github_service import build_github
from paw_server.modules.connectors.google_calendar import GoogleCalendarProvider
from paw_server.modules.costs import tokens
from paw_server.modules.costs.proxy import ModelProxy
from paw_server.modules.costs.readiness import enforcement_problems
from paw_server.modules.gateway.builtin import builtin_registry
from paw_server.modules.gateway.registry import sync_registry
from paw_server.modules.gateway.server import build_app
from paw_server.modules.gateway.service import GatewayService
from paw_server.modules.gateway.shaping import ContinuationStore
from paw_server.modules.gateway.tools import ToolDeps, attach_handlers
from paw_server.modules.ingestion.embedding import build_embedder

log = logging.getLogger("paw.gateway")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if not settings.gateway_token:
        log.error("PAW_GATEWAY_TOKEN is required")
        raise SystemExit(2)
    pool = create_pool(settings)
    pool.open()
    store = ContinuationStore()
    calendar = None
    if settings.google_client_id and settings.google_client_secret and settings.connector_key:
        idp = GoogleIdentityProvider(settings.google_client_id, settings.google_client_secret)
        provider = GoogleCalendarProvider(
            settings.google_client_id, settings.google_client_secret, identity=idp
        )
        calendar = CalendarService(pool, provider, settings.connector_key)
    github = build_github(pool, settings)
    deps = ToolDeps(pool, settings, build_embedder(settings), calendar, store, github)
    registry = attach_handlers(builtin_registry(), deps)
    with pool.connection() as conn:
        sync_registry(conn, registry)
    service = GatewayService(
        pool,
        registry,
        settings,
        continuations=store,
        approvals=ApprovalService(pool, github) if github else None,
    )
    proxy = None
    if settings.model_proxy_token:
        with pool.connection() as conn:
            models = [
                r["model"]
                for r in conn.execute("SELECT DISTINCT model FROM ops_rate_cards").fetchall()
            ]
            conn.commit()
        try:
            tokens.warm(
                models
            )  # a model we cannot count tokens for must stop start-up, not the first request
        except Exception:
            log.exception("token encodings unavailable; refusing to start the model proxy")
            raise SystemExit(2) from None
        proxy = ModelProxy(pool, settings)
    app = build_app(
        service,
        registry,
        settings.gateway_token,
        proxy,
        settings.model_proxy_token,
        (lambda: enforcement_problems(pool, settings)) if proxy else None,
    )
    log.info(
        "gateway listening", extra={"host": settings.gateway_host, "port": settings.gateway_port}
    )
    uvicorn.run(app, host=settings.gateway_host, port=settings.gateway_port, log_level="warning")


if __name__ == "__main__":
    main()
