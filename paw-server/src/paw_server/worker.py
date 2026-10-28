"""Placeholder worker process. Real job processing arrives in PAW-001B."""

import logging
import signal
import threading

from paw_server.core.config import get_settings
from paw_server.core.logging import configure_logging

log = logging.getLogger("paw.worker")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log.info("worker started (placeholder)", extra={"env": settings.env})
    while not stop.wait(30):
        log.info("worker heartbeat")
    log.info("worker stopped")


if __name__ == "__main__":
    main()
