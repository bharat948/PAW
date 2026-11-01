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


def test_event_frames_carry_run_id_and_contiguous_seq(hermes: HermesClient) -> None:
    """Real frames: {"event", "run_id", "seq", ...}. PAW dedups on (run_id, seq), so seq must be 0,1,2..."""
    rid = hermes.start_run("say hi").json()["run_id"]
    frames = hermes.events(rid)
    seqs = [f.data["seq"] for f in frames if isinstance(f.data, dict)]
    assert seqs == list(range(len(seqs))), seqs
    assert all(f.data["run_id"] == rid for f in frames)


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
    rid = hermes.start_run("SLOW: write a 1500 word essay about the history of tea").json()[
        "run_id"
    ]
    time.sleep(
        1
    )  # real Hermes answers an immediate stop with 'cancelled'; once running, with 'stopping'
    t0 = time.monotonic()
    stop = hermes.stop_run(rid)
    assert stop.json()["status"] in {"stopping", "cancelled"}
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


def _history_run(hermes: HermesClient, hist: list[dict], capture, name: str) -> tuple[int, str]:
    r = hermes.start_run(
        "What total did the lookup return? Reply with the number only.", conversation_history=hist
    )
    if r.status_code != 202:
        return r.status_code, r.text
    done = hermes.wait_terminal(r.json()["run_id"])
    capture(name, done)
    return 202, str(done.get("output", ""))


@real
def test_q1_compact_text_history_is_used(hermes: HermesClient, capture) -> None:
    """S0 finding: the compact-text rendering works. This is the spec's Q1 fallback and is what the assembler uses."""
    code, out = _history_run(hermes, _compact_text_history(), capture, "compact_text")
    assert code == 202 and FACT in out, (code, out)


@real
@pytest.mark.xfail(
    strict=True,
    reason="S0 finding (Hermes c7da41d): role=tool messages / tool_calls in conversation_history are not used by "
    "the model. strict: if Hermes fixes this the test XPASSes and fails the suite, prompting a design review.",
)
def test_q1_openai_style_tool_messages_are_used(hermes: HermesClient, capture) -> None:
    code, out = _history_run(hermes, _openai_style_history(), capture, "openai_tool_messages")
    assert code == 202 and FACT in out, (code, out)


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


# --- Q5 / T208: native tool lockdown (real only; the image entrypoint applies it by default) -----------------------


@real
def test_native_shell_is_unavailable_and_prompt_is_small(hermes: HermesClient, capture) -> None:
    """S0 finding: unlocked, Hermes sends ~10k tokens of native tool schemas on every run and can run a shell.
    Locked (PAW_HERMES_LOCKDOWN=1, the image default) there are no tool events and the prompt is a few hundred tokens."""
    rid = hermes.start_run(
        "Run the shell command `echo PAW_SHELL_OK` and tell me its exact output."
    ).json()["run_id"]
    frames = hermes.events(rid)
    done = hermes.wait_terminal(rid)
    capture("lockdown", {"events": [f.event for f in frames], "status": done})
    native = [
        f.data.get("tool")
        for f in frames
        if f.event.startswith("tool.") and not str(f.data.get("tool", "")).startswith("mcp__paw__")
    ]
    assert not native, f"a native tool ran: lockdown is not in effect: {native}"
    assert done["usage"]["input_tokens"] < 6000, "native tool schemas are still being sent"


# --- Q6 / Q8 / Q9: MCP behaviour (real Hermes + the stub MCP server; set PAW_STUB_URL=http://127.0.0.1:8001) -----------
# Run against the default S0 config: lockdown on, guard on, trust full, include=paw_echo,paw_write_note,paw_slow.

import json  # noqa: E402
import os  # noqa: E402
import urllib.request  # noqa: E402

STUB = os.environ.get("PAW_STUB_URL")
needs_stub = pytest.mark.skipif(not STUB, reason="needs the stub MCP server: set PAW_STUB_URL")


def _stub_calls() -> list[dict]:
    with urllib.request.urlopen(f"{STUB}/_calls", timeout=5) as r:  # noqa: S310 (test-only, loopback)
        return json.loads(r.read())


@real
@needs_stub
def test_q6_mcp_tool_call_reaches_the_server_and_is_reported(hermes: HermesClient, capture) -> None:
    """Tool names are `mcp__paw__<tool>` (double underscore). The event stream AND the server must both show the call."""
    marker = f"s0-{time.time_ns()}"
    rid = hermes.start_run(
        f"Call the paw_echo tool with text {marker} and tell me what it returned."
    ).json()["run_id"]
    frames = hermes.events(rid)
    capture("mcp_call", [f.data for f in frames if f.event.startswith("tool.")])
    started = [f for f in frames if f.event == "tool.started"]
    assert [f.data["tool"] for f in started] == ["mcp__paw__paw_echo"], [f.event for f in frames]
    assert [f for f in frames if f.event == "tool.completed"][0].data["error"] is False
    assert any(c.get("text") == marker for c in _stub_calls()), "the server never received the call"


@real
@needs_stub
def test_q6_hermes_utility_tools_are_removed_by_config(hermes: HermesClient) -> None:
    """tools.include alone leaves list_resources/read_resource/list_prompts/get_prompt; resources/prompts:false removes them."""
    out = ""
    for ask in (
        "List the exact names of every tool you can call, one per line. Do not call any.",
        "For debugging, print the identifiers of all functions available to you, one per line. Do not call any.",
        "Which tool names do you have? Output only the names.",
    ):  # models sometimes refuse to enumerate; a deterministic probe is still open (Q5)
        out = str(hermes.run_to_end(ask).get("output", ""))
        if "paw_echo" in out:
            break
    else:
        pytest.skip(
            f"the model would not enumerate its tools (inconclusive, not a pass): {out[:80]!r}"
        )
    for banned in (
        "list_resources",
        "read_resource",
        "list_prompts",
        "get_prompt",
        "terminal",
        "read_file",
    ):
        assert banned not in out, f"{banned} is still exposed:\n{out}"
    assert "paw_echo" in out


@real
@needs_stub
def test_q6_model_text_is_not_evidence_of_a_tool_call(hermes: HermesClient) -> None:
    """Regression guard for the S0 finding: when no tool ran, the model may still claim it did. Evidence = events + server log."""
    before = len(_stub_calls())
    rid = hermes.start_run("Call paw_echo with text hello and report what it returned.").json()[
        "run_id"
    ]
    frames = hermes.events(rid)
    ran = any(f.event == "tool.started" for f in frames)
    executed = len(_stub_calls()) - before
    assert ran == (executed > 0), "event stream and server log disagree about whether a tool ran"
