"""
Vendor Offer Analytics (Price Desk) endpoints — Phase 1 / MVP.

GET  /api/vendor-offers/data        — the price-comparison ledger payload
POST /api/vendor-offers/upload      — upload a vendor catalog file → merge (§5B)
POST /api/vendor-offers/sync-asins  — pull UPC→ASIN from the existing Pair Library
POST /api/vendor-offers/assign      — hand-assign one ASIN to a UPC
GET  /api/vendor-offers/status      — configured vendors + row counts
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, StreamingResponse

from services import database
from services import vendor_offers as vo
from services import vendor_offers_export as vo_export
from services import vendor_offers_template_export as vo_tpl
from services.spapi import reports as sp_reports

log = logging.getLogger(__name__)
router = APIRouter()

_ALLOWED_EXT = {".xlsx", ".xls", ".csv", ".xlsm"}
_PAGE = Path(__file__).resolve().parent.parent / "static" / "price_desk.html"


@router.get("/vendor-offers/status")
async def status() -> dict:
    vo.init_schema()
    with database._connect() as conn:
        def n(t):
            try:
                return conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except Exception:
                return 0
        counts = {t: n(t) for t in ("vo_vendor_offers", "vo_products", "vo_asin_listings")}
    return {"upload_vendors": sorted(vo.VENDOR_LOADERS), "counts": counts}


@router.get("/vendor-offers/data")
async def data() -> dict:
    return await run_in_threadpool(vo.build_ledger)


def _xlsx_response(blob: bytes, filename: str) -> StreamingResponse:
    return StreamingResponse(
        iter([blob]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/vendor-offers/export")
async def export(body: dict = Body(default=None)) -> StreamingResponse:
    """Formatted Excel of the price comparison. Body (optional):
      { upcs: [...] }         → restrict to (and order by) exactly these items
                               (i.e. export the current on-screen view)
      { focus_vendor: "..." } → add a "<vendor> cheapest?" column
    Omit the body for the full book."""
    body = body or {}
    upcs = body.get("upcs") or None
    focus = (body.get("focus_vendor") or "").strip() or None
    try:
        blob = await run_in_threadpool(vo_export.build_xlsx, upcs, focus)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Export failed: {str(exc)[:200]}")
    return _xlsx_response(blob, vo_export.export_filename())


# ── Offer Analytics export (template format) — background job ──────────────
_ANALYTICS_JOBS: dict[str, dict] = {}
_AJOBS_LOCK = threading.Lock()
_AJOB_TTL = 3600


def _get_ajob(job_id: str) -> dict:
    now = time.time()
    with _AJOBS_LOCK:
        for jid in [j for j, jb in _ANALYTICS_JOBS.items()
                    if jb["status"] != "running" and now - jb["started"] > _AJOB_TTL]:
            _ANALYTICS_JOBS.pop(jid, None)
        job = _ANALYTICS_JOBS.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found (or expired).")
    return job


@router.post("/vendor-offers/analytics-export/start")
async def analytics_export_start(body: dict = Body(default=None)) -> dict:
    """Start building the Offer-Analytics (template) workbook. Body (optional):
    { upcs: [...] } restricts to the current view; { prep_fee } sets Prep & Out;
    { live: false } skips the per-ASIN SP-API enrichment (Approved + Storage)."""
    body = body or {}
    upcs = body.get("upcs") or None
    focus = (body.get("focus_vendor") or "").strip() or None
    has_asin = bool(body.get("has_asin"))
    only_available = bool(body.get("only_available"))
    prep = body.get("prep_fee")
    prep = float(prep) if prep is not None else None      # None → build reads the setting
    live = bool(body.get("live", True))
    job_id = uuid.uuid4().hex[:12]
    filename = vo_tpl.export_filename(focus)              # increment the per-vendor/day seq once
    job = {"status": "running", "phase": "pulling Amazon data", "done": 0,
           "total": 0, "blob": None, "error": None, "started": time.time(),
           "phase_started": time.time(), "filename": filename, "cancel": False}
    with _AJOBS_LOCK:
        _ANALYTICS_JOBS[job_id] = job

    def _progress(done: int, total: int, phase: str | None = None) -> None:
        if phase and phase != job.get("phase"):
            job["phase"], job["phase_started"] = phase, time.time()
        job["done"], job["total"] = done, total

    def _run() -> None:
        try:
            job["blob"] = vo_tpl.build_template_xlsx(
                upcs=upcs, focus_vendor=focus, prep_fee=prep, has_asin=has_asin,
                only_available=only_available, live=live, on_progress=_progress,
                should_cancel=lambda: job["cancel"])
            job["phase"], job["status"] = "done", "complete"
        except vo_tpl.ExportCancelled:
            job["status"], job["phase"] = "cancelled", "cancelled"
        except Exception as exc:  # noqa: BLE001
            log.exception("[analytics-export] failed")
            job["status"], job["error"] = "error", str(exc)[:400]

    threading.Thread(target=_run, name=f"analytics-export-{job_id}", daemon=True).start()
    return {"job_id": job_id, "filename": filename}


@router.post("/vendor-offers/analytics-export/jobs/{job_id}/cancel")
async def analytics_export_cancel(job_id: str) -> dict:
    job = _get_ajob(job_id)
    job["cancel"] = True
    return {"cancelled": True}


@router.get("/vendor-offers/sales-traffic/status")
async def sales_traffic_status() -> dict:
    """Cache state of the SP-API Sales & Traffic (30d units) report."""
    return sp_reports.cache_status()


@router.post("/vendor-offers/sales-traffic/refresh")
async def sales_traffic_refresh() -> dict:
    """Regenerate the Sales & Traffic report in the background (pre-warm the cache)."""
    threading.Thread(target=lambda: sp_reports.get_units_30d(max_age_hours=0),
                     name="sales-traffic-refresh", daemon=True).start()
    return {"started": True}


@router.get("/vendor-offers/analytics-export/jobs/{job_id}")
async def analytics_export_status(job_id: str) -> dict:
    job = _get_ajob(job_id)
    eta = None
    if job["status"] == "running" and job["done"] > 0 and job["total"]:
        el = time.time() - job.get("phase_started", job["started"])   # ETA within the current phase
        eta = max(0, round(el / job["done"] * (job["total"] - job["done"])))
    return {"status": job["status"], "phase": job["phase"], "done": job["done"],
            "total": job["total"], "eta_seconds": eta, "error": job["error"]}


@router.get("/vendor-offers/analytics-export/jobs/{job_id}/download")
async def analytics_export_download(job_id: str) -> StreamingResponse:
    job = _get_ajob(job_id)
    if job["status"] != "complete" or not job["blob"]:
        raise HTTPException(status_code=409, detail=f"Not ready ({job['status']}).")
    return _xlsx_response(job["blob"], job.get("filename") or "offer_analytics.xlsx")


@router.post("/vendor-offers/lookup-export")
async def lookup_export(body: dict = Body(...)) -> StreamingResponse:
    """Want-list price check: given pasted UPCs/ASINs (`text`) or an explicit
    `identifiers` list, return an xlsx of who's cheapest per item (or NOT FOUND)."""
    ids = body.get("identifiers")
    if not ids:
        ids = vo_export.parse_identifiers(body.get("text") or "")
    if not ids:
        raise HTTPException(status_code=400, detail="No UPCs or ASINs provided.")
    if len(ids) > 20000:
        raise HTTPException(status_code=400, detail="Too many identifiers (max 20,000).")
    try:
        blob = await run_in_threadpool(vo_export.build_lookup_xlsx, ids)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Lookup export failed: {str(exc)[:200]}")
    return _xlsx_response(blob, vo_export.export_filename("price_check"))


@router.post("/vendor-offers/upload")
async def upload(vendor: str = Form(...), file: UploadFile = File(...)) -> dict:
    vendor = (vendor or "").strip()
    if vendor not in vo.VENDOR_LOADERS:
        raise HTTPException(status_code=400, detail=(
            f"No loader for {vendor!r}. Known vendors: {sorted(vo.VENDOR_LOADERS)}"
        ))
    fname = Path(file.filename or "catalog").name
    if Path(fname).suffix.lower() not in _ALLOWED_EXT:
        raise HTTPException(status_code=400, detail=(
            f"Unsupported file type {Path(fname).suffix!r}. Expected one of {sorted(_ALLOWED_EXT)}."
        ))
    body = await file.read()
    if not body:
        raise HTTPException(status_code=400, detail="Empty file.")

    # save under data/vendor_incoming/<vendor>/, stamped "now" so the loader's
    # file-date staleness marks it as today's quote (an upload = a current quote)
    safe_vendor = re.sub(r"[^A-Za-z0-9 &._-]", "_", vendor).strip() or "vendor"
    dest_dir = vo.INCOMING_DIR / safe_vendor
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / fname
    dest.write_bytes(body)

    try:
        stats = await run_in_threadpool(vo.ingest_vendor_file, vendor, dest)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("[vendor_offers] ingest failed for %s", vendor)
        raise HTTPException(status_code=500, detail=f"Ingest failed: {str(exc)[:200]}")
    return {"ok": True, **stats}


@router.post("/vendor-offers/sync-asins")
async def sync_asins() -> dict:
    return await run_in_threadpool(vo.sync_asins_from_pair_library)


@router.post("/vendor-offers/assign")
async def assign(body: dict = Body(...)) -> dict:
    try:
        return await run_in_threadpool(
            vo.assign_asin,
            body.get("upc"), body.get("asin"), body.get("amazon_pack_size"),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/vendor-offers/assign-bulk")
async def assign_bulk(body: dict = Body(...)) -> dict:
    return await run_in_threadpool(
        vo.assign_bulk, body.get("text") or "", bool(body.get("dry")))


@router.get("/vendor-offers/page", response_class=HTMLResponse)
async def page() -> HTMLResponse:
    """Serve the Price Desk page with the ledger data inlined (same design as the
    standalone build step), so the embedded view is self-contained per load."""
    html = _PAGE.read_text(encoding="utf-8")
    payload = await run_in_threadpool(vo.build_ledger)
    # json.dumps escapes non-ASCII to \uXXXX; escape </ so it can't close <script>
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    html = html.replace("/*__DATA__*/null", blob, 1)
    return HTMLResponse(content=html, headers={"Cache-Control": "no-store"})
