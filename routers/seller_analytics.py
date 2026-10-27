"""
Seller Analytics — analyze a competitor's (or your own) full Amazon seller
ASIN catalog in the exact Offer Analytics template format.

POST /api/seller-analytics/lookup            — quick seller ASIN-count lookup
POST /api/seller-analytics/start             — start the full export (background job)
GET  /api/seller-analytics/jobs/{job_id}     — poll status/progress
GET  /api/seller-analytics/jobs/{job_id}/download — xlsx once complete
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse

from services import seller_analytics as sa
from services import job_persistence

log = logging.getLogger(__name__)
router = APIRouter()

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL = 3600


def _get_job(job_id: str) -> dict:
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
            "feature": "Seller Analytics",
            "job_id": jid,
            "status": j.get("status"),
            "phase": j.get("phase"),
            "done": j.get("done"),
            "total": j.get("total"),
            "started_at": j.get("started"),
        }
        for jid, j in items
    ]


def _xlsx_response(blob: bytes, filename: str) -> StreamingResponse:
    return StreamingResponse(
        iter([blob]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/seller-analytics/lookup")
async def seller_analytics_lookup(body: dict = Body(...)) -> dict:
    seller_id = str(body.get("seller_id") or "").strip()
    if not seller_id:
        raise HTTPException(status_code=400, detail="A seller ID is required.")
    result = sa.seller_lookup(seller_id)
    if result.get("error"):
        raise HTTPException(status_code=502, detail=result["error"])
    return result


@router.post("/seller-analytics/start")
async def seller_analytics_start(body: dict = Body(...)) -> dict:
    seller_id = str(body.get("seller_id") or "").strip()
    if not seller_id:
        raise HTTPException(status_code=400, detail="A seller ID is required.")
    roi_pct = body.get("roi_pct")
    try:
        roi_pct = float(roi_pct) if roi_pct is not None else sa.DEFAULT_ROI_PCT
    except (TypeError, ValueError):
        roi_pct = sa.DEFAULT_ROI_PCT
    live = bool(body.get("live", True))
    job_id = _launch_seller_analytics(seller_id, roi_pct, live)
    return {"job_id": job_id, "seller_id": seller_id, "roi_pct": roi_pct}


def _launch_seller_analytics(seller_id: str, roi_pct: float, live: bool) -> str:
    """The actual job-launch logic, shared by the live POST endpoint and the
    startup relauncher (job_persistence.resume_orphaned_jobs)."""
    job_id = uuid.uuid4().hex[:12]
    job = {"status": "running", "phase": "looking up seller on Keepa", "done": 0,
           "total": 0, "blob": None, "error": None, "filename": None,
           "completed_at": None, "started": time.time(), "phase_started": time.time()}
    with _JOBS_LOCK:
        _JOBS[job_id] = job
    job_persistence.save_job(job_id, "seller_analytics",
                              {"seller_id": seller_id, "roi_pct": roi_pct, "live": live})

    def _progress(done: int, total: int, phase: str | None = None) -> None:
        if phase and phase != job.get("phase"):
            job["phase"], job["phase_started"] = phase, time.time()
        job["done"], job["total"] = done, total

    def _run() -> None:
        try:
            blob, info = sa.build_seller_template_xlsx(
                seller_id, roi_pct=roi_pct, live=live, on_progress=_progress,
                should_cancel=lambda: job.get("cancel"))
            job["filename"] = sa.seller_filename(seller_id, info.get("seller_name"))
            job["blob"] = blob
            job["phase"], job["status"] = "done", "complete"
        except Exception as exc:  # noqa: BLE001
            log.exception("[seller-analytics] failed")
            job["status"], job["error"] = "error", str(exc)[:400]
        finally:
            job["completed_at"] = time.time()
            job_persistence.finish_job(job_id)

    threading.Thread(target=_run, name=f"seller-analytics-{job_id}", daemon=True).start()
    return job_id


def _relaunch(params: dict) -> None:
    seller_id = str(params.get("seller_id") or "").strip()
    if not seller_id:
        return
    roi_pct = params.get("roi_pct") or sa.DEFAULT_ROI_PCT
    live = bool(params.get("live", True))
    _launch_seller_analytics(seller_id, roi_pct, live)


job_persistence.register_relauncher("seller_analytics", _relaunch)


@router.get("/seller-analytics/jobs/{job_id}")
async def seller_analytics_status(job_id: str) -> dict:
    job = _get_job(job_id)
    eta = None
    if job["status"] == "running" and job["done"] > 0 and job["total"]:
        elapsed = time.time() - job["phase_started"]
        eta = max(0, round(elapsed / job["done"] * (job["total"] - job["done"])))
    return {
        "job_id": job_id, "status": job["status"], "phase": job["phase"],
        "done": job["done"], "total": job["total"], "error": job.get("error"),
        "filename": job.get("filename"), "eta_seconds": eta,
    }


@router.get("/seller-analytics/jobs/{job_id}/download")
async def seller_analytics_download(job_id: str) -> StreamingResponse:
    job = _get_job(job_id)
    if job["status"] != "complete" or not job["blob"]:
        raise HTTPException(status_code=409, detail=f"Job is {job['status']!r}, not ready.")
    return _xlsx_response(job["blob"], job["filename"] or "seller_analytics.xlsx")
