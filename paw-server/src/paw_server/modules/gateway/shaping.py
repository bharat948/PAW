"""Tool results as the model receives them: bounded, and marked as data. (Full rules land in 002D PR 5.)"""

import json
from typing import Any

MAX_BYTES = 16 * 1024


def shape(result: dict[str, Any]) -> tuple[str, bool]:
    """(text, truncated)."""
    text = json.dumps(result, ensure_ascii=False, default=str)
    raw = text.encode()
    if len(raw) <= MAX_BYTES:
        return text, False
    return raw[:MAX_BYTES].decode(errors="ignore"), True
