"""
Analytics tab (ROI & Cost) — FastAPI router.

Endpoints
---------
POST /api/analytics/preview  — upload file, return first N raw rows so the
                              wizard can show the header-row picker.
POST /api/analytics/runs     — create a run from the wizard payload + file,
                              kick off the 3-tier SP-API search + vetting
                              in a background thread.
GET  /api/analytics/runs     — list runs (newest first) for the history
                              table in the Analytics tab.
GET  /api/analytics/runs/{id}
                             — single run detail (progress + counts + the
                              first 200 candidates so the UI can render
                              a result grid without a second round-trip).
GET  /api/analytics/status   — shallow SP-API credentials probe.

The heavy lifting lives in `services.analytics.runner` — this router is
just the HTTP glue.
"""
from __future__ import annotations

import io
import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.styles import Font
from openpyxl.cell import WriteOnlyCell

from services import database
from services.file_parser import parse_raw_rows as _parse_raw_rows_shared
from services.analytics import parse_source_rows, start_analytics_run
from services.analytics.runner import request_pause, request_stop, resume_run, start_rescore
from services.analytics.ai_check import start_ai_check, is_running as ai_check_running, estimate_cost as ai_check_cost
from services.spapi import sp_api_configured

router = APIRouter(prefix="/analytics")


# --------------------------------------------------------------------------- #
# Raw-row file preview — NO header assumption.
# --------------------------------------------------------------------------- #

_PREVIEW_LIMIT = 25


def _parse_raw_rows(filename: str, data: bytes) -> list[list[Any]]:
    return _parse_raw_rows_shared(filename, data)


@router.post("/preview")
async def analytics_preview(catalog_file: UploadFile = File(...)) -> dict[str, Any]:
    """Return the first N raw rows so the wizard can render a header-row picker."""
    try:
        data = await catalog_file.read()
        rows = _parse_raw_rows(catalog_file.filename or "", data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Could not parse file: {exc}")

    if not rows:
        raise HTTPException(status_code=400, detail="File appears to be empty")

    preview = rows[:_PREVIEW_LIMIT]
    normalised = []
    max_cols = 0
    for i, r in enumerate(preview):
        cells = [("" if c is None else str(c)) for c in r]
        max_cols = max(max_cols, len(cells))
        normalised.append({"row_number": i + 1, "cells": cells})
    for row in normalised:
        if len(row["cells"]) < max_cols:
            row["cells"] += [""] * (max_cols - len(row["cells"]))

    return {
        "filename": catalog_file.filename,
        "size": len(data),
        "total_rows": len(rows),
        "rows": normalised,
        "max_cols": max_cols,
    }


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #


@router.get("/status")
def analytics_status() -> dict[str, Any]:
    return {"sp_api_configured": sp_api_configured()}


# --------------------------------------------------------------------------- #
# Runs list + detail
# --------------------------------------------------------------------------- #


def _decode_search_methods(value: Any) -> Any:
    if not value:
        return value
    try:
        return json.loads(value)
    except (ValueError, TypeError):
        return value


@router.get("/runs")
def list_analytics_runs() -> list[dict[str, Any]]:
    """Return every analytics run, newest first. Shape matches the UI's expectations."""
    with database._connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, name, marketplace, search_methods, pages_per_title,
                   ai_clean_titles, total_catalog_items, total_candidates_found,
                   verified_count, review_count, not_approved_count,
                   status, progress_phase, progress_done, progress_total,
                   max_rank, min_rank,
                   ai_check_status, ai_check_done, ai_check_total,
                   created_at, updated_at
            FROM analytics_runs
            ORDER BY created_at DESC
            """
        ).fetchall()

    out: list[dict[str, Any]] = []
    for r in rows:
        d = dict(r)
        d["search_methods"] = _decode_search_methods(d.get("search_methods"))
        out.append(d)
    return out


@router.get("/runs/{run_id}")
def get_run_detail(
    run_id: int,
    limit: int = 200,
    offset: int = 0,
    verdict: str = "",
    max_rank: int = 0,
) -> dict[str, Any]:
    """
    Return a single run row + up to `limit` candidates.

    max_rank: when > 0, only return candidates whose sales_rank <= max_rank
              (or whose rank is unknown/null). Pass 0 to disable.
    """
    with database._connect() as conn:
        conn.row_factory = sqlite3.Row
        run = conn.execute(
            "SELECT * FROM analytics_runs WHERE id=?", (run_id,),
        ).fetchone()
        if not run:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        run_d = dict(run)
        run_d["search_methods"] = _decode_search_methods(run_d.get("search_methods"))

        # Catalog rows are only needed for the initial full-data load.
        # Verdict-filtered fetches skip them — the client already has them.
        if not verdict:
            rows = conn.execute(
                "SELECT row_idx, data_json FROM analytics_catalog_rows "
                "WHERE run_id=? ORDER BY row_idx",
                (run_id,),
            ).fetchall()
            catalog_rows = [json.loads(r["data_json"]) for r in rows]
        else:
            catalog_rows = []

        rank_clause = "AND (sales_rank IS NULL OR sales_rank <= ?)" if max_rank > 0 else ""
        select_cols = (
            "row_idx, asin, sources, confidence, verdict, amz_pack, "
            "sales_rank, review_status, ai_verdict, ai_reasoning, data_json"
        )

        if verdict:
            base_params: list = [run_id, str(verdict)]
            if max_rank > 0:
                base_params.append(int(max_rank))
            if limit > 0:
                cand_rows = conn.execute(
                    f"SELECT {select_cols} FROM analytics_candidates "
                    f"WHERE run_id=? AND verdict=? {rank_clause} "
                    f"ORDER BY confidence DESC, row_idx, asin LIMIT ? OFFSET ?",
                    base_params + [int(limit), int(offset)],
                ).fetchall()
            else:
                cand_rows = conn.execute(
                    f"SELECT {select_cols} FROM analytics_candidates "
                    f"WHERE run_id=? AND verdict=? {rank_clause} "
                    f"ORDER BY confidence DESC, row_idx, asin",
                    base_params,
                ).fetchall()
        else:
            base_params = [run_id]
            if max_rank > 0:
                base_params.append(int(max_rank))
            cand_rows = conn.execute(
                f"SELECT {select_cols} FROM analytics_candidates "
                f"WHERE run_id=? {rank_clause} "
                f"ORDER BY confidence DESC, row_idx, asin "
                f"LIMIT ? OFFSET ?",
                base_params + [int(limit), int(offset)],
            ).fetchall()

    candidates: list[dict] = []
    for c in cand_rows:
        d = dict(c)
        try:
            d["sources"] = json.loads(d.get("sources") or "[]")
        except (ValueError, TypeError):
            d["sources"] = []
        try:
            d["data"] = json.loads(d.pop("data_json") or "{}")
        except (ValueError, TypeError):
            d["data"] = {}
        # Expose sales_rank at the top level for the UI; also pull from
        # data.amazon.sales_rank for rows that predate the column migration.
        if d.get("sales_rank") is None:
            amz = (d.get("data") or {}).get("amazon") or {}
            sr = amz.get("sales_rank")
            if sr is not None:
                d["sales_rank"] = sr
        candidates.append(d)

    return {
        "run": run_d,
        "catalog_rows": catalog_rows,
        "candidates": candidates,
    }


# --------------------------------------------------------------------------- #
# POST /runs — the wizard endpoint
# --------------------------------------------------------------------------- #


def _parse_json_field(raw: str, default: Any) -> Any:
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


@router.post("/runs")
async def create_analytics_run(
    catalog_file: UploadFile = File(...),
    name: str = Form(...),
    marketplace: str = Form("US"),
    header_row: int = Form(...),
    mapping: str = Form(...),           # JSON-encoded {"upc": "3", ...}
    brand: str = Form(""),
    search_methods: str = Form(...),    # JSON-encoded list
    pages_per_title: int = Form(5),
    ai_clean_titles: bool = Form(False),
    max_rank: int = Form(0),
    min_rank: int = Form(0),
) -> dict[str, Any]:
    """
    Create a new Analytics run.

    The wizard sends us a multipart POST with the raw catalog file plus
    every decision the user made in steps 1–4. We parse the file with
    their chosen header row + column mapping, persist the resulting
    source rows, and kick off a background thread that runs the
    3-tier SP-API search + vetting.

    Response shape:
        {"run_id": 17, "total_catalog_items": 142, "status": "Searching"}
    """
    try:
        data = await catalog_file.read()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Could not read upload: {exc}")

    mapping_dict = _parse_json_field(mapping, {})
    methods_list = _parse_json_field(search_methods, [])
    if not isinstance(mapping_dict, dict):
        raise HTTPException(status_code=400, detail="mapping must be a JSON object")
    if not isinstance(methods_list, list) or not methods_list:
        raise HTTPException(status_code=400, detail="search_methods must be a non-empty JSON array")

    # Parse the file into source rows using the wizard's picks.
    try:
        rows = parse_source_rows(
            filename=catalog_file.filename or "catalog.xlsx",
            data=data,
            header_row_idx=int(header_row),
            mapping=mapping_dict,
            brand=brand or "",
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Could not parse file: {exc}")

    if not rows:
        raise HTTPException(
            status_code=400,
            detail="No catalog rows found after applying your column mapping. "
                   "Check the header row and UPC / Item ID / Title columns.",
        )

    run_id = start_analytics_run(
        name=name.strip() or (catalog_file.filename or "Untitled run"),
        marketplace=marketplace or "US",
        search_methods=[str(m) for m in methods_list],
        pages_per_title=int(pages_per_title),
        ai_clean_titles=bool(ai_clean_titles),
        source_rows=rows,
        max_rank=int(max_rank),
        min_rank=int(min_rank),
    )

    return {
        "run_id": run_id,
        "total_catalog_items": len(rows),
        "status": "Searching" if sp_api_configured() else "Error",
        "sp_api_configured": sp_api_configured(),
    }


# --------------------------------------------------------------------------- #
# POST /runs/{run_id}/candidates/verdict — manual verdict override
# --------------------------------------------------------------------------- #

_VERDICT_VALUES = {"verified", "review", "not_approved"}


def _recompute_run_counts(conn: sqlite3.Connection, run_id: int) -> dict[str, int]:
    rows = conn.execute(
        "SELECT verdict, COUNT(*) AS n FROM analytics_candidates "
        "WHERE run_id=? GROUP BY verdict",
        (run_id,),
    ).fetchall()
    verified = review = not_approved = total = 0
    for r in rows:
        v = (r["verdict"] or "").lower()
        n = int(r["n"])
        total += n
        if v == "verified":
            verified += n
        elif v == "review":
            review += n
        elif v == "not_approved":
            not_approved += n
    conn.execute(
        "UPDATE analytics_runs SET total_candidates_found=?, "
        "  verified_count=?, review_count=?, not_approved_count=?, "
        "  updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (total, verified, review, not_approved, run_id),
    )
    return {
        "total_candidates_found": total,
        "verified_count": verified,
        "review_count": review,
        "not_approved_count": not_approved,
    }


@router.post("/runs/{run_id}/candidates/verdict")
def update_candidate_verdict(
    run_id: int,
    body: dict = Body(...),
) -> dict[str, Any]:
    """
    Manually override the verdict on one candidate row. The wizard sets
    verdicts automatically from the confidence-score thresholds; this
    endpoint is the UI's "Promote" / "Reject" / "Approve" / "Discard"
    button — the same workflow the Vetting review modal uses.

    Body: {"row_idx": int, "asin": str, "verdict": "verified"|"review"|"not_approved",
           "review_status": str (optional)}
    """
    row_idx = body.get("row_idx")
    asin = body.get("asin")
    verdict = (body.get("verdict") or "").lower().strip()
    review_status = body.get("review_status") or ""

    if row_idx is None or asin is None:
        raise HTTPException(status_code=400, detail="row_idx and asin are required")
    if verdict not in _VERDICT_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"verdict must be one of {sorted(_VERDICT_VALUES)}",
        )

    with database._LOCK, database._connect() as conn:
        conn.row_factory = sqlite3.Row

        # Update the candidate row and also rewrite the embedded verdict
        # inside data_json so CSV exports / UI reloads stay consistent.
        existing = conn.execute(
            "SELECT data_json FROM analytics_candidates "
            "WHERE run_id=? AND row_idx=? AND asin=?",
            (int(run_id), int(row_idx), str(asin)),
        ).fetchone()
        if not existing:
            raise HTTPException(status_code=404, detail="Candidate not found")

        try:
            data = json.loads(existing["data_json"] or "{}")
        except (ValueError, TypeError):
            data = {}
        data["verdict"] = verdict
        if review_status:
            data["review_status"] = review_status

        conn.execute(
            "UPDATE analytics_candidates "
            "SET verdict=?, review_status=?, data_json=? "
            "WHERE run_id=? AND row_idx=? AND asin=?",
            (verdict, review_status, json.dumps(data),
             int(run_id), int(row_idx), str(asin)),
        )

        counts = _recompute_run_counts(conn, int(run_id))

    return {
        "ok": True,
        "run_id": int(run_id),
        "row_idx": int(row_idx),
        "asin": str(asin),
        "verdict": verdict,
        "review_status": review_status,
        "counts": counts,
    }


@router.post("/runs/{run_id}/candidates/bulk_verdict")
def bulk_update_candidate_verdict(
    run_id: int,
    body: dict = Body(...),
) -> dict[str, Any]:
    """
    Apply the same verdict to a list of (row_idx, asin) pairs. This powers
    the "Reject all" / "Approve all" / "Promote all" bulk actions in the
    Approved / Review / Not Approved tabs of the run detail view.

    Body: {"items": [{"row_idx": int, "asin": str}, ...],
           "verdict": "verified"|"review"|"not_approved",
           "review_status": str (optional)}
    """
    items = body.get("items") or []
    verdict = (body.get("verdict") or "").lower().strip()
    review_status = body.get("review_status") or ""

    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=400, detail="items must be a non-empty list")
    if verdict not in _VERDICT_VALUES:
        raise HTTPException(
            status_code=400,
            detail=f"verdict must be one of {sorted(_VERDICT_VALUES)}",
        )

    updated = 0
    with database._LOCK, database._connect() as conn:
        conn.row_factory = sqlite3.Row
        for it in items:
            row_idx = it.get("row_idx")
            asin = it.get("asin")
            if row_idx is None or asin is None:
                continue
            existing = conn.execute(
                "SELECT data_json FROM analytics_candidates "
                "WHERE run_id=? AND row_idx=? AND asin=?",
                (int(run_id), int(row_idx), str(asin)),
            ).fetchone()
            if not existing:
                continue
            try:
                data = json.loads(existing["data_json"] or "{}")
            except (ValueError, TypeError):
                data = {}
            data["verdict"] = verdict
            if review_status:
                data["review_status"] = review_status
            conn.execute(
                "UPDATE analytics_candidates "
                "SET verdict=?, review_status=?, data_json=? "
                "WHERE run_id=? AND row_idx=? AND asin=?",
                (verdict, review_status, json.dumps(data),
                 int(run_id), int(row_idx), str(asin)),
            )
            updated += 1

        counts = _recompute_run_counts(conn, int(run_id))

    return {
        "ok": True,
        "run_id": int(run_id),
        "updated": updated,
        "verdict": verdict,
        "review_status": review_status,
        "counts": counts,
    }


# --------------------------------------------------------------------------- #
# POST /runs/{run_id}/rescore — re-score candidates with a different title col
# --------------------------------------------------------------------------- #


@router.post("/runs/{run_id}/rescore")
def rescore_run(run_id: int, body: dict = Body(...)) -> dict[str, Any]:
    """
    Kick off a background re-score for an existing run using a different title
    column from the original catalog's raw data.

    Body: {"title_col": "Full Product Title"}
      - title_col: column header name from the raw dict stored in
        analytics_catalog_rows.data_json.raw.  Pass "" to use the
        originally-mapped title field.

    Returns immediately; progress is tracked via the run's progress_done/total
    fields (same mechanism as the search pipeline).
    """
    with database._connect() as conn:
        conn.row_factory = sqlite3.Row
        run = conn.execute(
            "SELECT id FROM analytics_runs WHERE id=?", (run_id,)
        ).fetchone()
        if not run:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    title_col = (body.get("title_col") or "").strip()
    brand_col = (body.get("brand_col") or "").strip()
    max_rank  = int(body.get("max_rank") or 0)
    min_rank  = int(body.get("min_rank") or 0)
    start_rescore(int(run_id), title_col, brand_col, max_rank, min_rank)
    return {"ok": True, "started": True}


# --------------------------------------------------------------------------- #
# POST /runs/{run_id}/control — pause / stop / resume
# --------------------------------------------------------------------------- #

_ACTIVE_STATUSES   = {"Searching", "Pending", "Vetting"}
_PAUSABLE_STATUSES = {"Searching", "Vetting"}
_RESUMABLE_STATUSES = {"Paused", "Stopped"}
_STOPPABLE_STATUSES = _PAUSABLE_STATUSES | _RESUMABLE_STATUSES | {"Pending"}


@router.post("/runs/{run_id}/control")
def control_analytics_run(run_id: int, body: dict = Body(...)) -> dict[str, Any]:
    """
    Body: {"action": "pause" | "stop" | "resume"}

    pause  — signals the running pipeline to pause after its current step.
    stop   — signals the pipeline to stop and mark the run as Stopped.
    resume — restarts a Paused/Stopped run from where it left off.
    """
    action = (body.get("action") or "").lower().strip()
    if action not in ("pause", "stop", "resume"):
        raise HTTPException(status_code=400, detail="action must be pause, stop, or resume")

    with database._connect() as conn:
        conn.row_factory = sqlite3.Row
        run = conn.execute(
            "SELECT status FROM analytics_runs WHERE id=?", (run_id,)
        ).fetchone()
    if not run:
        raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    status = run["status"] or ""

    if action == "pause":
        if status not in _PAUSABLE_STATUSES:
            raise HTTPException(status_code=400, detail=f"Run is {status} — can only pause a running run")
        request_pause(run_id)
        # Write immediately so the UI reflects it even if the thread hasn't
        # reached its next control-check point yet.
        with database._LOCK, database._connect() as conn:
            conn.execute(
                "UPDATE analytics_runs SET status='Paused', "
                "progress_phase='Paused by user', updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status NOT IN ('Complete', 'Error', 'Stopped')",
                (run_id,),
            )
        return {"ok": True, "action": "pause", "run_id": run_id}

    if action == "stop":
        if status not in _STOPPABLE_STATUSES:
            raise HTTPException(status_code=400, detail=f"Run is {status} — nothing to stop")
        request_stop(run_id)
        # Write Stopped immediately so the UI reflects it even when the
        # background thread has died (e.g. after a server restart).
        with database._LOCK, database._connect() as conn:
            conn.execute(
                "UPDATE analytics_runs SET status='Stopped', "
                "progress_phase='Stopped by user', updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND status NOT IN ('Complete', 'Error')",
                (run_id,),
            )
        return {"ok": True, "action": "stop", "run_id": run_id}

    if action == "resume":
        if status not in _RESUMABLE_STATUSES:
            raise HTTPException(status_code=400, detail=f"Run is {status} — can only resume a Paused or Stopped run")
        ok = resume_run(run_id)
        if not ok:
            raise HTTPException(status_code=400, detail="Could not resume run")
        return {"ok": True, "action": "resume", "run_id": run_id}


# --------------------------------------------------------------------------- #
# DELETE /runs/{run_id} — permanently remove a run and all its data
# --------------------------------------------------------------------------- #

@router.delete("/runs/{run_id}")
def delete_analytics_run(run_id: int) -> dict[str, Any]:
    request_stop(run_id)
    with database._LOCK, database._connect() as conn:
        run = conn.execute(
            "SELECT id FROM analytics_runs WHERE id=?", (run_id,)
        ).fetchone()
        if not run:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        conn.execute("DELETE FROM analytics_runs WHERE id=?", (run_id,))
    return {"ok": True, "deleted": run_id}


# --------------------------------------------------------------------------- #
# AI check endpoints
# --------------------------------------------------------------------------- #


@router.get("/runs/{run_id}/ai_check/estimate")
def ai_check_estimate(run_id: int, verdict: str = "review") -> dict[str, Any]:
    """Return cost/time estimate for running AI check on candidates of this run."""
    with database._connect() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM analytics_candidates "
            "WHERE run_id=?" + (" AND verdict=?" if verdict else ""),
            (run_id, verdict) if verdict else (run_id,),
        ).fetchone()
    count = row["cnt"] if row else 0
    est   = ai_check_cost(count)
    est["already_running"] = ai_check_running(run_id)
    return est


@router.post("/runs/{run_id}/ai_check")
def start_ai_check_endpoint(run_id: int, body: dict = Body(...)) -> dict[str, Any]:
    """Start AI check for candidates of this run in a background thread."""
    with database._connect() as conn:
        conn.row_factory = sqlite3.Row
        run = conn.execute(
            "SELECT id FROM analytics_runs WHERE id=?", (run_id,)
        ).fetchone()
        if not run:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")

    if ai_check_running(run_id):
        return {"ok": True, "already_running": True}

    verdict_filter = (body.get("verdict") or "review").lower()
    if verdict_filter == "all":
        verdict_filter = ""
    start_ai_check(run_id, verdict_filter)
    return {"ok": True, "started": True}


# --------------------------------------------------------------------------- #
# GET /runs/{run_id}/export — multi-sheet Excel download
# --------------------------------------------------------------------------- #

_HDR_FONT = Font(bold=True)


def _hdr_row(ws, titles: list[str]) -> list[WriteOnlyCell]:
    cells = []
    for t in titles:
        c = WriteOnlyCell(ws, value=t)
        c.font = _HDR_FONT
        cells.append(c)
    return cells


@router.get("/runs/{run_id}/export")
def export_analytics_run(
    run_id: int,
    min_rank: int = 0,
    max_rank: int = 0,
    skip_null_rank: bool = False,
):
    """
    Return a four-sheet Excel workbook filtered by the caller's rank window:
      Sheet 1 "Approved"     — verified candidates
      Sheet 2 "Review"       — review candidates
      Sheet 3 "Not Approved" — not_approved candidates
      Sheet 4 "Not Found"    — catalog rows with no Amazon match
    """
    with database._connect() as conn:
        conn.row_factory = sqlite3.Row
        run = conn.execute(
            "SELECT * FROM analytics_runs WHERE id=?", (run_id,)
        ).fetchone()
        if not run:
            raise HTTPException(status_code=404, detail=f"Run {run_id} not found")
        run_d = dict(run)

        cat_rows = conn.execute(
            "SELECT row_idx, data_json FROM analytics_catalog_rows "
            "WHERE run_id=? ORDER BY row_idx",
            (run_id,),
        ).fetchall()

        cand_rows = conn.execute(
            "SELECT row_idx, asin, confidence, verdict, amz_pack, "
            "sales_rank, data_json "
            "FROM analytics_candidates WHERE run_id=? ORDER BY confidence DESC",
            (run_id,),
        ).fetchall()

    src_by_idx: dict[int, dict] = {}
    for r in cat_rows:
        try:
            src_by_idx[r["row_idx"]] = json.loads(r["data_json"] or "{}")
        except (ValueError, TypeError):
            src_by_idx[r["row_idx"]] = {}

    found_idxs = {c["row_idx"] for c in cand_rows}

    def _rank_passes(sales_rank) -> bool:
        if sales_rank is None:
            return not skip_null_rank and max_rank == 0
        r = int(sales_rank)
        if min_rank > 0 and r < min_rank:
            return False
        if max_rank > 0 and r > max_rank:
            return False
        return True

    cand_hdr = [
        "Row", "Source UPC", "Source Item ID", "Source Title", "Source Brand",
        "ASIN", "Amazon Title", "Amazon Brand", "BSR", "Confidence",
    ]

    buckets: dict[str, list] = {"verified": [], "review": [], "not_approved": []}
    for c in cand_rows:
        if not _rank_passes(c["sales_rank"]):
            continue
        src = src_by_idx.get(c["row_idx"], {})
        try:
            amz = (json.loads(c["data_json"] or "{}")).get("amazon") or {}
        except (ValueError, TypeError):
            amz = {}
        row = [
            (c["row_idx"] or 0) + 1,
            src.get("upc") or "",
            src.get("itemid") or "",
            src.get("title") or "",
            src.get("brand") or "",
            c["asin"] or "",
            amz.get("title") or "",
            amz.get("brand") or amz.get("manufacturer") or "",
            c["sales_rank"] if c["sales_rank"] is not None else "",
            round(float(c["confidence"] or 0), 1),
        ]
        buckets.get((c["verdict"] or "").lower(), buckets["not_approved"]).append(row)

    wb = Workbook(write_only=True)

    for sheet_name, verdict_key in [
        ("Approved", "verified"),
        ("Review", "review"),
        ("Not Approved", "not_approved"),
    ]:
        ws = wb.create_sheet(sheet_name)
        ws.append(_hdr_row(ws, cand_hdr))
        for row in buckets[verdict_key]:
            ws.append(row)

    # Not Found sheet — unaffected by rank filter
    ws_nf = wb.create_sheet("Not Found")
    ws_nf.append(_hdr_row(ws_nf, [
        "Row", "Source UPC", "Source Item ID", "Source Title", "Source Brand",
    ]))
    for idx in sorted(src_by_idx.keys()):
        if idx in found_idxs:
            continue
        src = src_by_idx[idx]
        ws_nf.append([
            idx + 1,
            src.get("upc") or "",
            src.get("itemid") or "",
            src.get("title") or "",
            src.get("brand") or "",
        ])

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    safe_name = (run_d.get("name") or f"run-{run_id}").replace(" ", "_")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}_analytics.xlsx"'},
    )
