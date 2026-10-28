from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from paw_server import __version__
from paw_server.api import documents, health, retrieval
from paw_server.core.config import Settings, get_settings
from paw_server.core.errors import install_error_handlers
from paw_server.core.logging import configure_logging
from paw_server.core.middleware import CorrelationMiddleware
from paw_server.core.ratelimit import SlidingWindowLimiter
from paw_server.db.pool import create_pool
from paw_server.modules.ingestion.embedding import Embedder, build_embedder
from paw_server.modules.ingestion.storage import ObjectStore, build_store


def create_app(
    settings: Settings | None = None,
    store: ObjectStore | None = None,
    embedder: Embedder | None = None,
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
    app.state.search_limiter = SlidingWindowLimiter(settings.search_rate_limit_per_min)
    install_error_handlers(app)
    app.add_middleware(CorrelationMiddleware)
    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_methods=["GET", "POST"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
            expose_headers=["x-correlation-id"],
        )
    app.include_router(health.router)
    app.include_router(documents.router)
    app.include_router(retrieval.router)
    return app


def app_factory() -> FastAPI:
    return create_app()
