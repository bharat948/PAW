"""Reading the owner's Google Calendar. One implementation for the web page and the gateway tool: token refresh,
reconnect detection and the event query live here, not in either caller."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg_pool import ConnectionPool

from paw_server.modules.connectors import store
from paw_server.modules.connectors.google_calendar import CalendarProvider, ConnectorError

PROVIDER = "google_calendar"


class CalendarError(Exception):
    """`code`: not_connected · needs_reauth · unavailable."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class CalendarEvents:
    account_email: str
    events: list[dict[str, Any]]


class CalendarService:
    def __init__(self, pool: ConnectionPool[Any], provider: CalendarProvider, key: str) -> None:
        self.pool, self.provider, self.key = pool, provider, key

    def _needs_reauth(self, connection_id: Any, why: str) -> CalendarError:
        with self.pool.connection() as conn:
            store.set_status(conn, connection_id, "needs_reauth", why)
        return CalendarError("needs_reauth")

    def _access_token(self, live: store.Connection) -> str:
        with self.pool.connection() as conn:
            sec = store.load_secrets(conn, self.key, live.id)
        if sec is None:  # key rotated or row damaged: the owner must reconnect
            raise self._needs_reauth(live.id, "secret_unreadable")
        if datetime.fromisoformat(sec["expires_at"]) > datetime.now(UTC) + timedelta(seconds=60):
            return str(sec["access_token"])
        try:
            token, expires = self.provider.refresh(sec["refresh_token"])
        except ConnectorError as exc:
            if exc.code == "revoked":
                raise self._needs_reauth(live.id, "revoked") from exc
            raise CalendarError("unavailable") from exc
        with self.pool.connection() as conn:
            store.update_secrets(
                conn,
                self.key,
                live.id,
                {**sec, "access_token": token, "expires_at": expires.isoformat()},
            )
        return str(token)

    def list_events(self, owner: str, *, days: int, limit: int) -> CalendarEvents:
        with self.pool.connection() as conn:
            live = store.get_live(conn, owner, PROVIDER)
        if live is None:
            raise CalendarError("not_connected")
        if live.status != "active":
            raise CalendarError("needs_reauth")
        token = self._access_token(live)
        now = datetime.now(UTC)
        try:
            items = self.provider.list_events(
                token, time_min=now, time_max=now + timedelta(days=days), limit=limit
            )
        except ConnectorError as exc:
            if exc.code == "revoked":
                raise self._needs_reauth(live.id, "revoked") from exc
            raise CalendarError("unavailable") from exc
        return CalendarEvents(live.account_email, items)
