"""
Generic-ASIN / DOG checker endpoints (Tools panel) — mirrors eligibility.py.

POST /api/generic-check/check              — submit ASINs, start 5-worker background job
GET  /api/generic-check/jobs/{job_id}      — poll progress + live results
GET  /api/generic-check/jobs/{job_id}/export — CSV download (available at any point)

Classifies each ASIN as DOG (404) / GENERIC (SB85 issue 5885 via a non-persisting
VALIDATION_PREVIEW) / NOT_GENERIC. No listing is ever created.
"""
from __future__ import annotations

import csv
import io
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.generic_check import classify_generic
from services.spapi.config import load_sp_api_credentials, sp_api_configured
from services.safety import safe_spreadsheet_value

router = APIRouter()

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL = 3600

WORKERS = 5            # each ASIN = 1 catalog GET + 1 listings VALIDATION_PREVIEW PUT
MAX_ASINS = 5_000


def _get_job(job_id: str) -> dict:
    now = time.time()
    with _JOBS_LOCK:
        stale = [
            jid for jid, j in _JOBS.items()
            if j.get("status") != "running" and now - j.get("started_at", now) > _JOB_TTL
        ]
        for jid in stale:
            _JOBS.pop(jid, None)
    job = _JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


def _eta(job: dict) -> int | None:
    done = job["done"]
    if done == 0 or job["status"] != "running":
        return None
    elapsed = time.time() - job["started_at"]
    return max(0, round(elapsed / done * (job["total"] - done)))


class CheckBody(BaseModel):
    asins: list[str]


@router.get("/generic-check/status")
async def generic_status() -> dict:
    """Probe whether SP-API + seller ID are configured (Listings role also required)."""
    configured = sp_api_configured()
    seller_ok = False
    if configured:
        try:
            seller_ok = bool(load_sp_api_credentials().seller_id)
        except RuntimeError:
            pass
    return {"sp_api_configured": configured, "seller_id_configured": seller_ok}


@router.post("/generic-check/check")
async def start_check(body: CheckBody) -> dict:
    asins = list(dict.fromkeys(a.strip().upper() for a in body.asins if a.strip()))
    if not asins:
        raise HTTPException(status_code=400, detail="No valid ASINs provided.")
    if len(asins) > MAX_ASINS:
        raise HTTPException(status_code=400,
                            detail=f"Max {MAX_ASINS} ASINs per request (received {len(asins)}).")
    try:
        creds = load_sp_api_credentials()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    if not creds.seller_id:
        raise HTTPException(status_code=503, detail=(
            "AMZ_SELLER_ID is not set. Add it to your .env and restart — the generic "
            "check submits a validation-preview listing which requires a seller ID."
        ))

    job_id = str(uuid.uuid4())
    job: dict = {
        "job_id": job_id, "total": len(asins), "done": 0,
        "status": "running", "results": [], "started_at": time.time(),
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job

    def run() -> None:
        lock = threading.Lock()
        try:
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                futures = {pool.submit(classify_generic, a): a for a in asins}
                for future in as_completed(futures):
                    result = future.result()
                    with lock:
                        job["results"].append(result)
                        job["done"] += 1
            job["status"] = "complete"
        except Exception as exc:  # noqa: BLE001
            job["status"] = "error"
            job["error"] = str(exc)

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job_id, "total": len(asins)}


@router.get("/generic-check/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = _get_job(job_id)
    return {
        "job_id": job_id, "total": job["total"], "done": job["done"],
        "status": job["status"], "eta_seconds": _eta(job), "results": job["results"],
    }


@router.get("/generic-check/jobs/{job_id}/export")
async def export_job(job_id: str) -> StreamingResponse:
    """Three-column CSV: ASIN, generic_status, detail."""
    job = _get_job(job_id)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["asin", "generic_status", "detail"])
    for r in job["results"]:
        writer.writerow([
            safe_spreadsheet_value(r["asin"]),
            safe_spreadsheet_value(r["status"]),
            safe_spreadsheet_value(r.get("detail", "")),
        ])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=generic_check_results.csv"},
    )
