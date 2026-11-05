"""Conservative input-token bound for a chat request, used to size a spending reservation.

The reservation must never be smaller than what the provider will bill, so this over-counts on purpose:
the whole request body is tokenised (tool schemas as compact JSON, which renders at least as long as the
provider's own tool format), each message gets fixed framing overhead, and the total gets a relative and an
absolute margin. If the provider still reports more input than the bound, settlement refuses it and the
attempt stays reserved as uncertain (see service.settle); a bad estimate is flagged, never silently accepted.
"""

import json
from functools import lru_cache
from typing import Any

import tiktoken

from paw_server.modules.costs.pricing import CostError

PER_MESSAGE = 4  # role/separator framing per message
PRIMING = 3  # reply priming
MARGIN_PERCENT = 10
MARGIN_TOKENS = 256
TEXT_PARTS = ("text",)


@lru_cache(maxsize=8)
def encoding_for(model: str) -> tiktoken.Encoding:
    try:
        return tiktoken.encoding_for_model(model)
    except KeyError:
        raise CostError("unsupported_model") from None


def warm(models: list[str]) -> None:
    """Load every encoding now (they may download once). Called at start-up so a missing encoding stops the
    service instead of failing the first request."""
    for m in models:
        encoding_for(m).encode("warm")


def _text(enc: tiktoken.Encoding, s: Any) -> int:
    if not isinstance(s, str):
        raise CostError("unsupported_payload")
    return len(enc.encode(s, disallowed_special=()))


def _content(enc: tiktoken.Encoding, content: Any) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return _text(enc, content)
    if isinstance(content, list):
        total = 0
        for part in content:
            if not isinstance(part, dict) or part.get("type") not in TEXT_PARTS:
                raise CostError("unsupported_payload")  # images, audio, files: not priced here
            total += _text(enc, part.get("text"))
        return total
    raise CostError("unsupported_payload")


def _json(enc: tiktoken.Encoding, value: Any) -> int:
    return _text(enc, json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True))


def estimate_input(body: dict[str, Any], model: str) -> int:
    """Upper-biased token count of everything in a chat-completions body that the model reads."""
    enc = encoding_for(model)
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise CostError("unsupported_payload")
    total = PRIMING
    for m in messages:
        if not isinstance(m, dict):
            raise CostError("unsupported_payload")
        total += PER_MESSAGE + _content(enc, m.get("content"))
        for key in ("role", "name", "tool_call_id"):
            if m.get(key) is not None:
                total += _text(enc, m[key])
        for key in ("tool_calls", "function_call"):
            if m.get(key) is not None:
                total += _json(enc, m[key])
    for key in ("tools", "functions", "tool_choice", "function_call", "response_format"):
        if body.get(key) is not None:
            total += _json(enc, body[key])
    return total


def input_bound(estimate: int, context: int) -> int:
    """The tokens to reserve for input: the estimate plus margin, never above the model's context window.
    Raises when even the bare estimate cannot fit, so an oversized request never reaches the provider."""
    if estimate > context:
        raise CostError("input_exceeds_context")
    margin = -(-estimate * MARGIN_PERCENT // 100)  # ceiling division, exact
    return min(context, estimate + margin + MARGIN_TOKENS)
