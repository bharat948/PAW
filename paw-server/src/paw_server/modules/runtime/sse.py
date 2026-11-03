"""Server-sent events reader. Hermès sends unnamed frames: `data: {"event": "...", "seq": n, ...}`."""

import json
from collections.abc import Iterable, Iterator
from typing import Any


def parse_frames(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """Yields each data frame that is a JSON object. Comments (`: open`, keepalives) and junk are skipped."""
    data: list[str] = []
    for line in lines:
        if line == "":
            if data:
                try:
                    obj = json.loads("\n".join(data))
                except ValueError:
                    obj = None
                if isinstance(obj, dict):
                    yield obj
            data = []
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
