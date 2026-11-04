"""Conversation maintenance: task summaries, deletion replay and 30-day purge."""

import logging
import signal
import threading

from paw_server.core.config import get_settings
from paw_server.core.logging import configure_logging
from paw_server.db.pool import create_pool
from paw_server.modules.chat.llm import Completion, LLMError, Message, OpenAICompatLLM, build_llm
from paw_server.modules.sessions.journal import Journal
from paw_server.modules.sessions.privacy import purge_due, replay
from paw_server.modules.sessions.summaries import SummaryWorker

log = logging.getLogger("paw.sessions")


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    pool = create_pool(settings)
    pool.open()
    journal = Journal(settings.deletion_journal_dir)
    model = build_llm(settings.model_copy(update={"llm_model": settings.agent_summary_model}))

    def complete(messages: list[Message], max_tokens: int) -> Completion:
        if not isinstance(model, OpenAICompatLLM):
            raise LLMError("summary_provider_not_configured")
        return model.complete_with_usage(messages, max_tokens)

    worker = SummaryWorker(pool, settings, complete)
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    while not stop.is_set():
        try:
            with pool.connection() as conn:
                replay(conn, journal)
                purge_due(conn)
            if worker.run_once():
                continue
        except Exception:
            log.exception("session maintenance failed")
        stop.wait(5)
    pool.close()


if __name__ == "__main__":
    main()
