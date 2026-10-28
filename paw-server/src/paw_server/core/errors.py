import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from starlette.exceptions import HTTPException as StarletteHTTPException

from paw_server.core.logging import correlation_id_var

log = logging.getLogger(__name__)

_CODES = {
    400: ("bad_request", "The request was malformed.", False),
    401: ("unauthenticated", "Authentication is required.", False),
    403: ("forbidden", "You do not have access to this resource.", False),
    404: ("not_found", "The requested resource was not found.", False),
    405: ("method_not_allowed", "Method not allowed.", False),
    409: ("conflict", "The request conflicts with the current state.", False),
    413: ("payload_too_large", "The payload is too large.", False),
    415: ("unsupported_media_type", "This media type is not supported.", False),
    422: ("validation_error", "The request failed validation.", False),
    429: ("rate_limited", "Too many requests. Please retry later.", True),
    500: ("internal_error", "An unexpected error occurred.", False),
    503: ("service_unavailable", "The service is temporarily unavailable.", True),
}


class ErrorEnvelope(BaseModel):
    code: str
    message: str
    correlation_id: str
    retryable: bool
    details: list[dict[str, Any]] | None = None


class ApiError(Exception):
    """Raise from handlers to emit the standard envelope with a specific code."""

    def __init__(
        self,
        status: int,
        code: str | None = None,
        message: str | None = None,
        details: list[dict[str, Any]] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.headers = headers
        self.status = status
        d_code, d_msg, _ = _CODES.get(status, _CODES[500])
        self.code = code or d_code
        self.message = message or d_msg
        self.details = details


def _envelope(
    status: int,
    code: str,
    message: str,
    details: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    retryable = _CODES.get(status, _CODES[500])[2]
    body = ErrorEnvelope(
        code=code,
        message=message,
        correlation_id=correlation_id_var.get(),
        retryable=retryable,
        details=details,
    )
    return JSONResponse(
        status_code=status, content=body.model_dump(exclude_none=True), headers=headers
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api(_: Request, exc: ApiError) -> JSONResponse:
        return _envelope(exc.status, exc.code, exc.message, exc.details, exc.headers)

    @app.exception_handler(StarletteHTTPException)
    async def _http(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code, message, _r = _CODES.get(exc.status_code, _CODES[500])
        return _envelope(exc.status_code, code, message)

    @app.exception_handler(RequestValidationError)
    async def _validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = [
            {"loc": [str(p) for p in e["loc"]], "msg": e["msg"], "type": e["type"]}
            for e in exc.errors()
        ]
        return _envelope(422, "validation_error", _CODES[422][1], details)

    @app.exception_handler(Exception)
    async def _unhandled(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", exc_info=exc)
        return _envelope(500, "internal_error", _CODES[500][1])
