from fastapi import APIRouter
from pydantic import BaseModel

from paw_server.core.errors import ApiError
from paw_server.main import create_app

KEYS = {"code", "message", "correlation_id", "retryable"}


class Body(BaseModel):
    n: int


def _app(settings):
    app = create_app(settings)
    r = APIRouter()

    @r.get("/t/{status}")
    def raise_status(status: int):
        raise ApiError(status)

    @r.get("/boom")
    def boom():
        raise RuntimeError("secret internals")

    @r.post("/body")
    def body(b: Body):
        return b

    app.include_router(r)
    return app


def test_envelope_for_all_statuses(settings):
    from fastapi.testclient import TestClient

    c = TestClient(_app(settings), raise_server_exceptions=False)
    for status in (400, 401, 403, 404, 409, 413, 429, 503):
        r = c.get(f"/t/{status}")
        assert r.status_code == status
        assert KEYS <= r.json().keys()
    assert c.get("/t/429").json()["retryable"] is True
    assert c.get("/nope").json()["code"] == "not_found"

    r = c.post("/body", json={"n": "x"})
    assert r.status_code == 422
    assert r.json()["code"] == "validation_error" and r.json()["details"]

    r = c.get("/boom")
    assert r.status_code == 500
    assert "secret" not in r.text
    assert KEYS <= r.json().keys()


def test_correlation_id_echoed(client):
    r = client.get("/health", headers={"x-correlation-id": "abc-123"})
    assert r.headers["x-correlation-id"] == "abc-123"
    assert client.get("/health").headers["x-correlation-id"]
