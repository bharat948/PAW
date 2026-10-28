import psycopg
import pytest
from alembic import command

from paw_server.core.config import Settings
from paw_server.db.migrations_info import alembic_config, expected_head
from paw_server.db.pool import create_pool
from paw_server.db.readiness import check_ready

pytestmark = pytest.mark.integration


def test_upgrade_from_empty_is_idempotent_and_ready(db_url):
    pool = create_pool(Settings(database_url=db_url))  # type: ignore[arg-type]
    assert check_ready(pool).failed_check == "migrations"  # empty DB: not migrated

    cfg = alembic_config(db_url)
    command.upgrade(cfg, "head")
    command.upgrade(cfg, "head")  # rerun is a no-op

    with psycopg.connect(db_url) as c:
        assert c.execute("SELECT version_num FROM alembic_version").fetchone() == (expected_head(),)
        assert c.execute("SELECT count(*) FROM workspaces WHERE slug='default'").fetchone() == (1,)
        assert c.execute("SELECT 1 FROM pg_extension WHERE extname='vector'").fetchone()
    assert check_ready(pool).ready
    pool.close()


def test_ready_fails_on_incompatible_migration(db_url):
    command.upgrade(alembic_config(db_url), "head")
    with psycopg.connect(db_url, autocommit=True) as c:
        c.execute("UPDATE alembic_version SET version_num='9999'")
    pool = create_pool(Settings(database_url=db_url))  # type: ignore[arg-type]
    assert check_ready(pool).failed_check == "migrations"
    pool.close()
