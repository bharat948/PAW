"""Google Calendar (read-only). Business code depends only on `CalendarProvider`.

Scope is `calendar.readonly` plus `openid email` so we can record which Google account was connected.
Event output is deliberately minimal (no description, attendees or meeting links).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urlencode

import httpx

from paw_server.modules.auth.oidc import AUTH_URL, TOKEN_URL, IdentityError

CALENDAR_SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
SCOPES = ["openid", "email", CALENDAR_SCOPE]
API = "https://www.googleapis.com/calendar/v3"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"


class ConnectorError(Exception):
    """`code`: revoked (user withdrew access: reconnect), unreachable, rejected, missing_scope."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Grant:
    email: str
    refresh_token: str
    access_token: str
    expires_at: datetime
    scopes: list[str]


class CalendarProvider(Protocol):
    def authorize_url(
        self, *, redirect_uri: str, state: str, nonce: str, code_challenge: str
    ) -> str: ...
    def exchange_code(
        self, *, code: str, redirect_uri: str, code_verifier: str, nonce: str
    ) -> Grant: ...
    def refresh(self, refresh_token: str) -> tuple[str, datetime]: ...
    def list_events(
        self, access_token: str, *, time_min: datetime, time_max: datetime, limit: int
    ) -> list[dict[str, Any]]: ...
    def revoke(self, token: str) -> None: ...


def shape_event(item: dict[str, Any]) -> dict[str, Any]:
    start, end = item.get("start") or {}, item.get("end") or {}
    return {
        "id": item.get("id"),
        "title": item.get("summary") or "(no title)",
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "all_day": "date" in start,
        "location": item.get("location"),
        "status": item.get("status"),
        "link": item.get("htmlLink"),
    }


class GoogleCalendarProvider:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        *,
        identity: Any,
        http: httpx.Client | None = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.client_id, self._secret = client_id, client_secret
        self._identity = identity  # GoogleIdentityProvider: reused to verify the id token
        self._http = http or httpx.Client(timeout=timeout_s)

    def authorize_url(
        self, *, redirect_uri: str, state: str, nonce: str, code_challenge: str
    ) -> str:
        q = {
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "access_type": "offline",  # we need a refresh token
            "prompt": "consent",  # Google only issues the refresh token on a consent screen
        }
        return f"{AUTH_URL}?{urlencode(q)}"

    def _post(self, url: str, data: dict[str, str]) -> httpx.Response:
        try:
            return self._http.post(url, data=data)
        except httpx.HTTPError as exc:
            raise ConnectorError("unreachable") from exc

    def exchange_code(
        self, *, code: str, redirect_uri: str, code_verifier: str, nonce: str
    ) -> Grant:
        r = self._post(
            TOKEN_URL,
            {
                "code": code,
                "client_id": self.client_id,
                "client_secret": self._secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
                "code_verifier": code_verifier,
            },
        )
        if r.status_code != 200:
            raise ConnectorError("rejected")
        body = r.json()
        scopes = str(body.get("scope", "")).split()
        if CALENDAR_SCOPE not in scopes:  # the user can untick it on the consent screen
            raise ConnectorError("missing_scope")
        if not body.get("refresh_token") or not body.get("access_token"):
            raise ConnectorError("rejected")
        try:
            ident = self._identity.verify_id_token(body["id_token"], nonce)
        except (KeyError, IdentityError) as exc:
            raise ConnectorError("rejected") from exc
        return Grant(
            email=ident.email,
            refresh_token=body["refresh_token"],
            access_token=body["access_token"],
            expires_at=datetime.now(UTC) + timedelta(seconds=int(body.get("expires_in", 3600))),
            scopes=scopes,
        )

    def refresh(self, refresh_token: str) -> tuple[str, datetime]:
        r = self._post(
            TOKEN_URL,
            {
                "client_id": self.client_id,
                "client_secret": self._secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        if r.status_code == 400 and r.json().get("error") == "invalid_grant":
            raise ConnectorError("revoked")
        if r.status_code != 200:
            raise ConnectorError("rejected")
        body = r.json()
        return body["access_token"], datetime.now(UTC) + timedelta(
            seconds=int(body.get("expires_in", 3600))
        )

    def list_events(
        self, access_token: str, *, time_min: datetime, time_max: datetime, limit: int
    ) -> list[dict[str, Any]]:
        try:
            r = self._http.get(
                f"{API}/calendars/primary/events",
                headers={"Authorization": f"Bearer {access_token}"},
                params={
                    "timeMin": time_min.isoformat(),
                    "timeMax": time_max.isoformat(),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "maxResults": str(limit),
                    "fields": "items(id,summary,start,end,location,status,htmlLink)",
                },
            )
        except httpx.HTTPError as exc:
            raise ConnectorError("unreachable") from exc
        if r.status_code in (401, 403):
            raise ConnectorError("revoked")
        if r.status_code != 200:
            raise ConnectorError("rejected")
        return [shape_event(i) for i in r.json().get("items", [])]

    def revoke(self, token: str) -> None:
        try:
            self._http.post(REVOKE_URL, data={"token": token})
        except httpx.HTTPError:
            pass  # best effort: we delete our copy regardless
