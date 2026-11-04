"""
One persistent requests.Session per thread for SP-API calls.

Plain requests.get() opens a new HTTPS connection every call, and on this
Windows host building that connection's SSL context (loading the CA bundle)
costs ~0.7 CPU-seconds -- measured 2026-10-28: 10 getListingsRestrictions
calls took 12.7s wall / 6.8s CPU, 6.9s of it in load_verify_locations. A
session keeps the connection alive, so that cost is paid once per thread.
Per-thread rather than shared because requests.Session isn't documented as
thread-safe and the export pipelines call SP-API from worker pools.
"""
from __future__ import annotations

import threading

import requests

_local = threading.local()


def thread_session() -> requests.Session:
    s = getattr(_local, "session", None)
    if s is None:
        s = _local.session = requests.Session()
    return s
