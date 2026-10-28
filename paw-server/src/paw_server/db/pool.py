from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from paw_server.core.config import Settings
from paw_server.modules.ingestion.jobs import Conn


def create_pool(settings: Settings) -> ConnectionPool[Conn]:
    """Pool is opened lazily so the process starts (and stays alive) without a database."""
    return ConnectionPool(
        settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
        timeout=settings.db_connect_timeout_s,
        open=False,
        kwargs={
            "connect_timeout": max(1, int(settings.db_connect_timeout_s)),
            "row_factory": dict_row,
        },
    )
