def test_health_is_process_oriented(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "alive"}


def test_ready_503_without_db_uses_envelope(client):
    r = client.get("/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["code"] == "not_ready"
    assert body["retryable"] is True
    assert body["correlation_id"]
    assert body["details"] == [{"check": "database", "status": "failing"}]
    assert "127.0.0.1" not in r.text and "postgresql" not in r.text


def test_health_stays_ok_when_ready_fails(client):
    assert client.get("/ready").status_code == 503
    assert client.get("/health").status_code == 200
