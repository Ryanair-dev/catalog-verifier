"""
Families of one Analytics run, for the UI and as a download.  Read only: nothing here changes a verdict.

    GET /api/run-families/{run_id}          JSON: the families + every candidate pair with its relation,
                                            + what the AI router did ("ai": expanded vendor titles)
    GET /api/run-families/{run_id}/export   Excel: a Families sheet + a Pairs sheet

Its own URL prefix (/api/run-families/...) so it can never collide with the routes in routers/analytics.py.
Paths here are written without "/api": main.py adds it, the same way as for every other router:
    app.include_router(run_families.router, prefix="/api", tags=["families"])
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


def _ai_summary(vendor_rows: list[dict]) -> tuple[dict, dict]:
    """What the AI title reader did for this run: ({"rows_total", "rows_checked", "rows_with_result",
    "attributes_found", "pairs_ai", "pairs_learned", "switch_on"},
    {row number as shown: {"expanded_title", "pairs", "attributes", "why"}})."""
    try:
        from services.analytics.matching_core.agents.attr_ai import enabled
        switch_on = bool(enabled())
    except Exception:  # noqa: BLE001
        switch_on = False
    per_row: dict = {}
    totals = {"rows_total": len(vendor_rows), "rows_checked": 0, "rows_with_result": 0, "attributes_found": 0,
              "pairs_ai": 0, "pairs_learned": 0, "switch_on": switch_on}
    for vr in vendor_rows:
        record = (vr.get("source") or {}).get("ai_unabbrev")
        if not isinstance(record, dict):
            continue
        totals["rows_checked"] += 1
        pairs = [{"from": p.get("from"), "to": p.get("to"), "source": p.get("source") or "ai"}
                 for p in record.get("pairs") or []]
        attributes = {box: {"value": (v or {}).get("value"), "evidence": (v or {}).get("evidence")}
                      for box, v in (record.get("attributes") or {}).items() if isinstance(v, dict)}
        if not pairs and not attributes:
            continue
        totals["rows_with_result"] += 1
        totals["attributes_found"] += len(attributes)
        totals["pairs_ai"] += sum(1 for p in pairs if p["source"] == "ai")
        totals["pairs_learned"] += sum(1 for p in pairs if p["source"] == "learned")
        expanded = record.get("expanded_title") or ""
        per_row[str(row_label(vr["row_idx"]))] = {
            "expanded_title": expanded if expanded != record.get("original_title") else "",
            "pairs": pairs, "attributes": attributes, "why": record.get("why") or []}
    return totals, per_row


@router.get("/run-families/{run_id}")
def run_families(run_id: int) -> dict:
    result, candidates, vendor_rows = _load(run_id)
    lines = pair_table(result, candidates)
    counts: dict[str, Counter] = {}
    for d in lines:
        counts.setdefault(d["Family"], Counter())[d["Relation"]] += 1
    ai_totals, ai_rows = _ai_summary(vendor_rows)

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