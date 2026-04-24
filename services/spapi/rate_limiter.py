"""
Token-bucket rate limiter, mirroring Amazon's own algorithm.

Ported verbatim from asin-scraper/scripts/rate_limiter.py. The limiters
are module-level singletons on purpose — they enforce the per-operation
throttle across the whole process (across threads / async workers / etc).

Rates are taken straight from Amazon's SP-API reference:
  - searchCatalogItems:      2 req/sec, burst 2
  - getCatalogItem:          2 req/sec, burst 2
  - getListingsRestrictions: 5 req/sec, burst 10
"""
from __future__ import annotations

import threading
import time


class TokenBucketLimiter:
    """
    rate  = tokens added per second (sustained limit)
    burst = max tokens the bucket can hold
    """

    def __init__(self, rate: float, burst: int):
        self.rate = rate
        self.burst = burst
        self._tokens = float(burst)
        self._last_refill = time.monotonic()
        self._lock = threading.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_refill
        self._tokens = min(self.burst, self._tokens + elapsed * self.rate)
        self._last_refill = now

    def acquire(self) -> None:
        """Block until a token is available, then consume it."""
        with self._lock:
            self._refill()
            if self._tokens >= 1:
                self._tokens -= 1
                return
            wait = (1 - self._tokens) / self.rate
        time.sleep(wait)
        self.acquire()  # retry after sleep


LIMITERS: dict[str, TokenBucketLimiter] = {
    "searchCatalogItems":      TokenBucketLimiter(rate=2, burst=2),
    "getCatalogItem":          TokenBucketLimiter(rate=2, burst=2),
    "getListingsRestrictions": TokenBucketLimiter(rate=5, burst=10),
}
