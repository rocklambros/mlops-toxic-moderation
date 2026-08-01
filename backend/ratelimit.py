"""In-process token bucket.

Deliberately not Redis and not a database table. The control protects the request path, so
adding a network dependency to it would make the endpoint fail exactly when the thing it
defends against is happening. One backend instance serves /predict (delivery spec section 4),
so per-process state is per-service state.

The clock is injected so the tests are deterministic rather than slept-through.
"""

import threading
import time
from dataclasses import dataclass

MAX_TRACKED_KEYS = 10_000


@dataclass
class _Bucket:
    tokens: float
    updated: float


class RateLimiter:
    def __init__(self, per_minute: int, burst: int, clock=time.monotonic) -> None:
        if per_minute <= 0 or burst <= 0:
            raise ValueError("per_minute and burst must both be positive")
        self.rate = per_minute / 60.0
        self.burst = float(burst)
        self._clock = clock
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()

    def allow(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            if len(self._buckets) >= MAX_TRACKED_KEYS and key not in self._buckets:
                self._evict()
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=self.burst, updated=now)
                self._buckets[key] = bucket
            bucket.tokens = min(self.burst, bucket.tokens + (now - bucket.updated) * self.rate)
            bucket.updated = now
            if bucket.tokens < 1.0:
                return False
            bucket.tokens -= 1.0
            return True

    def _evict(self) -> None:
        """Drop the least recently seen half. Full buckets are indistinguishable from absent
        ones, so evicting a full bucket grants nothing an attacker did not already have."""
        ordered = sorted(self._buckets.items(), key=lambda item: item[1].updated)
        for key, _ in ordered[: len(ordered) // 2]:
            del self._buckets[key]
