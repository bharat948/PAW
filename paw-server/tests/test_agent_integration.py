"""Admin Hermes runs: admission, queue, worker, cancel, recovery and the durable event stream, against PostgreSQL."""

import json
import threading
import time
from uuid import UUID

import psycopg
import pytest

from paw_server.db.pool import create_pool
from paw_server.modules.runtime import lifecycle as lc
from paw_server.modules.runtime.agent_worker import AgentWorker
from paw_server.modules.runtime.protocol import RuntimeError_
from tests.conftest import AUTH
from tests.runtime.fakes import FakeRuntime, completed, delta

pytestmark = pytest.mark.integration


def sql(env, q, *args):
    with psycopg.connect(env.url, autocommit=True) as conn:
        cur = conn.execute(q, args)
        return cur.fetchall() if cur.description else None


class Harness:
    def __init__(self, env, rt: FakeRuntime | None = None, **kw) -> None:
        self.env, self.rt = env, rt or FakeRuntime()
        self.c = env.client(runtime=self.rt, **kw)
        self.settings = env.settings(**kw)
        self.pool = create_pool(self.settings)
        self.pool.open()
        self.worker = AgentWorker(self.pool, self.rt, self.settings)

    def session(self) -> str:
        r = self.c.post("/v1/agent-sessions", json={"title": "t"}, headers=AUTH)
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def send(self, session: str, message="hello", cid=None, expect=202):
        cid = cid or f"c{time.monotonic_ns()}"
        r = self.c.post(
            f"/v1/agent-sessions/{session}/runs",
            json={"client_request_id": cid, "message": message},
            headers=AUTH,
        )
        assert r.status_code == expect, r.text
        return r.json()

    def run(self, run_id: str) -> dict:
        return self.c.get(f"/v1/agent-runs/{run_id}", headers=AUTH).json()

    def events(self, run_id: str, **params) -> list[dict]:
        r = self.c.get(f"/v1/agent-runs/{run_id}/events", params=params, headers=AUTH)
        assert r.status_code == 200
        return parse_sse(r.text)

    def types(self, run_id: str) -> list[str]:
        return [e["type"] for e in self.events(run_id)]


def parse_sse(text: str) -> list[dict]:
    out = []
    for block in text.split("\n\n"):
        data = [ln[5:].strip() for ln in block.splitlines() if ln.startswith("data:")]
        if data:
            out.append(json.loads(data[0]))
    return out


@pytest.fixture
def h(env):
    x = Harness(env)
    yield x
    x.pool.close()


# ---- admission ---------------------------------------------------------------------------------------------------


def test_submit_returns_a_queued_run_and_a_duplicate_returns_the_same_one(h, env):
    s = h.session()
    a = h.send(s, cid="same")
    assert a["state"] == "QUEUED" and a["message"] == "hello" and a["output"] is None
    b = h.send(s, cid="same", expect=200)
    assert b["id"] == a["id"]
    assert sql(env, "SELECT count(*) FROM agent_runs") == [(1,)]
    assert sql(env, "SELECT count(*) FROM agent_run_events WHERE type='run.queued'") == [(1,)]


def test_concurrent_duplicate_submits_create_exactly_one_run(h, env):
    s = h.session()
    results, barrier = [], threading.Barrier(8)

    def go():
        barrier.wait()
        r = h.c.post(
            f"/v1/agent-sessions/{s}/runs",
            json={"client_request_id": "dup", "message": "hi"},
            headers=AUTH,
        )
        results.append((r.status_code, r.json().get("id")))

    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert {code for code, _ in results} <= {200, 202}
    assert len({rid for _, rid in results}) == 1
    assert sql(env, "SELECT count(*) FROM agent_runs") == [(1,)]


def test_a_second_active_run_in_the_same_session_is_refused(h):
    s = h.session()
    h.send(s, cid="a")
    r = h.c.post(
        f"/v1/agent-sessions/{s}/runs",
        json={"client_request_id": "b", "message": "x"},
        headers=AUTH,
    )
    assert r.status_code == 409 and r.json()["code"] == "session_busy"


def test_reusing_a_request_id_with_another_message_is_a_conflict(h):
    s = h.session()
    h.send(s, message="one", cid="k")
    r = h.c.post(
        f"/v1/agent-sessions/{s}/runs",
        json={"client_request_id": "k", "message": "two"},
        headers=AUTH,
    )
    assert r.status_code == 409 and r.json()["code"] == "idempotency_conflict"


def test_queue_overflow_is_429_and_other_sessions_are_independent(env):
    h = Harness(env, agent_queue_limit=2)
    try:
        h.send(h.session())
        h.send(h.session())
        r = h.c.post(
            f"/v1/agent-sessions/{h.session()}/runs",
            json={"client_request_id": "z", "message": "x"},
            headers=AUTH,
        )
        assert (
            r.status_code == 429
            and r.json()["code"] == "queue_full"
            and r.json()["retryable"] is True
        )
        assert sql(env, "SELECT count(*) FROM agent_runs") == [(2,)]
    finally:
        h.pool.close()


@pytest.mark.parametrize(
    "body",
    [{"client_request_id": "a", "message": ""}, {"client_request_id": "a", "message": "x" * 8001},
     {"client_request_id": "bad id!", "message": "x"}, {"client_request_id": "", "message": "x"}, {"message": "x"}],
)  # fmt: skip
def test_invalid_submissions_are_rejected(h, body):
    r = h.c.post(f"/v1/agent-sessions/{h.session()}/runs", json=body, headers=AUTH)
    assert r.status_code == 422


def test_authentication_and_ownership(h, env):
    s = h.session()
    run = h.send(s)
    for method, path in [("post", "/v1/agent-sessions"), ("get", "/v1/agent-sessions"), ("get", f"/v1/agent-runs/{run['id']}"),
                         ("get", f"/v1/agent-runs/{run['id']}/events"), ("post", f"/v1/agent-runs/{run['id']}/cancel"),
                         ("get", "/v1/runtime/health")]:  # fmt: skip
        assert getattr(h.c, method)(path).status_code == 401, path
    sql(
        env,
        "INSERT INTO actors (id, kind, display_name) VALUES ('someone-else', 'human', 'Someone')",
    )
    ws = sql(env, "SELECT id FROM workspaces LIMIT 1")[0][0]
    other = sql(
        env,
        "INSERT INTO agent_sessions (owner, workspace_id) VALUES ('someone-else', %s) RETURNING id",
        ws,
    )[0][0]
    assert (
        h.c.post(
            f"/v1/agent-sessions/{other}/runs",
            json={"client_request_id": "a", "message": "x"},
            headers=AUTH,
        ).status_code
        == 404
    )
    other_run = sql(
        env,
        "INSERT INTO agent_runs (session_id, actor_id, client_request_id, user_message) VALUES (%s,'someone-else','a','x') RETURNING id",
        other,
    )[0][0]
    assert h.c.get(f"/v1/agent-runs/{other_run}", headers=AUTH).status_code == 404
    assert h.c.get(f"/v1/agent-runs/{other_run}/events", headers=AUTH).status_code == 404
    assert h.c.post(f"/v1/agent-runs/{other_run}/cancel", headers=AUTH).status_code == 404


def test_agent_endpoints_say_unavailable_when_hermes_is_not_configured(env):
    c = env.client()
    r = c.post("/v1/agent-sessions", json={}, headers=AUTH)
    assert r.status_code == 503 and r.json()["code"] == "agent_unavailable"
    assert c.get("/v1/runtime/health", headers=AUTH).json() == {
        "configured": False,
        "ready": False,
        "missing": ["not_configured"],
        "config": "unknown",
    }


def test_runtime_health_reports_missing_capabilities(h):
    h.rt.missing = ["run_stop"]
    assert h.c.get("/v1/runtime/health", headers=AUTH).json() == {
        "configured": True,
        "ready": False,
        "missing": ["run_stop"],
        "config": "unknown",
    }
    h.rt.missing = []
    assert h.c.get("/v1/runtime/health", headers=AUTH).json()["ready"] is True


def test_sessions_list_and_run_history(h):
    s = h.session()
    r1 = h.send(s, message="first")
    assert h.worker.run_once()
    h.send(s, message="second")
    runs = h.c.get(f"/v1/agent-sessions/{s}/runs", headers=AUTH).json()["runs"]
    assert [r["message"] for r in runs] == ["first", "second"] and runs[0]["id"] == r1["id"]
    assert runs[0]["state"] == "SUCCEEDED" and runs[0]["output"] == "hello"
    assert [x["id"] for x in h.c.get("/v1/agent-sessions", headers=AUTH).json()["sessions"]] == [s]


# ---- worker ------------------------------------------------------------------------------------------------------


def test_worker_completes_a_run_and_stores_every_event_in_order(h, env):
    run = h.send(h.session())
    assert h.worker.run_once() is True
    done = h.run(run["id"])
    assert (
        done["state"] == "SUCCEEDED"
        and done["output"] == "hello"
        and done["usage"] == {"input_tokens": 11, "output_tokens": 7}
    )
    assert done["started_at"] and done["finished_at"]
    evs = h.events(run["id"])
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1))
    assert [e["type"] for e in evs] == [
        "run.queued",
        "run.state",
        "run.state",
        "message.delta",
        "message.delta",
        "run.completed",
    ]
    assert [e["payload"].get("state") for e in evs if e["type"] == "run.state"] == [
        "DISPATCHING",
        "RUNNING",
    ]
    assert sql(env, "SELECT count(*) FROM agent_run_leases") == [(0,)]
    assert h.worker.run_once() is False


def test_worker_sends_the_pae_run_id_instructions_and_prior_turns_as_history(h):
    s = h.session()
    first = h.send(s, message="my name is Bharat")
    h.rt.script = lambda hid: [completed("Nice to meet you")]
    h.worker.run_once()
    h.rt.script = lambda hid: [completed("Bharat")]
    h.send(s, message="what is my name?")
    h.worker.run_once()
    one, two = h.rt.created
    assert (
        one["run_id"] == first["id"] and one["history"] == [] and "PAW Admin" in one["instructions"]
    )
    assert two["history"] == [
        {"role": "user", "content": "my name is Bharat"},
        {"role": "assistant", "content": "Nice to meet you"},
    ]


def test_failed_runs_remain_in_history_with_their_failed_outcome(h):
    s = h.session()
    h.send(s, message="bad turn")
    h.rt.script = lambda hid: [("run.failed", {"error": "boom"})]
    h.worker.run_once()
    h.send(s, message="next")
    h.rt.script = lambda hid: [completed("ok")]
    h.worker.run_once()
    assert h.rt.created[1]["history"] == [
        {"role": "user", "content": "bad turn"},
        {"role": "assistant", "content": "Run ended: failed."},
    ]


def test_runtime_reported_failure_is_failed(h):
    run = h.send(h.session())
    h.rt.script = lambda hid: [delta("par"), ("run.failed", {"error": "model exploded"})]
    h.worker.run_once()
    r = h.run(run["id"])
    assert r["state"] == "FAILED" and r["error_code"] == "runtime_error"
    assert h.types(run["id"])[-1] == "run.failed"


def test_a_completion_that_is_not_clean_is_interrupted_not_succeeded(h):
    run = h.send(h.session())
    h.rt.script = lambda hid: [
        delta("half"),
        ("run.interrupted", {"output": "half", "reason": "incomplete_turn"}),
    ]
    h.worker.run_once()
    r = h.run(run["id"])
    assert (
        r["state"] == "INTERRUPTED"
        and r["output"] == "half"
        and r["error_code"] == "incomplete_turn"
    )


def test_lost_stream_keeps_partial_output_and_is_never_success(h):
    run = h.send(h.session())
    h.rt.script = lambda hid: [delta("par"), delta("tial")]
    h.rt.stream_error = RuntimeError_("stream_lost", "gone")
    h.worker.run_once()
    r = h.run(run["id"])
    assert (
        r["state"] == "INTERRUPTED"
        and r["output"] == "partial"
        and r["error_code"] == "stream_lost"
    )
    assert h.types(run["id"])[-1] == "run.interrupted"


@pytest.mark.parametrize(("code", "state", "stored"), [
    ("rejected", "FAILED", "dispatch_rejected"), ("auth", "FAILED", "dispatch_auth"),
    ("unavailable", "INTERRUPTED", "dispatch_unknown"), ("timeout", "INTERRUPTED", "dispatch_unknown"),
])  # fmt: skip
def test_dispatch_failures(h, code, state, stored):
    run = h.send(h.session())
    h.rt.create_error = RuntimeError_(code)
    h.worker.run_once()
    r = h.run(run["id"])
    assert (r["state"], r["error_code"]) == (state, stored)


def test_unexpected_crash_still_ends_the_run_and_releases_the_lease(h, env):
    run = h.send(h.session())

    def boom(hid):
        raise ValueError("bug")

    h.rt.script = boom
    h.worker.run_once()
    assert (
        h.run(run["id"])["state"] == "INTERRUPTED"
        and h.run(run["id"])["error_code"] == "worker_error"
    )
    assert sql(env, "SELECT count(*) FROM agent_run_leases") == [(0,)]


def test_a_stream_that_ends_without_a_terminal_event_is_interrupted(h):
    run = h.send(h.session())
    h.rt.script = lambda hid: [delta("x")]
    h.worker.run_once()
    assert (
        h.run(run["id"])["error_code"] == "no_terminal_event"
        and h.run(run["id"])["state"] == "INTERRUPTED"
    )


def test_replayed_source_events_are_stored_once(h):
    run = h.send(h.session())
    h.rt.script = lambda hid: [delta("a"), delta("b"), completed("ab")]
    orig = h.rt.stream_events

    def twice(hid, after_seq=-1):
        evs = list(orig(hid))
        yield from evs[:2]
        yield from evs  # a reconnect replays from the start

    h.rt.stream_events = twice
    h.worker.run_once()
    assert h.run(run["id"])["state"] == "SUCCEEDED"
    assert h.types(run["id"]).count("message.delta") == 2


def test_runs_are_processed_one_at_a_time_oldest_first(h, env):
    a, b = h.send(h.session(), message="A"), h.send(h.session(), message="B")
    seen = []

    def watch(i):
        seen.append(
            sql(env, "SELECT count(*) FROM agent_runs WHERE state IN ('DISPATCHING','RUNNING')")[0][
                0
            ]
        )

    h.rt.between_events = watch
    assert (
        h.worker.run_once()
        and h.run(a["id"])["state"] == "SUCCEEDED"
        and h.run(b["id"])["state"] == "QUEUED"
    )
    assert h.worker.run_once() and h.run(b["id"])["state"] == "SUCCEEDED"
    assert set(seen) == {1}


def test_a_held_lease_blocks_every_other_run(h, env):
    run = h.send(h.session())
    rid = h.worker.runtime_id
    other = sql(
        env,
        "INSERT INTO agent_sessions (owner, workspace_id) SELECT 'owner', id FROM workspaces LIMIT 1 RETURNING id",
    )[0][0]
    busy = sql(
        env,
        "INSERT INTO agent_runs (session_id, actor_id, client_request_id, user_message, state) VALUES (%s,'owner','x','y','RUNNING') RETURNING id",
        other,
    )[0][0]
    sql(env, "INSERT INTO agent_run_leases (runtime_id, run_id) VALUES (%s, %s)", rid, busy)
    assert h.worker.run_once() is False and h.run(run["id"])["state"] == "QUEUED"


def test_restart_recovery_interrupts_orphaned_runs_and_keeps_partial_output(h, env):
    run = h.send(h.session())
    with h.pool.connection() as conn:
        claimed = lc.claim_next(conn, h.worker.runtime_id)
        assert lc.move(conn, claimed["id"], "RUNNING", hermes_run_id="h1")
        lc.append_event(conn, claimed["id"], "message.delta", {"text": "part"}, "h1:0")
        lc.append_event(conn, claimed["id"], "message.delta", {"text": "ial"}, "h1:1")
    assert h.worker.recover() == 1
    r = h.run(run["id"])
    assert (r["state"], r["output"], r["error_code"]) == (
        "INTERRUPTED",
        "partial",
        "runtime_missing",
    )
    assert sql(env, "SELECT count(*) FROM agent_run_leases") == [(0,)]
    assert h.worker.recover() == 0
    h.send_next = h.send(h.session())  # the system keeps working afterwards
    assert h.worker.run_once() is True


# ---- cancel ------------------------------------------------------------------------------------------------------


def test_cancel_a_queued_run(h):
    run = h.send(h.session())
    r = h.c.post(f"/v1/agent-runs/{run['id']}/cancel", headers=AUTH)
    assert r.status_code == 200 and r.json()["state"] == "CANCELLED" and r.json()["finished_at"]
    assert h.types(run["id"])[-1] == "run.cancelled"
    assert h.worker.run_once() is False and h.rt.created == []


def _running(h):
    run = h.send(h.session())
    with h.pool.connection() as conn:
        claimed = lc.claim_next(conn, h.worker.runtime_id)
        lc.move(conn, claimed["id"], "RUNNING", hermes_run_id="hermes-1")
    return run


def test_cancel_a_running_run_asks_hermes_to_stop_then_settles_cancelled(h):
    run = _running(h)
    r = h.c.post(f"/v1/agent-runs/{run['id']}/cancel", headers=AUTH)
    assert r.json()["state"] == "CANCELLING" and h.rt.stopped == ["hermes-1"]
    again = h.c.post(f"/v1/agent-runs/{run['id']}/cancel", headers=AUTH)
    assert again.json()["state"] == "CANCELLING" and h.rt.stopped == [
        "hermes-1"
    ]  # idempotent: no second stop
    with h.pool.connection() as conn:
        lc.finish(
            conn,
            UUID(run["id"]),
            "CANCELLED",
            event_type="run.cancelled",
            payload={},
            source_event_id="hermes-1:9",
        )
    assert h.run(run["id"])["state"] == "CANCELLED"


def test_completion_racing_a_cancel_settles_cancelled_and_keeps_the_output(h):
    run = _running(h)
    h.c.post(f"/v1/agent-runs/{run['id']}/cancel", headers=AUTH)
    with h.pool.connection() as conn:
        lc.finish(
            conn,
            UUID(run["id"]),
            "SUCCEEDED",
            event_type="run.completed",
            payload={},
            output="finished anyway",
        )
    r = h.run(run["id"])
    assert r["state"] == "CANCELLED" and r["output"] == "finished anyway"
    assert h.types(run["id"])[-1] == "run.cancelled"  # event and state agree


def test_cancel_of_a_finished_run_changes_nothing(h):
    run = h.send(h.session())
    h.worker.run_once()
    r = h.c.post(f"/v1/agent-runs/{run['id']}/cancel", headers=AUTH)
    assert r.status_code == 200 and r.json()["state"] == "SUCCEEDED"


def test_cancel_while_dispatching_asks_to_retry(h):
    run = h.send(h.session())
    with h.pool.connection() as conn:
        lc.claim_next(conn, h.worker.runtime_id)
    r = h.c.post(f"/v1/agent-runs/{run['id']}/cancel", headers=AUTH)
    assert r.status_code == 409 and r.json()["code"] == "not_cancellable_yet"


def test_terminal_runs_are_immutable_through_the_lifecycle_api(h):
    run = h.send(h.session())
    h.worker.run_once()
    with h.pool.connection() as conn:
        assert lc.finish(conn, UUID(run["id"]), "FAILED") is None
        assert lc.move(conn, UUID(run["id"]), "RUNNING") is False
    assert h.run(run["id"])["state"] == "SUCCEEDED"


# ---- event stream ------------------------------------------------------------------------------------------------


def test_stream_replays_from_the_database_with_ids_and_ends_after_the_terminal_event(h):
    run = h.send(h.session())
    h.worker.run_once()
    r = h.c.get(f"/v1/agent-runs/{run['id']}/events", headers=AUTH)
    assert (
        r.headers["content-type"].startswith("text/event-stream")
        and r.headers["cache-control"] == "no-store"
    )
    evs = parse_sse(r.text)
    assert [e["seq"] for e in evs] == list(range(1, 7)) and evs[-1]["type"] == "run.completed"
    assert "id: 1\nevent: run.queued" in r.text


def test_reconnect_after_a_sequence_number_or_last_event_id_has_no_gaps_or_duplicates(h):
    run = h.send(h.session())
    h.worker.run_once()
    full = h.events(run["id"])
    assert [e["seq"] for e in h.events(run["id"], after=3)] == [4, 5, 6]
    r = h.c.get(f"/v1/agent-runs/{run['id']}/events", headers={**AUTH, "Last-Event-ID": "4"})
    assert [e["seq"] for e in parse_sse(r.text)] == [5, 6]
    assert [e["seq"] for e in h.events(run["id"], after=6)] == []
    assert full[:3] + h.events(run["id"], after=3) == full


def test_a_live_stream_delivers_events_as_the_worker_commits_them(h):
    run = h.send(h.session())
    h.rt.script = lambda hid: [delta("a"), delta("b"), delta("c"), completed("abc")]
    h.rt.between_events = lambda i: time.sleep(0.15)
    t = threading.Timer(0.3, h.worker.run_once)
    t.start()
    got = []
    with h.c.stream("GET", f"/v1/agent-runs/{run['id']}/events", headers=AUTH) as r:
        buf = ""
        for chunk in r.iter_text():
            buf += chunk
        got = parse_sse(buf)
    t.join()
    assert [e["seq"] for e in got] == list(range(1, len(got) + 1))
    assert [e["type"] for e in got if e["type"] == "message.delta"] == ["message.delta"] * 3
    assert got[0]["type"] == "run.queued" and got[-1]["type"] == "run.completed"


# ---- stop deadline (002A step 7) ---------------------------------------------------------------------------------


def test_a_stop_hermes_never_confirms_ends_interrupted_after_the_deadline(env):
    h = Harness(env, agent_stop_deadline_s=1)
    run = _running(h)
    with h.pool.connection() as conn:
        lc.append_event(conn, UUID(run["id"]), "message.delta", {"text": "so far"}, "hermes-1:0")
    h.c.post(f"/v1/agent-runs/{run['id']}/cancel", headers=AUTH)
    assert h.worker.reap() == 0  # within the deadline: still waiting for Hermes
    time.sleep(
        1.2
    )  # events are append-only, so the deadline is crossed by waiting, not by back-dating
    assert h.worker.reap() == 1
    r = h.run(run["id"])
    assert (r["state"], r["error_code"], r["output"]) == (
        "INTERRUPTED",
        "stop_not_confirmed",
        "so far",
    )
    assert h.types(run["id"])[-1] == "run.interrupted"
    assert sql(env, "SELECT count(*) FROM agent_run_leases") == [(0,)]
    assert h.worker.reap() == 0
    h.pool.close()


def test_events_arriving_after_the_deadline_are_not_stored_after_the_terminal_event(h):
    run = h.send(h.session())
    rid = UUID(run["id"])

    def script(hid):
        return [delta("a"), delta("late"), completed("a late")]

    def end_it(i):
        if i == 1:  # the reaper ends the run while the stream is still delivering
            with h.pool.connection() as conn:
                lc.request_cancel(conn, rid)
                lc.finish(conn, rid, "INTERRUPTED", error_code="stop_not_confirmed")

    h.rt.script, h.rt.between_events = script, end_it
    h.worker.run_once()
    # read the table, not the stream: the stream stops at the first terminal event and would hide later rows
    stored = [
        t
        for (t,) in sql(
            h.env, "SELECT type FROM agent_run_events WHERE run_id = %s ORDER BY seq", rid
        )
    ]
    assert stored[-1] == "run.interrupted" and "run.completed" not in stored
    assert h.run(run["id"])["state"] == "INTERRUPTED"


def test_reaper_leaves_other_states_alone(h):
    running = _running(h)
    queued = h.send(h.session())
    assert h.worker.reap() == 0
    assert h.run(queued["id"])["state"] == "QUEUED" and h.run(running["id"])["state"] == "RUNNING"


# ---- run deadlines (A1-08) ---------------------------------------------------------------------------------------


def test_the_deadline_is_set_once_from_the_profile_and_survives_a_worker_restart(h, env):
    run = h.send(h.session())
    h.worker.run_once()
    [(secs, first)] = sql(
        env,
        "SELECT extract(epoch FROM deadline_at - started_at), deadline_at FROM agent_runs WHERE id = %s",
        run["id"],
    )
    assert 599 <= secs <= 601  # the default profile's max_run_seconds
    AgentWorker(h.pool, h.rt, h.settings).recover()
    assert sql(env, "SELECT deadline_at FROM agent_runs WHERE id = %s", run["id"]) == [(first,)]


def _expired(h, env):
    run = _running(h)
    sql(
        env,
        "UPDATE agent_runs SET deadline_at = now() - interval '1 second' WHERE id = %s",
        run["id"],
    )
    return run


def test_a_run_before_its_deadline_is_left_alone(h, env):
    run = _running(h)
    sql(
        env,
        "UPDATE agent_runs SET deadline_at = now() + interval '1 hour' WHERE id = %s",
        run["id"],
    )
    assert h.worker.expire_deadlines() == 0 and h.rt.stopped == []
    assert h.run(run["id"])["state"] == "RUNNING"


def test_an_expired_run_is_asked_to_stop_once_and_keeps_the_reason(h, env):
    run = _expired(h, env)
    assert h.worker.expire_deadlines() == 1 and h.rt.stopped == ["hermes-1"]
    r = h.run(run["id"])
    assert (r["state"], r["error_code"]) == ("CANCELLING", "deadline_exceeded")
    assert h.worker.expire_deadlines() == 0 and h.rt.stopped == ["hermes-1"]  # idempotent
    with h.pool.connection() as conn:
        lc.finish(
            conn,
            UUID(run["id"]),
            "CANCELLED",
            event_type="run.cancelled",
            payload={},
            output="partial",
        )
    r = h.run(run["id"])
    assert (r["state"], r["error_code"], r["output"]) == (
        "CANCELLED",
        "deadline_exceeded",
        "partial",
    )


def test_an_expired_run_whose_stop_is_never_confirmed_ends_interrupted_with_its_partial_output(env):
    h = Harness(env, agent_stop_deadline_s=1)
    run = _expired(h, env)
    with h.pool.connection() as conn:
        lc.append_event(conn, UUID(run["id"]), "message.delta", {"text": "so far"}, "hermes-1:0")
    assert h.worker.expire_deadlines() == 1
    time.sleep(1.2)
    assert h.worker.reap() == 1
    r = h.run(run["id"])
    assert (r["state"], r["error_code"], r["output"]) == (
        "INTERRUPTED",
        "deadline_exceeded",
        "so far",
    )
    assert sql(env, "SELECT count(*) FROM agent_run_leases") == [(0,)]
    h.pool.close()


def test_a_failed_stop_request_still_leaves_the_run_cancelling_for_the_stop_deadline(h, env):
    run = _expired(h, env)

    def broken(hid):
        raise RuntimeError("runtime down")

    h.rt.stop_run = broken
    assert h.worker.expire_deadlines() == 1
    assert h.run(run["id"])["state"] == "CANCELLING"
