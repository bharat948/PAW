from alembic import context
from sqlalchemy import create_engine

from paw_server.core.config import get_settings


def _url() -> str:
    url = context.config.get_main_option("sqlalchemy.url") or get_settings().database_url
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


def run_migrations_online() -> None:
    engine = create_engine(_url())
    with engine.connect() as connection:
        context.configure(connection=connection, target_metadata=None)
        with context.begin_transaction():
            context.run_migrations()


run_migrations_online()
