import threading
import time
from collections import defaultdict, deque

from paw_server.core.errors import ApiError


class SlidingWindowLimiter:
    """In-process limiter (single API replica). Move to a shared store before scaling API out."""

    def __init__(self, limit: int, window_s: float = 60.0) -> None:
        self.limit, self.window = limit, window_s
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, key: str) -> None:
        now = time.monotonic()
        with self._lock:
            q = self._hits[key]
            while q and now - q[0] > self.window:
                q.popleft()
            if len(q) >= self.limit:
                retry = max(1, int(self.window - (now - q[0])) + 1)
                raise ApiError(429, headers={"Retry-After": str(retry)})
            q.append(now)
