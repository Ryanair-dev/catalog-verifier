"""
Shared "survive a server restart" backing store for the in-memory background
jobs scattered across routers/vendor_offers.py (Offer Analytics export),
routers/po_analytics.py (PO Analytics export), routers/eligibility.py,
routers/storage_fees.py, and routers/generic_check.py (the three Toolbox ASIN
checks).

Every one of those jobs lives in a plain Python dict + a daemon thread --
exactly what you want for a snappy single-process app, except the dict (and
whatever progress/blob it holds) is gone the instant the process restarts.
Restarting happens constantly during active development, so a long-running
export or a big ASIN batch check gets silently thrown away every time a code
change is deployed.

Design (deliberately NOT a rewrite of those job dicts): each router keeps
doing exactly what it already does for day-to-day polling/download. This
module is a thin shadow table -- every job mirrors {job_type, params} here
the moment it starts, and the row is deleted the moment the job leaves
"running" (done/error/cancelled). On every app startup, resume_orphaned_jobs()
looks for any row STILL marked "running" (meaning the process died -- or was
restarted -- while that job was in flight) and relaunches it fresh (new job
id, same params) via a per-job-type callback each router registers at import
time.

A relaunch always starts the job over from the beginning -- none of these
five pipelines support resuming mid-batch -- so the guarantee this provides
is "never silently lost", not "picks up exactly where it left off".
"""
from __future__ import annotations

import json
import logging
import time
from typing import Callable

from services.database import _LOCK, _connect

log = logging.getLogger(__name__)

# job_type -> fn(params: dict) -> None   (starts a brand-new job with these
# params, same as a fresh POST to that job's /start or /check endpoint).
_RELAUNCHERS: dict[str, Callable[[dict], None]] = {}

_initialized = False


def register_relauncher(job_type: str, fn: Callable[[dict], None]) -> None:
    """Call once at router import time."""
    _RELAUNCHERS[job_type] = fn


def init() -> None:
    global _initialized
    if _initialized:
        return
    with _LOCK, _connect() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS background_jobs (
            job_id       TEXT PRIMARY KEY,
            job_type     TEXT NOT NULL,
            params_json  TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'running',
            created_at   REAL,
            updated_at   REAL
        )""")
    _initialized = True


def save_job(job_id: str, job_type: str, params: dict) -> None:
    """Call right after a job's in-memory dict is created, before the worker
    thread starts. Best-effort -- a failure here never blocks the real job."""
    init()
    now = time.time()
    try:
        with _LOCK, _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO background_jobs "
                "(job_id, job_type, params_json, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'running', ?, ?)",
                (job_id, job_type, json.dumps(params), now, now),
            )
    except Exception:
        log.exception("[job_persistence] save_job failed for %s", job_id)


def finish_job(job_id: str) -> None:
    """Call once a job leaves 'running' (complete/error/cancelled) so a future
    restart never mistakes it for an orphan."""
    try:
        with _LOCK, _connect() as conn:
            conn.execute("DELETE FROM background_jobs WHERE job_id=?", (job_id,))
    except Exception:
        log.exception("[job_persistence] finish_job failed for %s", job_id)


def resume_orphaned_jobs() -> dict:
    """Call once at app startup, after every router module has been imported
    (so every relauncher is registered). Any row still 'running' means the
    previous process died (or was restarted) mid-job -- a clean finish always
    deletes its own row first. Relaunches each one fresh, then clears the
    whole table (a relaunch failure is logged + skipped, not retried forever)."""
    init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT job_id, job_type, params_json FROM background_jobs WHERE status='running'"
        ).fetchall()
    resumed: list[dict] = []
    skipped: list[dict] = []
    for r in rows:
        job_type = r["job_type"]
        try:
            params = json.loads(r["params_json"] or "{}")
        except (TypeError, ValueError):
            params = {}
        fn = _RELAUNCHERS.get(job_type)
        if not fn:
            skipped.append({"job_type": job_type, "reason": "no relauncher registered"})
            continue
        try:
            fn(params)
            resumed.append({"job_type": job_type, "params": params})
        except Exception as exc:  # noqa: BLE001
            log.exception("[job_persistence] relaunch failed for job_type=%s", job_type)
            skipped.append({"job_type": job_type, "reason": str(exc)[:200]})
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM background_jobs WHERE status='running'")
    if resumed or skipped:
        log.info("[job_persistence] startup resume: %d resumed, %d skipped (%s)",
                  len(resumed), len(skipped), skipped)
    return {"resumed": resumed, "skipped": skipped}
