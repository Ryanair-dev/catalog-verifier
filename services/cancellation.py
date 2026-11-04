"""
Shared "is this batch job being cancelled" primitives.

Used by every outbound API client function that can block for a while
(rate-limit waits, 429/5xx backoff) so a long retry/backoff doesn't hold a
caller's cancel button hostage -- see services/spapi/catalog.py,
services/restrictions.py, and services/keepa.py's own usage.

Added 2026-10-20 after a real user report: "when i click on cancel in the
vendor offer analytics it doesnt stop." The cancel flag itself WAS being
checked between work items the whole time -- the actual bug was that a
worker thread stuck inside a retry-backoff sleep (most likely during a
getCatalogItem 429 storm, which this app has hit repeatedly) had no way to
notice a cancel request mid-sleep, so it kept retrying for its full
multi-minute sequence regardless of what the user clicked.
"""
from __future__ import annotations

import time
from typing import Callable


class JobCancelled(Exception):
    """Raised by a lower-level API client when a `should_cancel` callback
    reports the caller no longer wants this call, so a multi-attempt
    retry/backoff bails out immediately instead of running to completion
    first. Callers that pass `should_cancel` should catch this and treat it
    exactly like their own cancellation signal -- NOT a per-item failure."""


def cancellable_sleep(seconds: float, should_cancel: Callable[[], bool] | None) -> None:
    """time.sleep(seconds), but wakes up early (in <=0.5s increments) and
    raises JobCancelled the moment should_cancel() reports true, instead of
    blocking for the whole wait window."""
    if not should_cancel:
        time.sleep(seconds)
        return
    deadline = time.monotonic() + seconds
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if should_cancel():
            raise JobCancelled()
        time.sleep(min(0.5, remaining))
