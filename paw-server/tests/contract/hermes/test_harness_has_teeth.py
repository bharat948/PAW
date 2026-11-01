"""The contract tests must be able to FAIL. These run the same assertions against deliberately broken fakes."""

import pytest

from .fake_hermes import KEY, FakeHermes
from .hermes_client import HermesClient, parse_sse


def test_missing_capability_is_detected() -> None:
    fake = FakeHermes(features={"run_submission": True, "run_status": True, "run_events_sse": True})
    try:
        feats = HermesClient(fake.url, KEY).capabilities().json()["features"]
        assert (
            feats.get("run_stop") is not True
        )  # the real test would fail here -> adapter readiness 503
    finally:
        fake.close()


def test_stubborn_run_does_not_settle_after_stop() -> None:
    fake = FakeHermes()
    try:
        c = HermesClient(fake.url, KEY)
        rid = c.start_run("SLOW STUBBORN").json()["run_id"]
        assert c.stop_run(rid).json()["status"] == "stopping"
        with pytest.raises(TimeoutError):
            c.wait_terminal(
                rid, deadline_s=1.5
            )  # => 002A-R06: must become INTERRUPTED after the stop deadline
    finally:
        fake.close()


def test_wrong_key_is_rejected() -> None:
    fake = FakeHermes()
    try:
        assert HermesClient(fake.url, "wrong").capabilities().status_code == 401
    finally:
        fake.close()


def test_sse_parser_handles_comments_multiline_and_non_json() -> None:
    lines = [
        ": keepalive",
        "",
        "event: tool.started",
        'data: {"tool": "x"}',
        "",
        "data: plain",
        "data: two",
        "",
    ]
    frames = list(parse_sse(iter(lines)))
    assert frames[0].event == "tool.started" and frames[0].data == {"tool": "x"}
    assert frames[0].comments == ["keepalive"]
    assert frames[1].event == "message" and frames[1].data == "plain\ntwo"
