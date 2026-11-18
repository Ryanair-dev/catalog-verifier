"""
Competitor Analysis -- find every competing listing of one of our products
(same item, other brands) and size up the market. See
services/competitor_analysis.py and docs/features/competitor_analysis.md.

POST /api/competitor-analysis/start          -- one product (JSON)
POST /api/competitor-analysis/start-bulk     -- spreadsheet, one product per row
GET  /api/competitor-analysis/template       -- blank bulk template (.xlsx)
GET  /api/competitor-analysis/jobs/{job_id}  -- poll status/progress (+ summary when done)
GET  /api/competitor-analysis/jobs/{job_id}/download -- xlsx once complete
"""
from __future__ import annotations

import io
import logging
import threading
import time
import uuid

from fastapi import APIRouter, Body, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from openpyxl import Workbook

from services import competitor_analysis as ca
from services import file_parser, job_persistence, job_results

log = logging.getLogger(__name__)
router = APIRouter()

_JOBS: dict[str, dict] = {}
_JOBS_LOCK = threading.Lock()
_JOB_TTL = 3600
MAX_BULK = 50

_COLS = {
    "description": ("description", "product", "product description", "item", "item description", "title"),
    "our_asin": ("our asin", "asin"),
    "our_price": ("our price", "price", "our price per unit", "price per unit"),
    "extra_terms": ("extra search terms", "search terms", "keywords", "extra terms"),
}


def _get_job(job_id: str) -> dict:
    now = time.time()
    with _JOBS_LOCK:
        for jid in [j for j, jb in _JOBS.items()
                    if jb["status"] != "running" and jb.get("completed_at")
                    and now - jb["completed_at"] > _JOB_TTL]:
            _JOBS.pop(jid, None)
        job = _JOBS.get(job_id)
    if not job:
        job = job_results.stub_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found (or expired).")
    return job


def list_jobs_for_tasks_panel() -> list[dict]:
    with _JOBS_LOCK:
        items = list(_JOBS.items())
    return [{"feature": "Competitor Analysis", "job_id": jid, "status": j.get("status"), "phase": j.get("phase"),
             "done": j.get("done"), "total": j.get("total"), "started_at": j.get("started"),
             "completed_at": j.get("completed_at")} for jid, j in items]


def _clean_product(p: dict) -> dict | None:
    desc = str(p.get("description") or "").strip()
    asin = str(p.get("our_asin") or "").strip().upper()
    if not desc and not asin:
        return None
    price = p.get("our_price")
    try:
        price = float(str(price).replace("$", "").replace(",", "")) if price not in (None, "") else None
    except ValueError:
        price = None
    terms = p.get("extra_terms") or []
    if isinstance(terms, str):
        terms = [t.strip() for t in terms.replace(";", ",").replace("\n", ",").split(",")]
    terms = [t for t in terms if t][:10]
    return {"description": desc[:400], "our_asin": asin or None, "our_price": price, "extra_terms": terms}


def _launch(products: list[dict], job_id: str | None = None) -> str:
    job_id = job_id or uuid.uuid4().hex[:12]
    job = {"status": "running", "phase": "starting", "done": 0, "total": 0, "blob": None, "error": None,
           "filename": None, "summary": None, "completed_at": None, "started": time.time(),
           "phase_started": time.time(), "products": len(products)}
    with _JOBS_LOCK:
        _JOBS[job_id] = job
    job_persistence.save_job(job_id, "competitor_analysis", {"products": products, "_job_id": job_id})

    def _progress(done: int, total: int, phase: str | None = None) -> None:
        if phase and phase != job.get("phase"):
            job["phase"], job["phase_started"] = phase, time.time()
        job["done"], job["total"] = done, total

    def _run() -> None:
        try:
            blob, results = ca.run(products, on_progress=_progress, should_cancel=lambda: job.get("cancel"))
            job["filename"] = ca.filename(results)
            job["summary"] = ca.summary_for_ui(results)
            job["blob"] = blob
            job_results.save(job_id, "competitor_analysis", "Competitor Analysis", job["filename"], blob)
            job["phase"], job["status"] = "done", "complete"
        except Exception as exc:  # noqa: BLE001
            log.exception("[competitor-analysis] failed")
            job["status"], job["error"] = ("cancelled", None) if job.get("cancel") else ("error", str(exc)[:400])
        finally:
            job["completed_at"] = time.time()
            job_persistence.finish_job(job_id)

    threading.Thread(target=_run, name=f"competitor-analysis-{job_id}", daemon=True).start()
    return job_id


def _relaunch(params: dict) -> None:
    products = [p for p in (params.get("products") or []) if isinstance(p, dict)]
    if products:
        _launch(products, job_id=params.get("_job_id"))


job_persistence.register_relauncher("competitor_analysis", _relaunch)


@router.post("/competitor-analysis/start")
async def competitor_start(body: dict = Body(...)) -> dict:
    p = _clean_product(body)
    if not p:
        raise HTTPException(status_code=400, detail="Describe the product (e.g. '3 inch paring knife') or give our ASIN.")
    return {"job_id": _launch([p]), "products": 1}


@router.post("/competitor-analysis/start-bulk")
async def competitor_start_bulk(file: UploadFile = File(...)) -> dict:
    data = await file.read()
    try:
        headers, rows = file_parser.parse_file(file.filename or "upload.xlsx", data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Couldn't read the file: {exc}") from exc
    norm = [str(h or "").strip().lower() for h in headers]
    idx = {key: next((norm.index(a) for a in aliases if a in norm), None) for key, aliases in _COLS.items()}
    if idx["description"] is None and idx["our_asin"] is None:
        raise HTTPException(status_code=400, detail="Need a 'Description' column (or 'Our ASIN'). "
                                                    "Download the template for the expected columns.")
    products = []
    for r in rows:
        p = _clean_product({k: (r[i] if i is not None and i < len(r) else None) for k, i in idx.items()})
        if p:
            products.append(p)
    if not products:
        raise HTTPException(status_code=400, detail="No product rows found.")
    if len(products) > MAX_BULK:
        raise HTTPException(status_code=400, detail=f"{len(products)} rows -- max {MAX_BULK} products per run.")
    return {"job_id": _launch(products), "products": len(products)}


@router.get("/competitor-analysis/template")
async def competitor_template() -> StreamingResponse:
    wb = Workbook()
    ws = wb.active
    ws.title = "Products"
    ws.append(["Description", "Our ASIN", "Our Price", "Extra Search Terms"])
    ws.append(["3 inch paring knife, single knife", "", 12.99, "3\" paring knife, small kitchen knife"])
    for col, w in zip("ABCD", (45, 14, 11, 45)):
        ws.column_dimensions[col].width = w
    buf = io.BytesIO()
    wb.save(buf)
    return StreamingResponse(iter([buf.getvalue()]),
                             media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": 'attachment; filename="competitor_analysis_template.xlsx"'})


@router.post("/competitor-analysis/jobs/{job_id}/cancel")
async def competitor_cancel(job_id: str) -> dict:
    job = _get_job(job_id)
    if job.get("status") == "running":
        job["cancel"] = True
    return {"job_id": job_id, "status": job.get("status")}


@router.get("/competitor-analysis/jobs/{job_id}")
async def competitor_status(job_id: str) -> dict:
    job = _get_job(job_id)
    eta = None
    if job["status"] == "running" and job.get("done") and job.get("total"):
        elapsed = time.time() - job["phase_started"]
        eta = max(0, round(elapsed / job["done"] * (job["total"] - job["done"])))
    return {"job_id": job_id, "status": job["status"], "phase": job.get("phase"), "done": job.get("done"),
            "total": job.get("total"), "error": job.get("error"), "filename": job.get("filename"),
            "summary": job.get("summary"), "eta_seconds": eta}


@router.get("/competitor-analysis/jobs/{job_id}/download")
async def competitor_download(job_id: str) -> StreamingResponse:
    job = _get_job(job_id)
    blob = job.get("blob") or (job_results.load_blob(job_id) if job["status"] == "complete" else None)
    if job["status"] != "complete" or not blob:
        raise HTTPException(status_code=409, detail=f"Job is {job['status']!r}, not ready.")
    return StreamingResponse(iter([blob]),
                             media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                             headers={"Content-Disposition": f'attachment; filename="{job.get("filename") or "competitor_analysis.xlsx"}"'})
