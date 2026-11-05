"""Agent worker: runs queued Admin Hermes runs against the Hermes container, one at a time."""

import logging
import signal
import threading

from paw_server.core.config import get_settings
from paw_server.core.logging import configure_logging
from paw_server.db.pool import create_pool
from paw_server.modules.runtime.agent_worker import AgentWorker
from paw_server.modules.runtime.hermes_adapter import HermesAdapter

log = logging.getLogger("paw.agent")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if not (settings.hermes_url and settings.hermes_key):
        log.error("PAW_HERMES_URL and PAW_HERMES_KEY are required for the agent worker")
        raise SystemExit(2)
    pool = create_pool(settings)
    pool.open()
    worker = AgentWorker(pool, HermesAdapter(settings.hermes_url, settings.hermes_key), settings)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    log.info("agent worker started, recovered %s interrupted run(s)", worker.recover())

    def reaper() -> (
        None
    ):  # runs beside the (blocking) run loop, so a hung stop cannot hold a run forever
        while not stop.wait(5):
            try:
                if n := worker.expire_deadlines():
                    log.warning("asked %s run(s) past their deadline to stop", n)
                worker.reconcile_costs()
                if n := worker.reap():
                    log.warning("ended %s run(s) whose stop was not confirmed in time", n)
            except Exception:
                log.exception("agent reaper error")

    threading.Thread(target=reaper, name="agent-reaper", daemon=True).start()
    while not stop.is_set():
        try:
            handled = worker.run_once()
        except Exception:  # DB outage etc.: keep the process alive, back off
            log.exception("agent worker loop error")
            handled = False
        if not handled:
            stop.wait(settings.worker_poll_s)
    pool.close()


if __name__ == "__main__":
    main()
