"""
PO Analytics — analyze received Purchase Orders for FBA profitability.

POST /api/po-analytics/start               — body {po_numbers: "49932" | "49932,49933"}
GET  /api/po-analytics/jobs/{job_id}        — poll status/progress
GET  /api/po-analytics/jobs/{job_id}/download — xlsx once complete
"""
from __future__ import annotations

import logging
import re
import threading
import time
import uuid

from fastapi import APIRouter, Body, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.analytics import po_analytics as poa
from services import job_persistence

log = logging.getLogger(__name__)
router = APIRouter()

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL = 3600


def _get_job(job_id: str) -> dict:
    # Same fix as routers/vendor_offers.py's _get_ajob: TTL is measured from
    # "completed_at", not "started" -- a job that took longer than _JOB_TTL
    # to run would otherwise be evicted (blob and all) the instant it
    # finished, before any poller could ever observe it as complete.
    now = time.time()
    with _JOBS_LOCK:
        for jid in [j for j, jb in _JOBS.items()
                   if jb["status"] != "running" and jb.get("completed_at")
                   and now - jb["completed_at"] > _JOB_TTL]:
            _JOBS.pop(jid, None)
        job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found (or expired).")
    return job


def list_jobs_for_tasks_panel() -> list[dict]:
    """Normalized snapshot for the Settings > Tasks Running tab -- see
    routers/settings.py tasks_running()."""
    with _JOBS_LOCK:
        items = list(_JOBS.items())
    return [
        {
            "feature": "PO Analytics Export",
            "job_id": jid,
            "status": j.get("status"),
            "phase": j.get("phase"),
            "done": j.get("done"),
            "total": j.get("total"),
            "started_at": j.get("started"),
            "completed_at": j.get("completed_at"),
        }
        for jid, j in items
    ]


def _xlsx_response(blob: bytes, filename: str) -> StreamingResponse:
    return StreamingResponse(
        iter([blob]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/po-analytics/start")
async def po_analytics_start(body: dict = Body(...)) -> dict:
    raw = str(body.get("po_numbers") or "").strip()
    po_numbers = poa._parse_po_numbers(raw)
    if not po_numbers:
        raise HTTPException(status_code=400, detail="No PO number(s) found in input.")

    job_id = _launch_po_analytics(po_numbers)
    return {"job_id": job_id, "po_numbers": po_numbers}


def _launch_po_analytics(po_numbers: list) -> str:
    """The actual job-launch logic, shared by the live POST endpoint and the
    startup relauncher (job_persistence.resume_orphaned_jobs)."""
    job_id = uuid.uuid4().hex[:12]
    job = {"status": "running", "phase": "pulling SellerCloud PO data", "done": 0,
           "total": 0, "blob": None, "error": None, "filename": None,
           "skipped_pos": [], "used_ordered_qty": False, "completed_at": None,
           "started": time.time(), "phase_started": time.time()}
    with _JOBS_LOCK:
        _JOBS[job_id] = job
    job_persistence.save_job(job_id, "po_export", {"po_numbers": po_numbers})

    def _progress(done: int, total: int, phase: str | None = None) -> None:
        if phase and phase != job.get("phase"):
            job["phase"], job["phase_started"] = phase, time.time()
        job["done"], job["total"] = done, total

    def _run() -> None:
        try:
            try:
                gathered = poa.gather(po_numbers)
            except RuntimeError as exc:
                # Nothing in the requested PO(s) (or their splits) has
                # actually been received yet -- per the user, analyze it
                # right away anyway rather than blocking until it arrives.
                # Falls back to ordered-quantity mode -- the exact same data
                # source the day-before-ETA automation already uses safely
                # for not-yet-received POs (see po_analytics.gather's own
                # docstring) -- instead of surfacing this as an error. Keyed
                # off "are Received" specifically, since that substring only
                # ever appears in THIS error message (a genuinely Cancelled
                # PO with no usable split raises a different message that
                # must still propagate as a real error).
                if "are Received" not in str(exc):
                    raise
                job["phase"] = "pulling SellerCloud PO data (not yet received — using ordered qty)"
                job["used_ordered_qty"] = True
                gathered = poa.gather(po_numbers, require_received=False)
            job["skipped_pos"] = gathered.get("skipped_pos") or []
            # Name the file after the PO(s) actually analyzed, not what was typed
            # in -- a cancelled PO auto-resolves to its real split (see gather()),
            # and the export should be named after the split, not the cancelled
            # original.
            filename_pos = gathered.get("resolved_po_numbers") or po_numbers
            job["filename"] = poa.po_analytics_filename(filename_pos, gathered["brand_label"])
            job["blob"] = poa.enrich_and_build(gathered, on_progress=_progress)
            job["phase"], job["status"] = "done", "complete"
        except Exception as exc:  # noqa: BLE001
            log.exception("[po-analytics] failed")
            job["status"], job["error"] = "error", str(exc)[:400]
        finally:
            job["completed_at"] = time.time()
            job_persistence.finish_job(job_id)

    threading.Thread(target=_run, name=f"po-analytics-{job_id}", daemon=True).start()
    return job_id


def _relaunch(params: dict) -> None:
    po_numbers = params.get("po_numbers") or []
    if not po_numbers:
        return
    _launch_po_analytics(po_numbers)


job_persistence.register_relauncher("po_export", _relaunch)


@router.get("/po-analytics/jobs/{job_id}")
async def po_analytics_status(job_id: str) -> dict:
    job = _get_job(job_id)
    eta = None
    if job["status"] == "running" and job["done"] > 0 and job["total"]:
        el = time.time() - job.get("phase_started", job["started"])
        eta = max(0, round(el / job["done"] * (job["total"] - job["done"])))
    return {"status": job["status"], "phase": job["phase"], "done": job["done"],
            "total": job["total"], "eta_seconds": eta, "error": job["error"],
            "filename": job.get("filename"), "skipped_pos": job.get("skipped_pos") or [],
            "used_ordered_qty": job.get("used_ordered_qty", False)}


@router.get("/po-analytics/jobs/{job_id}/download")
async def po_analytics_download(job_id: str) -> StreamingResponse:
    job = _get_job(job_id)
    if job["status"] != "complete" or not job["blob"]:
        raise HTTPException(status_code=409, detail=f"Not ready ({job['status']}).")
    return _xlsx_response(job["blob"], job.get("filename") or "po_analytics.xlsx")


# --------------------------------------------------------------------------- #
# Day-before-ETA automation (2026-10-09) — driven by Azure's own Monday.com
# mirror (services.monday_eta), zero Monday API calls. Recipient on/off config
# lives in routers/settings.py (GET/POST /api/settings/automation/po_analytics)
# since it's shared, generic storage; these two endpoints are specific to this
# automation's own scheduler/trigger.
# --------------------------------------------------------------------------- #

@router.get("/po-analytics/automation/status")
async def po_analytics_automation_status() -> dict:
    from services import po_automation
    return po_automation.scheduler_status()


@router.post("/po-analytics/automation/check-now")
async def po_analytics_automation_check_now() -> dict:
    """Run one check cycle immediately instead of waiting for the next daily
    12pm tick — e.g. to verify a just-added recipient/watched PO actually
    works."""
    from services import po_automation
    return await run_in_threadpool(po_automation.check_and_trigger)


@router.get("/po-analytics/automation/watchlist")
async def po_analytics_watchlist() -> dict:
    """The user-curated list of PO numbers this automation actually watches —
    the Inbound Shipments board tracks the whole company's POs, not just
    ecommerce, so only POs explicitly saved here are ever checked/analyzed."""
    from services import po_automation
    return {"watchlist": po_automation.get_watchlist()}


class WatchlistAdd(BaseModel):
    po_numbers: str   # free text: "49932, 49933" / "49932 49933" / "49932"
    recipients: str = ""   # optional per-PO override — one email per line (or comma/semicolon-separated)


def _parse_recipient_emails(raw: str) -> list[str]:
    """Same loose splitting + validation as the generic automation-settings
    recipients field (routers/settings.py) — one email per line, tolerant of
    stray commas/semicolons from a pasted list."""
    parts = re.split(r"[\n,;]+", raw or "")
    cleaned = [p.strip() for p in parts if p.strip()]
    for e in cleaned:
        if "@" not in e or " " in e:
            raise HTTPException(status_code=400, detail=f"'{e}' doesn't look like a valid email address.")
    return cleaned


@router.post("/po-analytics/automation/watchlist")
async def po_analytics_watchlist_add(body: WatchlistAdd) -> dict:
    from services import po_automation
    nums = poa._parse_po_numbers(body.po_numbers)
    if not nums:
        raise HTTPException(status_code=400, detail="No PO number(s) found in input.")
    recipients = _parse_recipient_emails(body.recipients) or None
    added = po_automation.add_to_watchlist(nums, recipients=recipients)
    # Pull this PO's current ETA/status/vendor/goods/amount right away instead
    # of leaving it blank until the next scheduled (at most once-a-day) check
    # or a manual "Check now" -- see refresh_tracking_for()'s own docstring.
    await run_in_threadpool(po_automation.refresh_tracking_for, nums)
    return {"added": added, "requested": len(nums), "watchlist": po_automation.get_watchlist()}


@router.delete("/po-analytics/automation/watchlist/{po_number}")
async def po_analytics_watchlist_remove(po_number: int) -> dict:
    from services import po_automation
    removed = po_automation.remove_from_watchlist(po_number)
    if not removed:
        raise HTTPException(status_code=404, detail=f"PO {po_number} isn't on the watchlist.")
    return {"removed": po_number, "watchlist": po_automation.get_watchlist()}


@router.get("/po-analytics/automation/archive/{po_number}/download")
async def po_analytics_archive_download(po_number: int) -> StreamingResponse:
    """The already-generated report for a watched PO that's been analyzed —
    saved at send time so it can be re-downloaded later without re-running
    the whole pipeline."""
    from services import po_automation
    result = po_automation.get_report(po_number)
    if not result:
        raise HTTPException(status_code=404, detail=f"No archived report for PO {po_number} yet.")
    filename, blob = result
    return _xlsx_response(blob, filename)
