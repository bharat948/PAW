from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator

from paw_server.api.deps import CurrentActor
from paw_server.modules.chat import service
from paw_server.modules.chat.service import ChatTurn

router = APIRouter(tags=["chat"])


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]  # clients can never inject a system message
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)
    top_k: int = Field(default=6, ge=1, le=12)
    document_ids: list[UUID] | None = Field(default=None, max_length=50)

    @field_validator("messages")
    @classmethod
    def _last_is_user(cls, v: list[ChatMessage]) -> list[ChatMessage]:
        if v[-1].role != "user" or not v[-1].content.strip():
            raise ValueError("the last message must be a non-empty user message")
        return v


@router.post("/v1/chat")
def chat(request: Request, actor: CurrentActor, body: ChatRequest) -> StreamingResponse:
    """Server-sent events: `retrieval` (query + sources + trace), `token`*, then `done` or `error`."""
    st = request.app.state
    st.chat_limiter.check(actor.id)
    gen = service.run_chat(
        st.pool,
        st.embedder,
        st.llm,
        st.settings,
        actor,
        messages=[ChatTurn(m.role, m.content) for m in body.messages],
        top_k=body.top_k,
        document_ids=body.document_ids,
    )
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},  # no proxy buffering
    )
