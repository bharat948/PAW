import psycopg
import pytest
from alembic import command

from paw_server.db.migrations_info import alembic_config


def test_additive_schema_roundtrip_and_immutable_rates(db_url):
    cfg = alembic_config(db_url)
    command.upgrade(cfg, "0013")
    command.upgrade(cfg, "head")
    with psycopg.connect(db_url, autocommit=True) as c:
        assert (
            c.execute(
                "SELECT cached_input_per_mtok FROM ops_rate_cards WHERE version='openai-2026-11-04'"
            ).fetchone()[0]
            > 0
        )
        with pytest.raises(psycopg.errors.CheckViolation):
            c.execute("UPDATE ops_rate_cards SET input_per_mtok=0")
    command.downgrade(cfg, "0013")
    command.upgrade(cfg, "head")
