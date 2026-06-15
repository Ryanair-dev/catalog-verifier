"""
Listing Eligibility Checker endpoints.

POST /api/eligibility/check              — submit ASINs, start 5-worker background job
GET  /api/eligibility/jobs/{job_id}      — poll progress + live results
GET  /api/eligibility/jobs/{job_id}/export — CSV download (available at any point)
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

from services.restrictions import classify, get_restrictions_api
from services.spapi.config import load_sp_api_credentials, sp_api_configured
from services.safety import safe_spreadsheet_value

router = APIRouter()

# ── In-memory job store ───────────────────────────────────────────────────────
_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL = 3600  # evict completed/error jobs after 1 hour

WORKERS = 5
MAX_ASINS = 5_000


# ── Helpers ───────────────────────────────────────────────────────────────────

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


# ── Schema ────────────────────────────────────────────────────────────────────

class CheckBody(BaseModel):
    asins: list[str]


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/eligibility/status")
async def eligibility_status() -> dict:
    """Probe whether SP-API + seller ID are configured."""
    configured = sp_api_configured()
    seller_ok = False
    if configured:
        try:
            creds = load_sp_api_credentials()
            seller_ok = bool(creds.seller_id)
        except RuntimeError:
            pass
    return {"sp_api_configured": configured, "seller_id_configured": seller_ok}


@router.post("/eligibility/check")
async def start_check(body: CheckBody) -> dict:
    # Clean + deduplicate
    asins = list(dict.fromkeys(a.strip().upper() for a in body.asins if a.strip()))
    if not asins:
        raise HTTPException(status_code=400, detail="No valid ASINs provided.")
    if len(asins) > MAX_ASINS:
        raise HTTPException(
            status_code=400,
            detail=f"Max {MAX_ASINS} ASINs per request (received {len(asins)}).",
        )

    # Validate credentials early for a clean error message
    try:
        creds = load_sp_api_credentials()
    except RuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc))

    if not creds.seller_id:
        raise HTTPException(
            status_code=503,
            detail=(
                "AMZ_SELLER_ID is not set. "
                "Add it to your .env file and restart — "
                "the Listings Restrictions API requires a seller ID."
            ),
        )

    job_id = str(uuid.uuid4())
    job: dict = {
        "job_id":     job_id,
        "total":      len(asins),
        "done":       0,
        "status":     "running",
        "results":    [],
        "started_at": time.time(),
    }
    with _JOBS_LOCK:
        _JOBS[job_id] = job

    # ── Background worker ──────────────────────────────────────────────────
    def run() -> None:
        api       = get_restrictions_api()
        seller_id = creds.seller_id
        lock      = threading.Lock()

        def check_one(asin: str) -> dict:
            try:
                raw = api.get_restrictions(asin, seller_id)
                return classify(asin, raw)
            except PermissionError as exc:
                return {
                    "asin": asin, "status": "ERROR",
                    "reasons": [{"type": "PERMISSION_ERROR", "message": str(exc),
                                 "hint": "", "can_request": False, "approval_url": None}],
                }
            except Exception as exc:  # noqa: BLE001
                return {
                    "asin": asin, "status": "ERROR",
                    "reasons": [{"type": "API_ERROR", "message": str(exc),
                                 "hint": "", "can_request": False, "approval_url": None}],
                }

        try:
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                futures = {pool.submit(check_one, asin): asin for asin in asins}
                for future in as_completed(futures):
                    result = future.result()
                    with lock:
                        job["results"].append(result)
                        job["done"] += 1
            job["status"] = "complete"
        except Exception as exc:  # noqa: BLE001
            job["status"] = "error"
            job["error"]  = str(exc)

    threading.Thread(target=run, daemon=True).start()

    return {"job_id": job_id, "total": len(asins)}


@router.get("/eligibility/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = _get_job(job_id)
    return {
        "job_id":      job_id,
        "total":       job["total"],
        "done":        job["done"],
        "status":      job["status"],
        "eta_seconds": _eta(job),
        "results":     job["results"],
    }


@router.get("/eligibility/jobs/{job_id}/export")
async def export_job(job_id: str) -> StreamingResponse:
    """Two-column CSV: ASIN, status."""
    job = _get_job(job_id)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["asin", "status"])
    for r in job["results"]:
        writer.writerow([
            safe_spreadsheet_value(r["asin"]),
            safe_spreadsheet_value(r["status"]),
        ])

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=eligibility_results.csv"},
    )
