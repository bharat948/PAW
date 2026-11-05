"""The authenticated model proxy: the only path from an actor's runtime to the model provider.

Every request is bounded and priced before the provider is contacted: the run is resolved from the identity the
runtime stamps on the call, the payload is checked against an allow-list, input tokens are counted, the worst-case
cost is reserved (committed) and the send intent is recorded (committed). Only then is the provider called, with a
key that never leaves PAW. Usage found in the response settles the attempt exactly once; anything we cannot prove
(missing usage, broken stream, timeout, client gone) leaves the reservation held as `uncertain`. A denied request
makes zero provider calls. Request and response bodies are never logged.
"""

import json
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import UUID

import anyio
import httpx
from psycopg_pool import ConnectionPool

from paw_server.core.config import Settings
from paw_server.modules.costs import service as costs
from paw_server.modules.costs import tokens
from paw_server.modules.costs.pricing import CostError, Rate

log = logging.getLogger("paw.model_proxy")

CALLABLE_STATES = ("DISPATCHING", "RUNNING")  # same window in which the gateway accepts a tool call
SSE = "text/event-stream"
# Fields that change neither price nor surface. Anything else (n>1, audio, modalities, web search, ...) is refused:
# a field we have not priced must not reach the provider. Widen only against a verified runtime request.
ALLOWED_FIELDS = frozenset(
    {
        "model", "messages", "stream", "stream_options", "max_tokens", "max_completion_tokens", "temperature",
        "top_p", "tools", "tool_choice", "parallel_tool_calls", "stop", "seed", "presence_penalty",
        "frequency_penalty", "logit_bias", "response_format", "user", "n",
    }
)  # fmt: skip
DENIALS: dict[str, tuple[int, str, str]] = {
    "identity_missing": (403, "permission_error", "No valid run identity on this request."),
    "run_not_active": (403, "permission_error", "This run cannot make model calls."),
    "budget_exceeded": (403, "permission_error", "This request would exceed a spending limit."),
    "pricing_unavailable": (403, "permission_error", "No price is configured for this model."),
    "missing_run_budget": (403, "permission_error", "This run has no spending limit configured."),
    "model_not_allowed": (400, "invalid_request_error", "This run may only use its configured model."),
    "unsupported_model": (400, "invalid_request_error", "This model cannot be priced."),
    "unsupported_payload": (400, "invalid_request_error", "This request contains something that is not supported."),
    "unsupported_output_bound": (400, "invalid_request_error", "The output limit is out of range for this model."),
    "input_exceeds_context": (400, "invalid_request_error", "The request is larger than the model's context window."),
    "request_too_large": (413, "invalid_request_error", "The request body is too large."),
    "accounting_unavailable": (503, "server_error", "Spending could not be checked; the model was not called."),
}  # fmt: skip


class ProxyDenied(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        self.status, self.kind, self.message = DENIALS.get(
            code, (403, "permission_error", "Denied.")
        )
        super().__init__(code)


@dataclass(frozen=True)
class Admitted:
    attempt_id: UUID
    run_id: UUID
    body: bytes  # exactly what was measured and priced
    stream: bool


def _error_body(d: ProxyDenied) -> dict[str, Any]:
    return {"error": {"message": d.message, "type": d.kind, "code": d.code}}


def _as_int(v: Any) -> int | None:
    return v if type(v) is int and v >= 1 else None


class ModelProxy:
    def __init__(
        self,
        pool: ConnectionPool[Any],
        settings: Settings,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.pool, self.settings = pool, settings
        self.http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.model_proxy_timeout_s, connect=10.0)
        )

    # ---- admission (blocking; runs in a worker thread) ------------------------------------------------------
    def _resolve(self, conn: Any, run_id: UUID) -> dict[str, Any]:
        row = conn.execute(
            "SELECT r.id, r.session_id, r.executing_actor_id, r.profile_version_id, s.owner, s.workspace_id, "
            "c.policy, c.config_fragment->>'model' AS model "
            "FROM agent_runs r JOIN agent_run_leases l ON l.run_id = r.id "
            "JOIN agent_runtimes rt ON rt.id = l.runtime_id AND rt.name = %s "
            "JOIN agent_sessions s ON s.id = r.session_id "
            "JOIN actor_compiled_profiles c ON c.profile_version_id = r.profile_version_id "
            "WHERE r.id = %s AND r.state = ANY(%s) AND s.deleted_at IS NULL",
            (self.settings.gateway_runtime, run_id, list(CALLABLE_STATES)),
        ).fetchone()
        conn.commit()
        if not row or not row["model"]:
            raise ProxyDenied("run_not_active")
        return dict(row)

    def _shape(self, body: dict[str, Any], rate: Rate) -> tuple[dict[str, Any], int, bool]:
        """The request we will actually send, its output bound, and whether it streams."""
        if (
            set(body) - ALLOWED_FIELDS or body.get("n", 1) != 1
        ):  # n > 1 would multiply the output bill
            raise ProxyDenied("unsupported_payload")
        out = dict(body)
        stream = out.get("stream", False)
        if not isinstance(stream, bool):
            raise ProxyDenied("unsupported_payload")
        given = [_as_int(out[k]) for k in ("max_completion_tokens", "max_tokens") if k in out]
        if given:
            if None in given:
                raise ProxyDenied("unsupported_output_bound")
            output = min(g for g in given if g is not None)
        else:
            output = min(self.settings.model_proxy_default_output_tokens, rate.max_output)
            out["max_completion_tokens"] = output
        if stream:
            out["stream_options"] = {
                "include_usage": True
            }  # usage in the final frame is how a stream settles
        else:
            out.pop("stream_options", None)
        return out, output, stream

    def admit(self, run_header: str | None, raw: bytes) -> Admitted:
        try:
            run_id = UUID(run_header or "")
        except ValueError:
            raise ProxyDenied("identity_missing") from None
        try:
            body = json.loads(raw)
        except ValueError:
            raise ProxyDenied("unsupported_payload") from None
        if not isinstance(body, dict):
            raise ProxyDenied("unsupported_payload")
        try:
            with self.pool.connection() as conn:
                run = self._resolve(conn, run_id)
                model = run["model"]
                if body.get("model") != model:
                    raise ProxyDenied("model_not_allowed")
                rate = costs.rate_for(conn, model)
                conn.commit()
                clean, output, stream = self._shape(body, rate)
                bound = tokens.input_bound(tokens.estimate_input(clean, model), rate.context)
                budgets = run["policy"].get("budgets") or {}
                try:
                    per_run, daily = (
                        Decimal(str(budgets["per_run_usd"])),
                        Decimal(str(budgets["daily_usd"])),
                    )
                except (KeyError, ArithmeticError):
                    raise ProxyDenied("missing_run_budget") from None
                with conn.transaction():
                    attempt = costs.reserve(
                        conn, workspace=run["workspace_id"], owner=run["owner"],
                        actor=run["executing_actor_id"], session=run["session_id"],
                        profile=run["profile_version_id"], model=model, output=output,
                        daily_cap=daily, global_cap=self.settings.cost_shared_daily_usd,
                        run=run_id, run_cap=per_run, input_tokens=bound,
                    )  # fmt: skip
                with conn.transaction():
                    if not costs.forward(conn, attempt["id"]):
                        raise ProxyDenied("accounting_unavailable")
            return Admitted(attempt["id"], run_id, json.dumps(clean).encode(), stream)
        except ProxyDenied:
            raise
        except CostError as e:
            raise ProxyDenied(e.code) from None
        except Exception:
            log.exception("model proxy admission failed", extra={"run_id": str(run_id)})
            raise ProxyDenied("accounting_unavailable") from None

    # ---- accounting after the provider answers (blocking) --------------------------------------------------
    def _settle(self, a: Admitted, served: str | None, usage: Any) -> None:
        with self.pool.connection() as conn, conn.transaction():
            if served is None:
                costs.uncertain(conn, a.attempt_id)
            else:
                costs.settle(conn, a.attempt_id, served, usage)

    async def _finish(self, a: Admitted, served: str | None, usage: Any) -> None:
        with anyio.CancelScope(
            shield=True
        ):  # a client that hangs up must not strand the accounting
            try:
                await anyio.to_thread.run_sync(self._settle, a, served, usage)
            except Exception:
                log.exception("model proxy settlement failed", extra={"attempt": str(a.attempt_id)})

    # ---- ASGI ----------------------------------------------------------------------------------------------
    async def _respond(
        self, send: Any, status: int, body: bytes, ctype: str = "application/json"
    ) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", ctype.encode()),
                    (b"content-length", str(len(body)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _deny(self, send: Any, d: ProxyDenied) -> None:
        await self._respond(send, d.status, json.dumps(_error_body(d)).encode())

    async def _read(self, scope: Any, receive: Any) -> bytes:
        limit = self.settings.model_proxy_max_body_bytes
        declared = dict(scope["headers"]).get(b"content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise ProxyDenied("request_too_large")
        body = b""
        while True:
            msg = await receive()
            body += msg.get("body", b"")
            if len(body) > limit:
                raise ProxyDenied("request_too_large")
            if not msg.get("more_body"):
                return bytes(body)

    async def handle(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            raw = await self._read(scope, receive)
            header = dict(scope["headers"]).get(b"x-paw-run-id", b"").decode("latin-1")
            admitted = await anyio.to_thread.run_sync(self.admit, header, raw)
        except ProxyDenied as d:
            log.info("model call denied", extra={"code": d.code})
            return await self._deny(send, d)
        await self._forward(admitted, send)

    async def _forward(self, a: Admitted, send: Any) -> None:
        s = self.settings
        request = self.http.build_request(
            "POST",
            f"{s.model_proxy_upstream_base.rstrip('/')}/chat/completions",
            content=a.body,
            headers={
                "Authorization": f"Bearer {s.model_proxy_upstream_key}",
                "Content-Type": "application/json",
            },
        )
        try:
            response = await self.http.send(request, stream=True)
        except httpx.HTTPError:
            # The send intent is already durable, so the reservation can only be held (uncertain), never released:
            # even a failed connect is not something this process can prove later. Reconciliation (A1-09) settles it.
            await self._finish(a, None, None)
            error = {"message": "The model provider could not be reached.", "type": "server_error"}
            return await self._respond(
                send, 502, json.dumps({"error": {**error, "code": "upstream_unreachable"}}).encode()
            )
        except BaseException:
            await self._finish(a, None, None)  # may have been sent: hold the reservation
            raise
        served: str | None = None
        usage: Any = None
        try:
            ctype = response.headers.get("content-type", "")
            if response.status_code == 200 and a.stream and SSE in ctype:
                await send(
                    {"type": "http.response.start", "status": 200,
                     "headers": [(b"content-type", SSE.encode()), (b"cache-control", b"no-cache")]}
                )  # fmt: skip
                pending = b""
                async for chunk in response.aiter_raw():
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
                    pending += chunk
                    *lines, pending = pending.split(b"\n")
                    for line in lines:
                        served, usage = _frame(line, served, usage)
                if pending:
                    served, usage = _frame(pending, served, usage)
                await send({"type": "http.response.body", "body": b""})
            else:
                data = await response.aread()
                if response.status_code == 200 and not a.stream:
                    try:
                        parsed = json.loads(data)
                        served, usage = parsed.get("model"), parsed.get("usage")
                    except (ValueError, AttributeError):
                        pass
                await self._respond(send, response.status_code, data, ctype or "application/json")
        except BaseException:
            await self._finish(a, None, None)  # interrupted: usage unknown, reservation held
            raise
        finally:
            await response.aclose()
        await self._finish(a, served if usage is not None else None, usage)
        log.info(
            "model call finished",
            extra={
                "run_id": str(a.run_id),
                "attempt": str(a.attempt_id),
                "status": response.status_code,
            },
        )


def _frame(line: bytes, served: str | None, usage: Any) -> tuple[str | None, Any]:
    """Pick the served model and the usage block out of one SSE line. Never raises on provider noise."""
    line = line.strip()
    if not line.startswith(b"data:") or line.endswith(b"[DONE]"):
        return served, usage
    try:
        frame = json.loads(line[5:])
    except ValueError:
        return served, usage
    if not isinstance(frame, dict):
        return served, usage
    if isinstance(frame.get("model"), str):
        served = frame["model"]
    if frame.get("usage"):
        usage = frame["usage"]
    return served, usage
