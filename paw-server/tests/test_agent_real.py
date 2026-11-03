"""The whole server path (API -> PostgreSQL -> worker -> adapter -> REAL Hermes) with a real conversation.

Needs PAW_HERMES_URL and PAW_HERMES_KEY (see deploy/hermes/README.md) and PAW_TEST_DATABASE_URL. Costs a few cents.
"""

import os

import pytest

from paw_server.modules.runtime.hermes_adapter import HermesAdapter
from tests.test_agent_integration import Harness

pytestmark = [
    pytest.mark.integration,
    pytest.mark.hermes_real,
    pytest.mark.skipif(not os.environ.get("PAW_HERMES_URL"), reason="needs a real Hermes"),
]


@pytest.fixture
def h(env):
    rt = HermesAdapter(os.environ["PAW_HERMES_URL"], os.environ["PAW_HERMES_KEY"])
    x = Harness(env, rt)
    yield x
    x.pool.close()


def test_conversation_with_real_hermes_remembers_earlier_turns_from_postgres(h):
    s = h.session()
    first = h.send(s, message="My favourite colour is teal. Reply with just: noted")
    assert h.worker.run_once()
    r1 = h.run(first["id"])
    assert (
        r1["state"] == "SUCCEEDED"
        and "noted" in r1["output"].lower()
        and r1["usage"]["input_tokens"] > 0
    )

    second = h.send(s, message="What is my favourite colour? Answer in one word.")
    assert h.worker.run_once()
    r2 = h.run(second["id"])
    assert (
        r2["state"] == "SUCCEEDED" and "teal" in r2["output"].lower()
    )  # the history came from PostgreSQL

    evs = h.events(second["id"])
    assert [e["seq"] for e in evs] == list(range(1, len(evs) + 1))
    assert evs[-1]["type"] == "run.completed" and any(e["type"] == "message.delta" for e in evs)
    assert not [e for e in evs if e["type"] == "unknown"]


def test_real_cancel_through_the_api(h):
    s = h.session()
    run = h.send(
        s, message="Count from 1 to 400, one number per line, with a short remark each time."
    )
    import threading
    import time

    t = threading.Thread(target=h.worker.run_once)
    t.start()
    for _ in range(100):
        if h.run(run["id"])["state"] == "RUNNING":
            break
        time.sleep(0.1)
    time.sleep(0.5)
    r = h.c.post(
        f"/v1/agent-runs/{run['id']}/cancel",
        headers={"Authorization": "Bearer test-owner-token-0123456789"},
    )
    assert r.status_code == 200
    t.join(timeout=30)
    final = h.run(run["id"])
    assert final["state"] == "CANCELLED" and final["finished_at"]
