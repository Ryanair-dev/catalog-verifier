"""
Match feedback API.   

For everyone (no key):
  GET  /api/feedback/reasons
  POST /api/feedback/report

For the developer (needs ?key=<FEEDBACK_ADMIN_KEY from .env>):
  GET  /api/feedback/export.csv
  GET  /api/feedback/monday/status   how many reports are on the Monday board / still waiting
  POST /api/feedback/monday/retry    send every waiting report now (e.g. after fixing the key)

A report is saved locally and answered "ok" right away; services/monday_reports.py
sends it to the Monday board in the background, so Monday problems never reach the user.
"""
from __future__ import annotations

import logging
import os
import re
import secrets
from typing import Any

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import Response

from services import match_feedback as mf
from services import monday_reports

log = logging.getLogger(__name__)
router = APIRouter(prefix="/feedback")

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")
_VALID_REASONS = set(mf.REASON_LABELS)


@router.get("/reasons")
def reasons() -> dict[str, Any]:
    pack = lambda lst: [{"code": c, "label": l} for c, l in lst]  # noqa: E731
    return {"different": pack(mf.REASONS_DIFFERENT), "same": pack(mf.REASONS_SAME)}


@router.post("/report")
def report(body: dict = Body(...)) -> dict[str, Any]:
    try:
        run_id = int(body.get("run_id"))
        row_idx = int(body.get("row_idx"))
    except (TypeError, ValueError):
        raise HTTPException(400, "run_id and row_idx are required")
    asin = str(body.get("asin") or "").strip().upper()
    if not asin:
        raise HTTPException(400, "asin is required")
    label_txt = str(body.get("label") or "").strip().lower()
    if label_txt not in ("same", "different"):
        raise HTTPException(400, "Choose whether the two are the same product or not.")
    codes = [str(c) for c in (body.get("reasons") or []) if str(c) in _VALID_REASONS]
    note = str(body.get("note") or "").strip()[:1000]
    if not codes and not note:
        raise HTTPException(400, "Pick at least one reason or write a short note.")
    correct = str(body.get("correct_asin") or "").strip().upper()
    if correct and not _ASIN_RE.match(correct):
        raise HTTPException(400, "The correct ASIN should be 10 letters/digits (like B0XXXXXXXX).")
    who = str(body.get("reported_by") or "").strip()[:60]
    try:
        fid = mf.record_report(
            run_id=run_id, row_idx=row_idx, asin=asin, label=1 if label_txt == "same" else 0,
            reasons=codes, note=note, correct_asin=correct, reported_by=who)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    return {"ok": True, "id": fid}


def _check_admin_key(key: str) -> None:
    expected = os.getenv("FEEDBACK_ADMIN_KEY", "")
    if not expected:
        raise HTTPException(403, "Set FEEDBACK_ADMIN_KEY in .env and restart to use this.")
    if not secrets.compare_digest(key or "", expected):
        raise HTTPException(403, "Wrong key.")


@router.get("/monday/status")
def monday_status(key: str = Query("")) -> dict[str, Any]:
    _check_admin_key(key)
    return monday_reports.status()


@router.post("/monday/retry")
def monday_retry(key: str = Query("")) -> dict[str, Any]:
    _check_admin_key(key)
    return {"ok": True, "queued": monday_reports.retry_now()}


@router.get("/export.csv")
def export_csv(key: str = Query("")) -> Response:
    _check_admin_key(key)
    return Response(
        content="\ufeff" + mf.export_csv(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="match_feedback.csv"'},
    )