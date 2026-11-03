"""HermesAdapter against a REAL pinned Hermès (PAW_HERMES_URL + PAW_HERMES_KEY; PAW_STUB_URL for the tool test).

Costs a few cents. Skipped otherwise. This is what proves the fake in test_hermes_adapter.py has not drifted.
"""

import json
import os
import urllib.request
import uuid

import pytest

from paw_server.modules.runtime.hermes_adapter import HermesAdapter
from paw_server.modules.runtime.protocol import RuntimeError_

URL, KEY, STUB = (os.environ.get(k) for k in ("PAW_HERMES_URL", "PAW_HERMES_KEY", "PAW_STUB_URL"))
pytestmark = [
    pytest.mark.hermes_real,
    pytest.mark.skipif(not (URL and KEY), reason="needs PAW_HERMES_URL and PAW_HERMES_KEY"),
]


@pytest.fixture
def a() -> HermesAdapter:
    assert URL and KEY
    return HermesAdapter(URL, KEY)


def stub_calls() -> list[dict]:
    with urllib.request.urlopen(f"{STUB}/_calls", timeout=5) as r:  # noqa: S310 (loopback test server)
        return json.loads(r.read())


def test_real_is_ready_and_healthy(a):
    assert a.missing_capabilities() == []
    assert a.healthy()


def test_real_wrong_key_is_an_auth_error():
    assert URL
    bad = HermesAdapter(URL, "wrong-key")
    with pytest.raises(RuntimeError_) as e:
        bad.create_run(run_id=str(uuid.uuid4()), input="hi")
    assert e.value.code == "auth"
    assert bad.missing_capabilities() == ["unreachable:auth"]


def test_real_text_run_streams_contiguous_events_and_completes(a):
    h = a.create_run(run_id=str(uuid.uuid4()), input="Reply with exactly the word: pong")
    evs = list(a.stream_events(h.hermes_run_id))
    seqs = [int(e.source_event_id.rsplit(":", 1)[1]) for e in evs]
    assert seqs == list(range(len(seqs)))
    assert [e.terminal for e in evs].count(True) == 1 and evs[-1].type == "run.completed"
    done = evs[-1].payload
    assert "pong" in done["output"].lower() and done["usage"]["input_tokens"] > 0
    assert (
        "".join(e.payload["text"] for e in evs if e.type == "message.delta").strip()
        == done["output"].strip()
    )
    snap = a.get_run(h.hermes_run_id)
    assert snap.status == "completed" and snap.usage and snap.usage.output_tokens > 0


def test_real_same_run_id_never_starts_a_second_run(a):
    rid = str(uuid.uuid4())
    first = a.create_run(run_id=rid, input="Reply with exactly the word: one")
    again = a.create_run(run_id=rid, input="Reply with exactly the word: one")
    assert again.hermes_run_id == first.hermes_run_id and again.replayed is True
    list(a.stream_events(first.hermes_run_id))


def test_real_reconnect_after_completion_replays_without_duplicates(a):
    h = a.create_run(run_id=str(uuid.uuid4()), input="Reply with exactly the word: again")
    first = [e.source_event_id for e in a.stream_events(h.hermes_run_id)]
    second = [e.source_event_id for e in a.stream_events(h.hermes_run_id)]
    assert second == first  # the full history is available after the run ends; ids are stable
    assert list(a.stream_events(h.hermes_run_id, after_seq=len(first) - 1)) == []


def test_real_stop_ends_in_cancelled(a):
    h = a.create_run(
        run_id=str(uuid.uuid4()),
        input="Count slowly from 1 to 500, one number per line, with a short comment each.",
    )
    a.stop_run(h.hermes_run_id)
    evs = list(a.stream_events(h.hermes_run_id))
    assert evs[-1].type == "run.cancelled"
    assert a.get_run(h.hermes_run_id).status == "cancelled"


@pytest.mark.skipif(not STUB, reason="needs the stub tool server (PAW_STUB_URL)")
def test_real_gateway_tool_call_is_named_without_prefix_and_not_native(a):
    marker = f"adapter-{uuid.uuid4().hex[:8]}"
    h = a.create_run(
        run_id=str(uuid.uuid4()),
        input=f"Call paw_echo with text {marker} and tell me what came back.",
    )
    evs = list(a.stream_events(h.hermes_run_id))
    tools = [e for e in evs if e.type.startswith("tool.")]
    assert [e.type for e in tools] == ["tool.started", "tool.completed"]
    assert all(e.payload["tool"] == "paw_echo" and e.payload["native"] is False for e in tools)
    assert tools[1].payload["error"] is False
    assert any(c.get("text") == marker for c in stub_calls()), "the tool server never saw the call"
    assert not [e for e in evs if e.type == "unknown"], [
        e.payload for e in evs if e.type == "unknown"
    ]
