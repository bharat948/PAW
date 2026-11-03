"""The normaliser against frames captured from the real pinned Hermès (docs/paw-002/captures)."""

import json
from pathlib import Path
from typing import Any

import pytest

from paw_server.modules.runtime.normaliser import normalise, tool_name, usage_from

CAP = Path(__file__).resolve().parents[3] / "docs" / "paw-002" / "captures"


def frames(name: str) -> list[dict[str, Any]]:
    data = json.loads(next(CAP.glob(f"*{name}*")).read_text())
    return [f.get("data", f) if isinstance(f.get("data"), dict) else f for f in data]


def test_real_text_run_normalises_to_deltas_then_one_completed():
    evs = [normalise(f, "run_x") for f in frames("exactly_one_terminal")]
    assert [e.type for e in evs] == ["message.delta", "message.delta", "run.completed"]
    assert [e.payload["text"] for e in evs[:2]] == ["Hi", "."]
    assert [e.source_event_id for e in evs] == ["run_x:0", "run_x:1", "run_x:2"]
    done = evs[2].payload
    assert done["output"] == "Hi."
    assert done["usage"] == {
        "input_tokens": 2312,
        "output_tokens": 4,
        "cache_read": None,
        "cache_write": None,
    }
    assert evs[2].terminal and not evs[0].terminal


def test_real_mcp_tool_call_maps_the_prefixed_name_and_is_not_native():
    started, completed = (normalise(f, "r") for f in frames("mcp_call"))
    assert started.type == "tool.started" and completed.type == "tool.completed"
    assert started.payload["tool"] == "paw_echo" and started.payload["native"] is False
    assert started.payload["call_id"] == completed.payload["call_id"] != ""
    assert completed.payload["error"] is False


def test_native_tool_is_flagged_so_the_inspector_shows_a_lockdown_breach():
    ev = normalise({"event": "tool.started", "tool": "terminal", "seq": 0, "preview": "ls"}, "r")
    assert ev.payload["native"] is True and ev.payload["tool"] == "terminal"
    assert tool_name("mcp__paw__docs_search") == ("docs_search", False)
    assert tool_name("mcp__other__x") == (
        "mcp__other__x",
        True,
    )  # another MCP server is not the PAW gateway


@pytest.mark.parametrize(
    "extra", [{"completed": False}, {"partial": True}, {"interrupted": True}, {}]
)
def test_a_completed_event_that_is_not_cleanly_completed_is_never_success(extra):
    ev = normalise({"event": "run.completed", "seq": 3, "output": "half", **extra}, "r")
    assert ev.type == "run.interrupted" and ev.payload["output"] == "half" and ev.terminal


def test_clean_completion_only_when_explicitly_completed():
    ev = normalise(
        {
            "event": "run.completed",
            "seq": 3,
            "output": "ok",
            "completed": True,
            "partial": False,
            "interrupted": False,
        },
        "r",
    )
    assert ev.type == "run.completed"


def test_failed_cancelled_and_interrupted_are_terminal():
    assert normalise({"event": "run.failed", "seq": 1, "error": "boom"}, "r").type == "run.failed"
    c = normalise({"event": "run.cancelled", "seq": 1, "turn_exit_reason": "stopped"}, "r")
    assert c.type == "run.cancelled" and c.terminal
    assert normalise({"event": "run.interrupted", "seq": 1}, "r").terminal


def test_approval_request_is_surfaced():
    ev = normalise(
        {"event": "approval.request", "seq": 2, "prompt_id": "p1", "description": "write file"}, "r"
    )
    assert ev.type == "approval.requested" and ev.payload["prompt_id"] == "p1"


def test_unknown_events_are_kept_visible_and_bounded():
    ev = normalise({"event": "reasoning.delta", "seq": 4, "blob": "x" * 10_000}, "r")
    assert ev.type == "unknown" and ev.payload["source_type"] == "reasoning.delta"
    assert len(ev.payload["raw"]) <= 2000 and not ev.terminal


@pytest.mark.parametrize(
    "bad",
    [
        {"event": "message.delta"},
        {"event": "message.delta", "seq": "1"},
        {"event": "message.delta", "seq": True},
    ],
)
def test_frames_without_a_usable_seq_are_ignored(bad):
    assert normalise(bad, "r") is None


def test_usage_parsing_is_defensive():
    assert (
        usage_from({"input_tokens": 1, "output_tokens": 2, "cache_read_tokens": 5}).cache_read == 5
    )
    assert (
        usage_from({"input_tokens": 1}) is None
        and usage_from(None) is None
        and usage_from("x") is None
    )
    assert usage_from({"input_tokens": "a", "output_tokens": 2}) is None
