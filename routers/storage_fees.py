"""
FBA Storage Fee estimator endpoints.

POST /api/storage-fees/check          — submit ASINs, start background job
GET  /api/storage-fees/jobs/{job_id}  — poll progress + live results
GET  /api/storage-fees/jobs/{job_id}/export — CSV download
"""
from __future__ import annotations

import csv
import io
import threading
import time
import uuid

import requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from services.spapi import get_catalog_api, sp_api_configured
from services.storage_fees import (
    extract_dimensions, get_size_tier, calc_storage_fee,
    storage_cache_get, storage_cache_put,
)
from services.safety import safe_spreadsheet_value

router = APIRouter()

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL = 3600  # evict completed/error jobs after 1 hour

WORKERS   = 2    # getCatalogItem rate limit: 2 req/s
MAX_ASINS = 5_000


# ── Helpers ──────────────────────────────────────────────────────────────────

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

@router.post("/storage-fees/check")
async def start_check(body: CheckBody) -> dict:
    asins = list(dict.fromkeys(a.strip().upper() for a in body.asins if a.strip()))
    if not asins:
        raise HTTPException(status_code=400, detail="No valid ASINs provided.")
    if len(asins) > MAX_ASINS:
        raise HTTPException(
            status_code=400,
            detail=f"Max {MAX_ASINS} ASINs per request (received {len(asins)}).",
        )
    if not sp_api_configured():
        raise HTTPException(status_code=503, detail="SP-API credentials not configured.")

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

    def run() -> None:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        lock = threading.Lock()

        # Serve cached storage results instantly (60-day TTL); only the ASINs not
        # in the cache hit getCatalogItem. This is what makes a repeat lookup fast.
        cached = storage_cache_get(asins)
        for asin in asins:
            hit = cached.get(asin)
            if hit is not None:
                hit = {**hit, "from_cache": True}
                with lock:
                    job["results"].append(hit)
                    job["done"] += 1
        to_fetch = [a for a in asins if a not in cached]
        if not to_fetch:
            job["status"] = "complete"
            return

        try:
            api = get_catalog_api()
        except Exception as exc:
            job["status"] = "error"
            job["error"]  = str(exc)
            return

        def fetch_one(asin: str) -> dict:
            try:
                raw = api.get_by_asin(asin)
                attrs = (raw.get("attributes") or {})
                summaries = raw.get("summaries") or []
                title = (summaries[0].get("itemName") or "") if summaries else ""

                dims = extract_dimensions(attrs)
                l_cm = dims["length_cm"]
                w_cm = dims["width_cm"]
                h_cm = dims["height_cm"]
                wt_g = dims["weight_g"]

                tier = None
                if all(v is not None for v in [l_cm, w_cm, h_cm, wt_g]):
                    tier = get_size_tier(dims["length_in"], dims["width_in"],
                                        dims["height_in"], dims["weight_lb"])

                fee_off, fee_peak = calc_storage_fee(l_cm, w_cm, h_cm, wt_g)

                return {
                    "asin":       asin,
                    "title":      title,
                    "dog":        False,          # in the catalog = live listing
                    "length_in":  dims["length_in"],
                    "width_in":   dims["width_in"],
                    "height_in":  dims["height_in"],
                    "weight_lb":  dims["weight_lb"],
                    "size_tier":  tier,
                    "fee_offpeak": fee_off,
                    "fee_peak":    fee_peak,
                    "status":     "ok" if tier else "no_dimensions",
                }
            except requests.HTTPError as exc:
                # 404 = not in the Amazon catalog → DOG (dead/delisted). The dims
                # call already tells us this, so DOG is captured for free here — no
                # need for a separate DOG lookup.
                if getattr(exc.response, "status_code", None) == 404:
                    return {
                        "asin": asin, "title": "", "dog": True, "status": "dog",
                        "length_in": None, "width_in": None, "height_in": None,
                        "weight_lb": None, "size_tier": None,
                        "fee_offpeak": None, "fee_peak": None,
                    }
                return {
                    "asin": asin, "title": "", "dog": None, "status": "error",
                    "error": str(exc),
                    "length_in": None, "width_in": None, "height_in": None,
                    "weight_lb": None, "size_tier": None,
                    "fee_offpeak": None, "fee_peak": None,
                }
            except Exception as exc:
                return {
                    "asin":   asin,
                    "title":  "",
                    "dog":    None,
                    "status": "error",
                    "error":  str(exc),
                    "length_in": None, "width_in": None, "height_in": None,
                    "weight_lb": None, "size_tier": None,
                    "fee_offpeak": None, "fee_peak": None,
                }

        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            fresh: list[dict] = []
            with ThreadPoolExecutor(max_workers=WORKERS) as pool:
                futures = {pool.submit(fetch_one, asin): asin for asin in to_fetch}
                for future in as_completed(futures):
                    result = future.result()
                    with lock:
                        job["results"].append(result)
                        job["done"] += 1
                        fresh.append(result)
            storage_cache_put(fresh)     # cache ok/no_dimensions/dog (never errors)
            job["status"] = "complete"
        except Exception as exc:
            job["status"] = "error"
            job["error"]  = str(exc)

    threading.Thread(target=run, daemon=True).start()
    return {"job_id": job_id, "total": len(asins)}


@router.get("/storage-fees/jobs/{job_id}")
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


@router.get("/storage-fees/jobs/{job_id}/export")
async def export_job(job_id: str, mode: str = "offpeak") -> StreamingResponse:
    """
    Two-column CSV: ASIN, fee.
    ?mode=offpeak  (default) → non-peak monthly fee
    ?mode=peak               → Q4 peak monthly fee
    """
    job = _get_job(job_id)
    use_peak = (mode == "peak")
    fee_col  = "fee_peak_q4_per_unit_mo" if use_peak else "fee_offpeak_per_unit_mo"

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["asin", "dog", fee_col])
    for r in job["results"]:
        fee_val = r.get("fee_peak") if use_peak else r.get("fee_offpeak")
        dog = r.get("dog")
        writer.writerow([
            safe_spreadsheet_value(r["asin"]),
            "DOG" if dog is True else ("" if dog is None else "live"),
            "" if fee_val is None else safe_spreadsheet_value(fee_val),
        ])

    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=storage_fees.csv"},
    )
