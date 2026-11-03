from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from paw_server import __version__
from paw_server.api import agent, auth, chat, dev, documents, health, integrations, retrieval
from paw_server.core.config import Settings, get_settings
from paw_server.core.errors import install_error_handlers
from paw_server.core.logging import configure_logging
from paw_server.core.middleware import CorrelationMiddleware
from paw_server.core.ratelimit import SlidingWindowLimiter
from paw_server.db.pool import create_pool
from paw_server.modules.auth.oidc import GoogleIdentityProvider, IdentityProvider
from paw_server.modules.chat.llm import LLM, build_llm
from paw_server.modules.connectors.google_calendar import CalendarProvider, GoogleCalendarProvider
from paw_server.modules.ingestion.embedding import Embedder, build_embedder
from paw_server.modules.ingestion.storage import ObjectStore, build_store
from paw_server.modules.runtime.hermes_adapter import HermesAdapter
from paw_server.modules.runtime.protocol import AgentRuntime


def create_app(
    settings: Settings | None = None,
    store: ObjectStore | None = None,
    embedder: Embedder | None = None,
    llm: LLM | None = None,
    idp: IdentityProvider | None = None,
    calendar: CalendarProvider | None = None,
    runtime: AgentRuntime | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.pool.open(wait=False)  # never blocks or fails startup if the DB is down
        yield
        app.state.pool.close()

    app = FastAPI(title="PAW API", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.pool = create_pool(settings)
    app.state.store = store or build_store(settings)
    app.state.embedder = embedder or build_embedder(settings)
    app.state.llm = llm or build_llm(settings)
    if idp is None and settings.auth_mode in ("google", "both"):
        idp = GoogleIdentityProvider(settings.google_client_id, settings.google_client_secret)
    app.state.idp = idp
    if (
        calendar is None
        and settings.auth_mode in ("google", "both")
        and settings.connector_key
        and isinstance(idp, GoogleIdentityProvider)
    ):
        calendar = GoogleCalendarProvider(
            settings.google_client_id, settings.google_client_secret, identity=idp
        )
    app.state.calendar = calendar
    if runtime is None and settings.hermes_url and settings.hermes_key:
        runtime = HermesAdapter(settings.hermes_url, settings.hermes_key)
    app.state.runtime = runtime
    app.state.auth_limiter = SlidingWindowLimiter(30)
    app.state.chat_limiter = SlidingWindowLimiter(settings.chat_rate_limit_per_min)
    app.state.search_limiter = SlidingWindowLimiter(settings.search_rate_limit_per_min)
    install_error_handlers(app)
    app.add_middleware(CorrelationMiddleware)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,  # the session cookie; origins are exact, never "*"
            allow_methods=["GET", "POST", "DELETE"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "X-CSRF-Token"],
            expose_headers=["x-correlation-id"],
        )
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(integrations.router)
    app.include_router(agent.router)
    app.include_router(documents.router)
    app.include_router(retrieval.router)
    app.include_router(chat.router)
    app.include_router(dev.router)
    return app


def app_factory() -> FastAPI:
    return create_app()
