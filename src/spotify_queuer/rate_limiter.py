"""Self-throttle rate limiter, per Build Spec §5.

A rolling request budget: max_requests per window_seconds. This is the only
rate-limit setting exposed in the GUI, and is separate from (and does not
replace) 429/Retry-After handling, which is handled by spotify_client.py.
"""
from __future__ import annotations

import threading
import time
from collections import deque


class RateLimiter:
    def __init__(self, max_requests: int, window_seconds: float, sleep_fn=time.sleep, time_fn=time.monotonic):
        self.max_requests = max_requests
        self.window_seconds = window_seconds
        self._sleep = sleep_fn
        self._time = time_fn
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a request is permitted under the rolling window budget, then record it."""
        while True:
            with self._lock:
                now = self._time()
                cutoff = now - self.window_seconds
                while self._timestamps and self._timestamps[0] <= cutoff:
                    self._timestamps.popleft()
                if len(self._timestamps) < self.max_requests:
                    self._timestamps.append(now)
                    return
                wait_for = self._timestamps[0] + self.window_seconds - now
            if wait_for > 0:
                self._sleep(wait_for)
