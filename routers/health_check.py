"""
Weekly catalog health check (Eligibility + DOG) — endpoints.

GET  /api/health-check/status               — scheduler state + next run + latest summary
POST /api/health-check/run                  — trigger a run now (background thread)
GET  /api/health-check/runs                 — recent run summaries
GET  /api/health-check/runs/{run_id}        — one run's per-ASIN results
GET  /api/health-check/runs/{run_id}/export — CSV of a run's results
"""
from __future__ import annotations

import csv
import io
import threading

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from services import database, health_check as hc
from services.safety import safe_spreadsheet_value
from services.spapi.config import sp_api_configured

router = APIRouter()


def _latest_summary() -> dict | None:
    with database._connect() as conn:
        try:
            row = conn.execute(
                "SELECT run_id,started_at,finished_at,trigger,total,dog,can_sell,"
                "needs_approval,restricted,errors FROM health_check_runs "
                "ORDER BY finished_at DESC LIMIT 1").fetchone()
        except Exception:
            return None
    if not row:
        return None
    keys = ("run_id", "started_at", "finished_at", "trigger", "total", "dog",
            "can_sell", "needs_approval", "restricted", "errors")
    return dict(zip(keys, row))


@router.get("/health-check/status")
async def status() -> dict:
    hc.init_schema()
    return {"configured": sp_api_configured(),
            "scheduler": hc.scheduler_status(),
            "latest": _latest_summary()}


@router.post("/health-check/run")
async def run_now() -> dict:
    if not sp_api_configured():
        raise HTTPException(status_code=400, detail="SP-API is not configured.")
    if hc._running.is_set():
        raise HTTPException(status_code=409, detail="A health check is already running.")
    # Run in the background so the request returns immediately (a full Pair-Library
    # sweep is a few minutes). Poll /health-check/status for completion.
    threading.Thread(target=hc.run_health_check, kwargs={"trigger": "manual"},
                     name="health-check-manual", daemon=True).start()
    return {"started": True, "next_scheduled": hc.next_run_time().isoformat()}


@router.get("/health-check/runs")
async def runs(limit: int = 20) -> dict:
    hc.init_schema()
    with database._connect() as conn:
        rows = conn.execute(
            "SELECT run_id,started_at,finished_at,trigger,total,dog,can_sell,"
            "needs_approval,restricted,errors FROM health_check_runs "
            "ORDER BY finished_at DESC LIMIT ?", (max(1, min(limit, 200)),)).fetchall()
    keys = ("run_id", "started_at", "finished_at", "trigger", "total", "dog",
            "can_sell", "needs_approval", "restricted", "errors")
    return {"runs": [dict(zip(keys, r)) for r in rows]}


@router.get("/health-check/runs/{run_id}")
async def run_detail(run_id: str) -> dict:
    with database._connect() as conn:
        rows = conn.execute(
            "SELECT asin,dog,eligibility,reasons,brand,manufacturer,storage_fee,"
            "storage_fee_peak,detail FROM health_check_results WHERE run_id=? "
            "ORDER BY dog DESC, eligibility", (run_id,)).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail="Run not found (or no results).")
    keys = ("asin", "dog", "eligibility", "reasons", "brand", "manufacturer",
            "storage_fee", "storage_fee_peak", "detail")
    return {"run_id": run_id, "results": [dict(zip(keys, r)) for r in rows]}


@router.get("/health-check/runs/{run_id}/export")
async def export_run(run_id: str) -> StreamingResponse:
    with database._connect() as conn:
        rows = conn.execute(
            "SELECT asin,dog,eligibility,reasons,brand,manufacturer,storage_fee,"
            "storage_fee_peak,detail FROM health_check_results WHERE run_id=? "
            "ORDER BY dog DESC, eligibility", (run_id,)).fetchall()
    if not rows:
        raise HTTPException(status_code=404, detail="Run not found (or no results).")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["asin", "dog", "eligibility", "reasons", "brand", "manufacturer",
                "storage_fee_offpeak", "storage_fee_peak", "detail"])
    for asin, dog, elig, reasons, brand, manu, fee, feep, detail in rows:
        w.writerow([safe_spreadsheet_value(v) for v in (
            asin, "DOG" if dog == 1 else ("live" if dog == 0 else ""),
            elig, reasons, brand, manu, fee, feep, detail)])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]), media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="health_check_{run_id}.csv"'})
