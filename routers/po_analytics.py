"""
PO Analytics — analyze received Purchase Orders for FBA profitability.

POST /api/po-analytics/start               — body {po_numbers: "49932" | "49932,49933"}
GET  /api/po-analytics/jobs/{job_id}        — poll status/progress
GET  /api/po-analytics/jobs/{job_id}/download — xlsx once complete
"""
from __future__ import annotations

import logging
import threading
import time
import uuid

from fastapi import APIRouter, Body, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import StreamingResponse

from services.analytics import po_analytics as poa

log = logging.getLogger(__name__)
router = APIRouter()

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL = 3600


def _get_job(job_id: str) -> dict:
    now = time.time()
    with _JOBS_LOCK:
        for jid in [j for j, jb in _JOBS.items()
                   if jb["status"] != "running" and now - jb["started"] > _JOB_TTL]:
            _JOBS.pop(jid, None)
        job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found (or expired).")
    return job


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

    job_id = uuid.uuid4().hex[:12]
    job = {"status": "running", "phase": "pulling SellerCloud PO data", "done": 0,
           "total": 0, "blob": None, "error": None, "filename": None,
           "started": time.time(), "phase_started": time.time()}
    with _JOBS_LOCK:
        _JOBS[job_id] = job

    def _progress(done: int, total: int, phase: str | None = None) -> None:
        if phase and phase != job.get("phase"):
            job["phase"], job["phase_started"] = phase, time.time()
        job["done"], job["total"] = done, total

    def _run() -> None:
        try:
            gathered = poa.gather(po_numbers)
            job["filename"] = poa.po_analytics_filename(po_numbers, gathered["brand_label"])
            job["blob"] = poa.enrich_and_build(gathered, on_progress=_progress)
            job["phase"], job["status"] = "done", "complete"
        except Exception as exc:  # noqa: BLE001
            log.exception("[po-analytics] failed")
            job["status"], job["error"] = "error", str(exc)[:400]

    threading.Thread(target=_run, name=f"po-analytics-{job_id}", daemon=True).start()
    return {"job_id": job_id, "po_numbers": po_numbers}


@router.get("/po-analytics/jobs/{job_id}")
async def po_analytics_status(job_id: str) -> dict:
    job = _get_job(job_id)
    eta = None
    if job["status"] == "running" and job["done"] > 0 and job["total"]:
        el = time.time() - job.get("phase_started", job["started"])
        eta = max(0, round(el / job["done"] * (job["total"] - job["done"])))
    return {"status": job["status"], "phase": job["phase"], "done": job["done"],
            "total": job["total"], "eta_seconds": eta, "error": job["error"],
            "filename": job.get("filename")}


@router.get("/po-analytics/jobs/{job_id}/download")
async def po_analytics_download(job_id: str) -> StreamingResponse:
    job = _get_job(job_id)
    if job["status"] != "complete" or not job["blob"]:
        raise HTTPException(status_code=409, detail=f"Not ready ({job['status']}).")
    return _xlsx_response(job["blob"], job.get("filename") or "po_analytics.xlsx")
