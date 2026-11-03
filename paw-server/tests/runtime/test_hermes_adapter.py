"""HermesAdapter: mocked-transport unit tests for every failure mode, plus runs against the fake Hermès."""

import json

import httpx
import pytest

from paw_server.modules.runtime.hermes_adapter import HermesAdapter
from paw_server.modules.runtime.protocol import RuntimeError_
from tests.contract.hermes.fake_hermes import KEY, FakeHermes

BASE = "http://hermes.test"


def adapter(handler, **kw) -> HermesAdapter:
    kw.setdefault("sleep", lambda s: None)
    http = httpx.Client(base_url=BASE, transport=httpx.MockTransport(handler))
    return HermesAdapter(BASE, "k", http=http, **kw)


def sse(*frames: dict, comment: bool = True) -> httpx.Response:
    body = (": open\n\n" if comment else "") + "".join(f"data: {json.dumps(f)}\n\n" for f in frames)
    return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})


def delta(seq, text="x"):
    return {"event": "message.delta", "run_id": "R", "seq": seq, "delta": text}


def done(seq, **kw):
    return {
        "event": "run.completed",
        "run_id": "R",
        "seq": seq,
        "output": "ab",
        "completed": True,
        "partial": False,
        "interrupted": False,
        "usage": {"input_tokens": 3, "output_tokens": 2},
        **kw,
    }


# ---- create_run -------------------------------------------------------------------------------------------------


def test_create_sends_key_bearer_and_body_and_returns_the_handle():
    seen = {}

    def h(req: httpx.Request):
        seen.update(
            auth=req.headers["authorization"],
            idem=req.headers["idempotency-key"],
            body=json.loads(req.content),
        )
        return httpx.Response(202, json={"run_id": "R1", "status": "started", "replayed": False})

    r = adapter(h).create_run(
        run_id="paw-1",
        input="hi",
        instructions="be brief",
        history=[{"role": "user", "content": "a"}],
    )
    assert (r.hermes_run_id, r.replayed) == ("R1", False)
    assert seen["auth"] == "Bearer k" and seen["idem"] == "paw-1"
    assert seen["body"] == {
        "input": "hi",
        "instructions": "be brief",
        "conversation_history": [{"role": "user", "content": "a"}],
    }


def test_create_omits_optional_fields_and_reads_the_replayed_header():
    def h(req):
        assert json.loads(req.content) == {"input": "hi"}
        return httpx.Response(202, json={"run_id": "R1"}, headers={"Idempotency-Replayed": "true"})

    assert adapter(h).create_run(run_id="p", input="hi").replayed is True


def test_create_retries_transient_failures_with_the_same_key_then_succeeds():
    keys = []

    def h(req):
        keys.append(req.headers["idempotency-key"])
        if len(keys) < 3:
            return httpx.Response(503)
        return httpx.Response(202, json={"run_id": "R1"})

    assert adapter(h).create_run(run_id="p", input="x").hermes_run_id == "R1"
    assert keys == ["p", "p", "p"]


def test_create_gives_up_after_the_retry_budget():
    calls = []
    with pytest.raises(RuntimeError_) as e:
        adapter(lambda r: calls.append(1) or httpx.Response(503), create_retries=2).create_run(
            run_id="p", input="x"
        )
    assert e.value.code == "unavailable" and e.value.retryable and len(calls) == 3


@pytest.mark.parametrize(
    ("status", "code"), [(401, "auth"), (403, "auth"), (400, "rejected"), (422, "rejected")]
)
def test_create_does_not_retry_permanent_errors(status, code):
    calls = []
    with pytest.raises(RuntimeError_) as e:
        adapter(
            lambda r: (
                calls.append(1)
                or httpx.Response(status, json={"error": {"message": "secret upstream text"}})
            )
        ).create_run(run_id="p", input="x")
    assert e.value.code == code and not e.value.retryable and len(calls) == 1
    assert "secret upstream text" not in str(e.value)


def test_create_maps_timeouts_and_network_errors():
    def boom(exc):
        def h(req):
            raise exc

        return h

    with pytest.raises(RuntimeError_) as e:
        adapter(boom(httpx.ReadTimeout("t")), create_retries=0).create_run(run_id="p", input="x")
    assert e.value.code == "timeout"
    with pytest.raises(RuntimeError_) as e:
        adapter(boom(httpx.ConnectError("c")), create_retries=0).create_run(run_id="p", input="x")
    assert e.value.code == "unavailable"


@pytest.mark.parametrize("body", [{"status": "started"}, {"run_id": ""}, ["x"]])
def test_create_rejects_a_response_without_a_run_id(body):
    with pytest.raises(RuntimeError_) as e:
        adapter(lambda r: httpx.Response(202, json=body)).create_run(run_id="p", input="x")
    assert e.value.code == "protocol"


def test_create_rejects_non_json_responses():
    with pytest.raises(RuntimeError_) as e:
        adapter(lambda r: httpx.Response(202, content=b"<html>")).create_run(run_id="p", input="x")
    assert e.value.code == "protocol"


# ---- stream_events ----------------------------------------------------------------------------------------------


def test_stream_yields_ordered_events_and_stops_at_the_terminal_event():
    a = adapter(lambda r: sse(delta(0, "a"), delta(1, "b"), done(2), delta(3, "after terminal")))
    evs = list(a.stream_events("R"))
    assert [e.type for e in evs] == ["message.delta", "message.delta", "run.completed"]
    assert evs[-1].payload["output"] == "ab" and evs[-1].payload["usage"]["output_tokens"] == 2


def test_stream_drops_duplicates_and_honours_after_seq():
    a = adapter(lambda r: sse(delta(0), delta(1), delta(1), done(2)))
    assert [e.source_event_id for e in a.stream_events("R")] == ["R:0", "R:1", "R:2"]
    assert [e.source_event_id for e in a.stream_events("R", after_seq=1)] == ["R:2"]


def test_a_gap_in_seq_is_a_protocol_fault_not_silently_accepted():
    a = adapter(lambda r: sse(delta(0), delta(2), done(3)))
    it = a.stream_events("R")
    assert next(it).source_event_id == "R:0"
    with pytest.raises(RuntimeError_) as e:
        next(it)
    assert e.value.code == "protocol"


def test_reconnect_after_a_cut_stream_replayed_from_the_start_has_no_duplicates():
    n = {"i": 0}

    def h(req):
        n["i"] += 1
        if req.url.path.endswith("/events"):
            return (
                sse(delta(0), delta(1))
                if n["i"] == 1
                else sse(delta(0), delta(1), delta(2), done(3))
            )
        raise AssertionError("must not need to poll")

    evs = list(adapter(h).stream_events("R"))
    assert [e.source_event_id for e in evs] == ["R:0", "R:1", "R:2", "R:3"] and evs[-1].terminal


def test_stream_ending_without_terminal_is_resolved_from_run_status():
    def h(req):
        if req.url.path.endswith("/events"):
            return sse(delta(0), delta(1))
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": "full text",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            },
        )

    evs = list(adapter(h, reconnects=1).stream_events("R"))
    assert [e.type for e in evs] == ["message.delta", "message.delta", "run.completed"]
    last = evs[-1]
    assert last.payload["synthesized"] is True and last.payload["output"] == "full text"
    assert last.source_event_id == "R:2"


@pytest.mark.parametrize(
    ("status", "terminal"),
    [("failed", "run.failed"), ("cancelled", "run.cancelled"), ("interrupted", "run.interrupted")],
)
def test_status_resolution_maps_other_terminal_states(status, terminal):
    def h(req):
        return (
            sse()
            if req.url.path.endswith("/events")
            else httpx.Response(200, json={"status": status})
        )

    assert [e.type for e in adapter(h, reconnects=0).stream_events("R")] == [terminal]


def test_unresolvable_run_becomes_stream_lost_never_success():
    def h(req):
        return (
            sse(delta(0))
            if req.url.path.endswith("/events")
            else httpx.Response(200, json={"status": "running"})
        )

    it = adapter(h, reconnects=0, poll_interval_s=1, poll_deadline_s=3).stream_events("R")
    assert next(it).type == "message.delta"
    with pytest.raises(RuntimeError_) as e:
        next(it)
    assert e.value.code == "stream_lost"


def test_status_endpoint_down_too_is_stream_lost():
    def h(req):
        return sse() if req.url.path.endswith("/events") else httpx.Response(503)

    with pytest.raises(RuntimeError_) as e:
        list(adapter(h, reconnects=0, poll_interval_s=1, poll_deadline_s=2).stream_events("R"))
    assert e.value.code == "stream_lost"


def test_auth_failure_on_the_stream_is_not_retried_or_masked():
    calls = []
    with pytest.raises(RuntimeError_) as e:
        list(adapter(lambda r: calls.append(1) or httpx.Response(401)).stream_events("R"))
    assert e.value.code == "auth" and len(calls) == 1


def test_connection_errors_fall_back_to_status():
    def h(req):
        if req.url.path.endswith("/events"):
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"status": "completed", "output": "ok"})

    evs = list(adapter(h, reconnects=1).stream_events("R"))
    assert [e.type for e in evs] == ["run.completed"]


def test_keepalive_comments_and_garbage_frames_are_ignored():
    body = (
        ": open\n\n: keepalive\n\ndata: not json\n\ndata: [1,2]\n\n"
        + f"data: {json.dumps(done(0))}\n\n"
    )
    evs = list(adapter(lambda r: httpx.Response(200, content=body.encode())).stream_events("R"))
    assert [e.type for e in evs] == ["run.completed"]


# ---- get_run / stop / capabilities -----------------------------------------------------------------------------


def test_get_run_snapshot_and_unknown_status():
    def h(r):
        return httpx.Response(
            200,
            json={
                "status": "waiting_for_approval",
                "pending_controls": [{"kind": "approval"}],
                "output": None,
            },
        )

    s = adapter(h).get_run("R")
    assert (
        s.status == "waiting_for_approval"
        and not s.terminal
        and s.pending_controls == [{"kind": "approval"}]
        and s.output == ""
    )
    assert (
        adapter(lambda r: httpx.Response(200, json={"status": "weird"})).get_run("R").status
        == "unknown"
    )


def test_stop_ok_and_already_finished_but_errors_surface():
    adapter(lambda r: httpx.Response(200, json={"status": "stopping"})).stop_run("R")
    adapter(lambda r: httpx.Response(409)).stop_run("R")
    with pytest.raises(RuntimeError_):
        adapter(lambda r: httpx.Response(404)).stop_run("R")
    with pytest.raises(RuntimeError_) as e:
        adapter(lambda r: httpx.Response(503)).stop_run("R")
    assert e.value.retryable


GOOD = {
    "auth": {"required": True},
    "features": {
        "run_submission": True,
        "run_status": True,
        "run_events_sse": True,
        "run_stop": True,
        "runs_idempotency": {"supported": True},
    },
}


def test_capabilities_ok():
    assert adapter(lambda r: httpx.Response(200, json=GOOD)).missing_capabilities() == []


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda c: c["features"].update(run_stop=False), "run_stop"),
        (lambda c: c["features"].pop("run_events_sse"), "run_events_sse"),
        (lambda c: c["features"].update(runs_idempotency={"supported": False}), "runs_idempotency"),
        (lambda c: c["features"].pop("runs_idempotency"), "runs_idempotency"),
        (lambda c: c.update(auth={"required": False}), "auth_required"),
        (lambda c: c.pop("auth"), "auth_required"),
    ],
)
def test_missing_capability_is_reported(mutate, expected):
    import copy

    caps = copy.deepcopy(GOOD)
    mutate(caps)
    assert expected in adapter(lambda r: httpx.Response(200, json=caps)).missing_capabilities()


def test_capabilities_unreachable_or_unauthorised_is_not_ready():
    assert adapter(lambda r: httpx.Response(401)).missing_capabilities() == ["unreachable:auth"]
    assert adapter(lambda r: httpx.Response(503)).missing_capabilities() == [
        "unreachable:unavailable"
    ]


def test_healthy():
    assert adapter(lambda r: httpx.Response(200)).healthy() is True
    assert adapter(lambda r: httpx.Response(500)).healthy() is False

    def down(req):
        raise httpx.ConnectError("x")

    assert adapter(down).healthy() is False


# ---- against the fake Hermès (real HTTP, real SSE) --------------------------------------------------------------


@pytest.fixture
def fake():
    f = FakeHermes()
    yield f
    f.close()


def real(fake, **kw) -> HermesAdapter:
    return HermesAdapter(
        fake.url, KEY, sleep=lambda s: None, poll_interval_s=0.05, poll_deadline_s=3, **kw
    )


def test_fake_end_to_end_run():
    f = FakeHermes()
    try:
        a = real(f)
        assert a.missing_capabilities() == [] and a.healthy()
        h = a.create_run(run_id="paw-1", input="hello")
        evs = list(a.stream_events(h.hermes_run_id))
        assert evs[-1].type == "run.completed" and "".join(
            e.payload["text"] for e in evs[:-1]
        ).startswith("echo:")
        assert a.get_run(h.hermes_run_id).status == "completed"
    finally:
        f.close()


def test_fake_wrong_key_is_an_auth_error(fake):
    bad = HermesAdapter(fake.url, "wrong", sleep=lambda s: None)
    with pytest.raises(RuntimeError_) as e:
        bad.create_run(run_id="p", input="x")
    assert e.value.code == "auth" and bad.missing_capabilities() == ["unreachable:auth"]


def test_fake_lost_create_response_never_starts_two_runs(fake):
    fake.lose_create_response = 1
    h = real(fake).create_run(run_id="paw-dup", input="hello")
    assert h.replayed is True and len(fake.runs) == 1 and fake.creates == 2


def test_fake_transient_503_then_success_starts_exactly_one_run(fake):
    fake.fail_create = 2
    real(fake).create_run(run_id="paw-503", input="hello")
    assert len(fake.runs) == 1 and fake.creates == 3


def test_fake_stream_cut_mid_run_has_no_gaps_or_duplicates(fake):
    a = real(fake)
    h = a.create_run(run_id="paw-cut", input="hello world")
    fake.cut_stream_after = 2
    evs = list(a.stream_events(h.hermes_run_id))
    seqs = [int(e.source_event_id.rsplit(":", 1)[1]) for e in evs]
    assert seqs == list(range(len(seqs))) and evs[-1].type == "run.completed"
    direct = [e for e in real(fake).stream_events(h.hermes_run_id)]
    assert [e.source_event_id for e in direct] == [e.source_event_id for e in evs]


def test_fake_stop_settles_as_cancelled(fake):
    a = real(fake)
    h = a.create_run(run_id="paw-slow", input="SLOW")
    a.stop_run(h.hermes_run_id)
    evs = list(a.stream_events(h.hermes_run_id))
    assert evs[-1].type == "run.cancelled"
