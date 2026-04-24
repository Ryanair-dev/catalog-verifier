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

import csv
import io
import json
import sqlite3
from typing import Any

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from openpyxl import load_workbook

from services import database
from services.analytics import parse_source_rows, start_analytics_run
from services.spapi import sp_api_configured

router = APIRouter(prefix="/analytics")


# --------------------------------------------------------------------------- #
# Raw-row file preview — NO header assumption.
# --------------------------------------------------------------------------- #

_PREVIEW_LIMIT = 25


def _parse_raw_rows(filename: str, data: bytes) -> list[list[Any]]:
    name = (filename or "").lower()
    if name.endswith(".csv") or name.endswith(".tsv"):
        text = data.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))
        return [list(r) for r in reader]
    wb = load_workbook(io.BytesIO(data), data_only=True)
    ws = wb.active
    return [list(r) for r in ws.iter_rows(values_only=True)]


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
def get_run_detail(run_id: int, limit: int = 200, offset: int = 0) -> dict[str, Any]:
    """
    Return a single run row + up to `limit` candidates. The UI can use
    `offset` + `limit` to page through result sets on runs with many rows.
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

        rows = conn.execute(
            "SELECT row_idx, data_json FROM analytics_catalog_rows "
            "WHERE run_id=? ORDER BY row_idx",
            (run_id,),
        ).fetchall()
        catalog_rows = [json.loads(r["data_json"]) for r in rows]

        cand_rows = conn.execute(
            "SELECT row_idx, asin, sources, confidence, verdict, amz_pack, "
            "       review_status, data_json "
            "FROM analytics_candidates WHERE run_id=? "
            "ORDER BY row_idx, confidence DESC "
            "LIMIT ? OFFSET ?",
            (run_id, int(limit), int(offset)),
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
