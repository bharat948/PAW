"""LLM provider adapter. Business code depends only on `LLM`; no provider logic elsewhere."""

import json
import re
from collections.abc import Iterator
from typing import Protocol

import httpx

from paw_server.core.config import Settings

Message = dict[str, str]


class LLMError(Exception):
    """The language model is unavailable or rejected the request (message is user-safe)."""


class LLM(Protocol):
    model: str
    generative: bool

    def complete(self, messages: list[Message], max_tokens: int) -> str: ...
    def stream(self, messages: list[Message], max_tokens: int) -> Iterator[str]: ...


class FakeLLM:
    """Offline stand-in: no model. Returns the closest source sentences with citations.

    Lets the whole RAG flow (retrieval, citations, trace UI) be exercised without a key or cost.
    """

    model = "offline-extractive"
    generative = False

    def complete(self, messages: list[Message], max_tokens: int) -> str:
        # query rewriting: just reuse the latest user message
        return messages[-1]["content"].splitlines()[-1].strip()

    def stream(self, messages: list[Message], max_tokens: int) -> Iterator[str]:
        user = messages[-1]["content"]
        found = re.findall(
            r"^\[(\d+)\] .*?\n(.+?)(?=\n---\n|\Z)", user.split("SOURCES:", 1)[-1], re.S | re.M
        )
        yield "Offline demo mode: no language model is configured, so here are the closest passages.\n\n"
        for n, body in found[:3]:
            sentence = re.split(r"(?<=[.!?])\s", " ".join(body.split()), maxsplit=1)[0][:240]
            yield f"- {sentence} [{n}]\n"


class OpenAICompatLLM:
    generative = True

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        timeout_s: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            timeout=timeout_s,
            transport=transport,
            headers={"Authorization": f"Bearer {api_key}"},
        )

    def _payload(self, messages: list[Message], max_tokens: int, stream: bool) -> dict[str, object]:
        # newer OpenAI models take max_completion_tokens; send it, compatible servers ignore extras
        return {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "temperature": 0.2,
            "max_completion_tokens": max_tokens,
        }

    @staticmethod
    def _check(r: httpx.Response) -> None:
        if r.status_code == 429 or r.status_code >= 500:
            raise LLMError("The language model is busy or unavailable. Please try again.")
        if r.status_code in (401, 403):
            raise LLMError("The language model rejected the API credentials.")
        if r.status_code >= 400:
            raise LLMError("The language model rejected the request.")

    def complete(self, messages: list[Message], max_tokens: int) -> str:
        try:
            r = self._client.post(
                "/chat/completions", json=self._payload(messages, max_tokens, False)
            )
        except httpx.HTTPError as exc:
            raise LLMError("The language model could not be reached.") from exc
        self._check(r)
        try:
            return str(r.json()["choices"][0]["message"]["content"]).strip()
        except (KeyError, IndexError, ValueError) as exc:
            raise LLMError("The language model returned an unexpected response.") from exc

    def stream(self, messages: list[Message], max_tokens: int) -> Iterator[str]:
        try:
            with self._client.stream(
                "POST", "/chat/completions", json=self._payload(messages, max_tokens, True)
            ) as r:
                if r.status_code >= 400:
                    r.read()
                    self._check(r)
                for line in r.iter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        return
                    try:
                        delta = json.loads(data)["choices"][0]["delta"].get("content")
                    except (KeyError, IndexError, ValueError):
                        continue
                    if delta:
                        yield delta
        except httpx.HTTPError as exc:
            raise LLMError("The language model connection was interrupted.") from exc


def build_llm(s: Settings) -> LLM:
    if s.llm_provider == "openai":
        return OpenAICompatLLM(
            s.llm_api_base, s.llm_api_key or s.embedding_api_key, s.llm_model, s.llm_timeout_s
        )
    return FakeLLM()
