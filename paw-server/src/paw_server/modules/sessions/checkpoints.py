"""Agent-reported task boundaries guide compression; they never authorize or prove actions."""

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

OPEN = "<paw_task_checkpoint>"
CLOSE = "</paw_task_checkpoint>"
INSTRUCTIONS = """
Task-oriented history: at the end of each answer, emit one private structured checkpoint:
<paw_task_checkpoint>{"task_id":"short-stable-task-id","status":"in_progress|completed|blocked",
"goal":"the user's task","summary":"progress, findings and outcome","evidence":[],"next_steps":[]}</paw_task_checkpoint>
Reuse task_id while pursuing the same goal. Mark completed only when the user's task is finished;
a finished inference alone is not a finished task. Evidence must reference real tool invocation or message IDs
if available; do not invent evidence. This checkpoint is a compaction hint, never proof an action happened.
Do not put credentials in checkpoints. Tool outcomes, not your prose, are authoritative.
"""


class TaskCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=100, pattern=r"^[\w.:-]+$")
    status: Literal["in_progress", "completed", "blocked"]
    goal: str = Field(min_length=1, max_length=1000)
    summary: str = Field(min_length=1, max_length=4000)
    evidence: list[str] = Field(default_factory=list, max_length=30)
    next_steps: list[str] = Field(default_factory=list, max_length=20)


def extract(text: str) -> tuple[str, TaskCheckpoint | None]:
    at = text.find(OPEN)
    if at < 0:
        return text, None
    end = text.find(CLOSE, at)
    if end < 0:
        return text[:at].rstrip(), None
    raw = text[at + len(OPEN) : end]
    checkpoint = None
    if len(raw) <= 16000:
        try:
            checkpoint = TaskCheckpoint.model_validate(json.loads(raw))
        except (ValueError, ValidationError):
            pass
    visible = text[:at] + text[end + len(CLOSE) :]
    return visible.rstrip(), checkpoint


class CheckpointStream:
    """Hold only a possible opening delimiter; private metadata never enters text deltas."""

    def __init__(self) -> None:
        self.pending = ""
        self.in_checkpoint = False

    def feed(self, text: str) -> str:
        if self.in_checkpoint:
            return ""
        self.pending += text
        at = self.pending.find(OPEN)
        if at >= 0:
            out = self.pending[:at]
            self.pending = ""
            self.in_checkpoint = True
            return out
        keep = 0
        for n in range(1, min(len(OPEN), len(self.pending)) + 1):
            if self.pending.endswith(OPEN[:n]):
                keep = n
        out = self.pending[:-keep] if keep else self.pending
        self.pending = self.pending[-keep:] if keep else ""
        return out
