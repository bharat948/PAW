"""Ingestion worker: polls the durable job queue. Run N replicas to scale; leases keep them safe."""

import logging
import os
import signal
import socket
import threading

from paw_server.core.config import get_settings
from paw_server.core.logging import configure_logging
from paw_server.db.pool import create_pool
from paw_server.modules.ingestion.embedding import build_embedder
from paw_server.modules.ingestion.pipeline import Worker
from paw_server.modules.ingestion.storage import build_store

log = logging.getLogger("paw.worker")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    pool = create_pool(settings)
    pool.open()
    worker = Worker(
        pool,
        build_store(settings),
        build_embedder(settings),
        settings,
        owner=f"{socket.gethostname()}-{os.getpid()}",
    )
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log.info("worker started", extra={"env": settings.env, "owner": worker.owner})
    while not stop.is_set():
        try:
            handled = worker.run_once()
        except Exception:  # DB outage etc.: keep the process alive, back off
            log.exception("worker loop error")
            handled = False
        if not handled:
            stop.wait(settings.worker_poll_s)
    pool.close()
    log.info("worker stopped")


if __name__ == "__main__":
    main()
