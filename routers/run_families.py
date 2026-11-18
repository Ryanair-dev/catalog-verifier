"""
Families of one Analytics run, for the UI and as a download. Makes review faster.
"""
from __future__ import annotations

import os
import tempfile
from collections import Counter
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from services.analytics.vendor_families import (
    FILE_VERSION,
    RELATIONS,
    build_families,
    load_run,
    pair_table,
    row_label,
    write_workbook,
)

router = APIRouter()

_VERDICT_CODE = {"Approved": "verified", "Review": "review", "Not Approved": "not_approved"}


def _load(run_id: int) -> tuple[dict, list[dict], list[dict]]:
    vendor_rows, candidates = load_run(run_id)
    if not vendor_rows:
        raise HTTPException(status_code=404, detail=f"Run {run_id} has no vendor rows")
    return build_families(vendor_rows, candidates), candidates, vendor_rows


def _plain(value: Any) -> Any:
    """Values safe for JSON (numbers and text stay, anything else becomes text)."""
    return value if isinstance(value, (int, float, str)) or value is None else str(value)


def _ai_summary(vendor_rows: list[dict], run_id: int) -> tuple[dict, dict]:
    """What the AI reader (brand_extractor.py, GPT-4o-mini) found for this run's vendor rows, and what it spent:
    ({"installed", "switch_on", "rows_total", "rows_with_result", "attributes_found", "sent", "cached",
      "cost_usd", "error", "message"}, {row number as shown: {"attributes": {...}}})."""
    totals = {"installed": True, "switch_on": False, "rows_total": len(vendor_rows), "rows_with_result": 0,
              "attributes_found": 0, "sent": 0, "cached": 0, "cost_usd": 0.0, "error": "", "message": ""}
    per_row: dict = {}
    try:
        from services import database
        from services.analytics import brand_extractor
        with database._connect() as conn:
            row = conn.execute("SELECT ai_clean_titles FROM analytics_runs WHERE id = ?", (run_id,)).fetchone()
        totals["switch_on"] = bool(row and row[0]) and brand_extractor.enabled()
        summary = brand_extractor.run_summary(run_id, vendor_rows)
        for key in ("rows_with_result", "attributes_found", "sent", "cached", "cost_usd", "error"):
            totals[key] = summary[key]
        per_row = {str(row_label(idx)): {"attributes": attrs} for idx, attrs in summary["per_row"].items()}
    except Exception as exc:  # noqa: BLE001 - brand_extractor.py missing or broken
        totals.update(installed=False, message=f"{type(exc).__name__}: {exc}")
    return totals, per_row


@router.get("/run-families/{run_id}")
def run_families(run_id: int) -> dict:
    result, candidates, vendor_rows = _load(run_id)
    lines = pair_table(result, candidates)
    counts: dict[str, Counter] = {}
    for d in lines:
        counts.setdefault(d["Family"], Counter())[d["Relation"]] += 1
    ai_totals, ai_rows = _ai_summary(vendor_rows, run_id)

    families = []
    for f in result["families"]:
        families.append({
            "id": f["id"],
            "name": f["name"],
            "brand": f["brand"],
            "words": f["words"],
            "varying": {box: [_plain(v) for v in values] for box, values in f["varying"].items()},
            "counts": dict(counts.get(f["id"], {})),
            "rows": [{
                "row": row_label(idx),
                "row_idx": idx,
                "title": result["rows"][idx]["title"],
                "variant": result["rows"][idx]["variant"],
                "upc": result["rows"][idx]["upc"],
                "ai": ai_rows.get(str(row_label(idx))),
            } for idx in f["rows"]],
        })

    pairs = [{
        "family": d["Family"],
        "row": d["Row"],
        "asin": d["ASIN"],
        "amazon_title": d["Amazon title"],
        "amazon_brand": d["Amazon brand"],
        "listing_variant": d["Listing variant"],
        "listing_family": d["Listing family"],
        "relation": d["Relation"],
        "why": d["Why"],
        "verdict": _VERDICT_CODE.get(d["Verdict"], d["Verdict"]),
        "confidence": d["Confidence"],
        "review_status": d["Review status"],
    } for d in lines]

    return {"run_id": run_id, "version": FILE_VERSION, "relations": list(RELATIONS),
            "families": families, "pairs": pairs, "ai": ai_totals, "ai_rows": ai_rows}


@router.get("/run-families/{run_id}/export")
def run_families_export(run_id: int) -> Response:
    result, candidates, _ = _load(run_id)
    handle, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(handle)
    try:
        write_workbook(result, candidates, path)
        with open(path, "rb") as fh:
            data = fh.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="run_{run_id}_families.xlsx"'},
    )