"""PAW-002 spike S0 contract tests (Q1-Q8 from the spec, section 14).

Un-marked tests assert DOCUMENTED protocol behaviour and run against the fake or a real Hermes.
`hermes_real` tests assert model/runtime behaviour the docs do not specify; they only run against a real image.
Q5 (tool lockdown), Q6 (MCP) and Q8 (hook block) need the container config and the PAW MCP gateway, so they are
NOT in this file yet; see docs/paw-002/plan.md. A skipped test is not a pass.
"""

import time

import pytest

from .hermes_client import TERMINAL_EVENTS, HermesClient

real = pytest.mark.hermes_real

# --- 002A-R01 / R02: auth and capabilities (documented) ---------------------------------------------------------


def test_requests_without_key_are_rejected(hermes: HermesClient) -> None:
    assert hermes.capabilities(auth=False).status_code == 401
    assert hermes.start_run("hi", auth=False).status_code == 401


def test_capabilities_advertise_what_the_adapter_needs(hermes: HermesClient, capture) -> None:
    body = hermes.capabilities().json()
    capture("capabilities", body)
    assert body["auth"]["required"] is True
    f = body["features"]
    for need in ("run_submission", "run_status", "run_events_sse", "run_stop"):
        assert f.get(need) is True, f"readiness must fail: {need} not advertised"


# --- Q3: run submission, event schema, usage (documented subset) ----------------------------------------------


def test_run_submission_returns_a_run_id(hermes: HermesClient) -> None:
    r = hermes.start_run("say hi")
    assert r.status_code == 202
    assert r.json()["run_id"]
    hermes.wait_terminal(r.json()["run_id"])


def test_events_end_in_exactly_one_terminal_event(hermes: HermesClient, capture) -> None:
    rid = hermes.start_run("say hi").json()["run_id"]
    frames = hermes.events(rid)
    capture("events", [{"event": f.event, "data": f.data, "comments": f.comments} for f in frames])
    names = [f.event for f in frames]
    assert sum(n in TERMINAL_EVENTS for n in names) == 1
    assert names[-1] in TERMINAL_EVENTS


def test_terminal_status_carries_usage(hermes: HermesClient, capture) -> None:
    done = hermes.run_to_end("say hi")
    capture("status", done)
    assert done["status"] == "completed"
    u = done["usage"]
    assert {
        "input_tokens",
        "output_tokens",
    } <= u.keys()  # 002A-R07 needs these; served model: see Q3 capture


def test_idempotency_key_replays_the_same_run(hermes: HermesClient) -> None:
    key = f"s0-{time.time_ns()}"
    a = hermes.start_run("say hi", idempotency_key=key)
    b = hermes.start_run("say hi", idempotency_key=key)
    assert a.status_code == b.status_code == 202
    assert a.json()["run_id"] == b.json()["run_id"]
    assert b.headers.get("Idempotency-Replayed") == "true"


# --- Q4: stop latency (documented: returns 'stopping', settles as cancelled) ------------------------------------


def test_stop_settles_as_cancelled(hermes: HermesClient, capture) -> None:
    rid = hermes.start_run("SLOW: count to one thousand slowly, one number per line").json()[
        "run_id"
    ]
    t0 = time.monotonic()
    stop = hermes.stop_run(rid)
    assert stop.json()["status"] == "stopping"
    final = hermes.wait_terminal(rid, deadline_s=60)
    capture("stop", {"latency_s": round(time.monotonic() - t0, 2), "final": final})
    assert final["status"] == "cancelled"


# --- Q1 / Q2 / Q7: model-dependent behaviour the docs do NOT specify (real only) ---------------------------------

FACT = "42317"


def _openai_style_history() -> list[dict]:
    return [
        {"role": "user", "content": "Look up the order total."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "lookup_total", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": f'{{"total": {FACT}}}'},
        {"role": "assistant", "content": "I looked it up."},
    ]


def _compact_text_history() -> list[dict]:
    return [
        {"role": "user", "content": "Look up the order total."},
        {
            "role": "assistant",
            "content": f'[tool lookup_total returned {{"total": {FACT}}}] I looked it up.',
        },
    ]


@real
@pytest.mark.parametrize("fmt", ["openai_tool_messages", "compact_text"])
def test_q1_history_fidelity(hermes: HermesClient, fmt: str, capture) -> None:
    """Records WHICH history rendering the model uses. The spec needs at least one to pass (else Option A)."""
    hist = _openai_style_history() if fmt == "openai_tool_messages" else _compact_text_history()
    r = hermes.start_run(
        "What total did the lookup return? Reply with the number only.", conversation_history=hist
    )
    assert r.status_code == 202, (
        r.text
    )  # a 4xx here is itself the finding: the format is not accepted
    done = hermes.wait_terminal(r.json()["run_id"])
    capture(fmt, done)
    assert FACT in str(done.get("output", "")), f"{fmt}: model did not use the history"


@real
def test_q2_no_hidden_history_between_runs(hermes: HermesClient) -> None:
    hermes.run_to_end("Remember this secret word for later: PELICAN-88.")
    done = hermes.run_to_end(
        "What secret word did I ask you to remember? If you do not know, say UNKNOWN."
    )
    assert "PELICAN-88" not in str(done.get("output", "")), (
        "Hermes merged hidden history without a session key"
    )


@real
def test_q7_instructions_are_followed(hermes: HermesClient) -> None:
    done = hermes.run_to_end(
        "Name one primary colour.", instructions="End every reply with the exact token ZEBRA-7."
    )
    assert "ZEBRA-7" in str(done.get("output", ""))
