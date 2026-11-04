"""Apply an explicitly supplied current deletion journal before a restored database is exposed."""

import json
import sys
from tempfile import TemporaryDirectory

from paw_server.core.config import get_settings
from paw_server.db.pool import create_pool
from paw_server.modules.sessions.journal import Journal, Receipt
from paw_server.modules.sessions.privacy import purge_due, replay


def main() -> None:
    payload = json.load(sys.stdin)
    if payload.get("version") != 1 or not isinstance(payload.get("receipts"), list):
        raise ValueError("A valid current deletion journal is required")
    receipts = [Receipt.model_validate(r) for r in payload["receipts"]]
    pool = create_pool(get_settings())
    pool.open()
    try:
        with TemporaryDirectory(prefix="paw-restore-journal-") as folder:
            journal = Journal(folder)
            with journal.guard():
                for receipt in receipts:
                    journal.write(receipt)
            with pool.connection() as conn:
                replay(conn, journal, force=True)
                purge_due(conn)
        print(
            "Deletion journal applied; synced through:",
            payload.get("synced_through") or "empty journal",
        )
    finally:
        pool.close()


if __name__ == "__main__":
    main()
