"""The agent runtime port. The rest of PAW depends on this, never on Hermès's wire format.

Event types PAW stores (stable, independent of Hermès):
  message.delta {text} · tool.started {tool, call_id, preview, native} · tool.completed {tool, call_id, duration_s, error, native}
  approval.requested {prompt_id, description} · unknown {source_type, raw}
  terminal, exactly one per run: run.completed {output, usage} · run.failed {error} · run.cancelled {} ·
  run.interrupted {output, reason}
A run is only ever SUCCEEDED because `run.completed` was received (or recovered from the run status, flagged `synthesized`).
"""

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Protocol

TERMINAL_TYPES = frozenset({"run.completed", "run.failed", "run.cancelled", "run.interrupted"})


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    cache_read: int | None = None  # None = the runtime did not report it
    cache_write: int | None = None


@dataclass(frozen=True)
class RuntimeEvent:
    type: str
    payload: dict[str, Any]
    source_event_id: str  # "<hermes run id>:<seq>"; unique per run, used to drop duplicates

    @property
    def terminal(self) -> bool:
        return self.type in TERMINAL_TYPES


@dataclass(frozen=True)
class RunHandle:
    hermes_run_id: str
    replayed: (
        bool  # True when the runtime recognised the idempotency key and returned the existing run
    )


@dataclass(frozen=True)
class RunSnapshot:
    status: (
        str  # started|running|waiting_for_approval|completed|failed|cancelled|interrupted|unknown
    )
    output: str = ""
    usage: Usage | None = None
    pending_controls: list[dict[str, Any]] = field(default_factory=list)

    @property
    def terminal(self) -> bool:
        return self.status in {"completed", "failed", "cancelled", "interrupted"}


class RuntimeError_(Exception):
    """A failure talking to the runtime. `code` is machine-safe; the message never carries upstream bodies.

    auth: our credential was refused · unavailable: network/5xx (retryable) · timeout · rejected: the runtime refused
    the request (not retryable) · capability_missing · protocol: the runtime broke the contract · stream_lost: the
    event stream ended and the run's final state could not be established
    """

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code, self.detail = code, detail

    @property
    def retryable(self) -> bool:
        return self.code in {"unavailable", "timeout"}


class AgentRuntime(Protocol):
    def create_run(
        self,
        *,
        run_id: str,
        input: str,
        instructions: str | None = None,
        history: list[dict[str, Any]] | None = None,
    ) -> RunHandle:
        """Start a run. `run_id` (the PAW run id) is the idempotency key: calling again can never start a second run."""
        ...

    def stream_events(self, hermes_run_id: str, after_seq: int = -1) -> Iterator[RuntimeEvent]:
        """Events in order, deduplicated, ending with exactly one terminal event, or RuntimeError_(stream_lost)."""
        ...

    def get_run(self, hermes_run_id: str) -> RunSnapshot: ...

    def stop_run(self, hermes_run_id: str) -> None: ...

    def missing_capabilities(self) -> list[str]:
        """Empty when the runtime is fit to use. Feeds /ready."""
        ...

    def healthy(self) -> bool: ...
