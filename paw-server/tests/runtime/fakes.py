from collections.abc import Callable, Iterator
from typing import Any

from paw_server.modules.runtime.protocol import RunHandle, RuntimeError_, RuntimeEvent


def delta(text: str) -> tuple[str, dict[str, Any]]:
    return "message.delta", {"text": text}


def completed(output: str, tin: int = 11, tout: int = 7) -> tuple[str, dict[str, Any]]:
    return "run.completed", {
        "output": output,
        "usage": {
            "input_tokens": tin,
            "output_tokens": tout,
            "cache_read": None,
            "cache_write": None,
        },
    }


class FakeRuntime:
    """AgentRuntime test double. `script(hermes_run_id)` returns the (type, payload) events to stream; after them
    `stream_error` (if set) is raised, which is how a lost stream is simulated."""

    def __init__(
        self, script: Callable[[str], list[tuple[str, dict[str, Any]]]] | None = None
    ) -> None:
        self.script = script or (lambda hid: [delta("he"), delta("llo"), completed("hello")])
        self.create_error: RuntimeError_ | None = None
        self.stream_error: Exception | None = None
        self.created: list[dict[str, Any]] = []
        self.stopped: list[str] = []
        self.missing: list[str] = []
        self.between_events: Callable[[int], None] | None = (
            None  # test hook, called before each event
        )

    def create_run(self, *, run_id: str, input: str, instructions: str | None = None,
                   history: list[dict[str, Any]] | None = None) -> RunHandle:  # fmt: skip
        self.created.append(
            {"run_id": run_id, "input": input, "instructions": instructions, "history": history}
        )
        if self.create_error:
            raise self.create_error
        return RunHandle(f"h-{run_id}", False)

    def stream_events(self, hermes_run_id: str, after_seq: int = -1) -> Iterator[RuntimeEvent]:
        for i, (t, p) in enumerate(self.script(hermes_run_id)):
            if self.between_events:
                self.between_events(i)
            yield RuntimeEvent(t, p, f"{hermes_run_id}:{i}")
        if self.stream_error:
            raise self.stream_error

    def stop_run(self, hermes_run_id: str) -> None:
        self.stopped.append(hermes_run_id)

    def missing_capabilities(self) -> list[str]:
        return self.missing

    def healthy(self) -> bool:
        return True
