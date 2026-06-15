"""
Scan lifecycle endpoints — the v3 entry point for the verification flow.

Every verification run is a *scan* with explicit state, so the UI can show a
Verification History list and let users come back to complete or review a
pending scan at any time.

State machine
-------------
    pending                 catalog uploaded, awaiting Amazon export
    ready                   Amazon export attached, not yet verified
    verifying               engine running
    verified_unreviewed     finished, nobody has touched the Review tab
    verified_partial        some rows reviewed / approved / rejected
    verified_complete       fully reviewed and exported at least once

Endpoints
---------
    POST   /api/scans/preview            first 10 rows + headers (no commit)
    POST   /api/scans                    create scan from catalog file + mapping
    GET    /api/scans                    list (for the History tab)
    GET    /api/scans/{id}               fetch scan (+ results if present)
    DELETE /api/scans/{id}               remove scan + cascade its rows
    POST   /api/scans/{id}/amazon        attach Amazon/Keepa export
    POST   /api/scans/{id}/verify        run the engine, store results
    POST   /api/scans/{id}/rows/{idx}    update a single result row (overrides)
    POST   /api/scans/{id}/mark-exported flip status to verified_complete
    GET    /api/template                 download the catalog .xlsx template
"""
from __future__ import annotations

import io
import json as _json
import os
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from pydantic import BaseModel

from concurrent.futures import ThreadPoolExecutor, as_completed

from services import ai_recheck, database
from services.confidence import score_row
from services.extractor import ai_extract, apply_abbreviations, rule_extract
from services.file_parser import parse_file
from services.keepa_matcher import find_candidates
from services.safety import read_upload_limited

router = APIRouter()


# --------------------------------------------------------------------------- #
# File parsing helpers
# --------------------------------------------------------------------------- #



def _apply_mapping(
    headers: list[str],
    rows: list[list[Any]],
    mapping: dict,
) -> list[dict]:
    """Turn raw rows into canonical verification records using the mapping."""
    data_start = int(mapping.get("data_start") or 0)
    rows = rows[data_start:]

    def col_index(col_name: str | None) -> int | None:
        if not col_name:
            return None
        try:
            return headers.index(col_name)
        except ValueError:
            return None

    def cell(row: list[Any], idx: int | None) -> Any:
        if idx is None or idx >= len(row):
            return None
        return row[idx]

    upc_idx = col_index(mapping.get("upc_col"))
    item_idx = col_index(mapping.get("item_id_col"))
    title_idx = col_index(mapping.get("title_col"))
    asin_idx = col_index(mapping.get("asin_col"))
    brand_col_idx = col_index(mapping.get("brand_col"))
    attr_indexes = [
        (name, col_index(name)) for name in (mapping.get("attr_cols") or [])
    ]

    brand_literal = (mapping.get("brand") or "").strip()

    out: list[dict] = []
    for row in rows:
        attrs = {}
        for col_name, idx in attr_indexes:
            if idx is not None:
                v = cell(row, idx)
                if v not in (None, ""):
                    attrs[col_name] = v
        # brand_col overrides brand_literal when present
        brand_val = None
        if brand_col_idx is not None:
            brand_val = str(cell(row, brand_col_idx) or "").strip() or None
        if not brand_val:
            brand_val = brand_literal or None
        out.append({
            "UPC/EAN": cell(row, upc_idx),
            "Item ID": cell(row, item_idx),
            "Vendor Title": cell(row, title_idx),
            "Brand": brand_val,
            "ASIN": cell(row, asin_idx),
            "_attributes": attrs,
        })
    return out


def _find_duplicates(rows: list[dict], column: str) -> set[str]:
    seen: dict[str, int] = {}
    for row in rows:
        value = row.get(column)
        if value in (None, ""):
            continue
        key = str(value).strip()
        seen[key] = seen.get(key, 0) + 1
    return {k for k, v in seen.items() if v > 1}


def _failed_signals(signals: dict) -> list[str]:
    labels = {
        "upc": "UPC", "item_id": "Item ID", "brand": "Brand",
        "title": "Title", "pack": "Pack",
    }
    return [labels[k] for k, v in signals.items()
            if isinstance(v, dict) and not v.get("matched")]


def _safe_amz_context(amz: dict | None) -> str:
    if not amz:
        return ""
    parts = []
    for k in ("Title", "item_name", "Brand", "brand", "Size", "size"):
        v = amz.get(k)
        if v:
            parts.append(f"{k}: {v}")
    return " | ".join(parts)[:800]


# Light hinting that keeps GPT-sourced additions from all landing in the same
# generic bucket. Anything that doesn't hint at a known axis falls through to
# "Product Attributes" so the UI can surface it without inventing a category.
_CAT_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("Colors",     ("color", "red", "blue", "green", "white", "black", "yellow")),
    ("Sizes",      ("size", "small", "medium", "large", "xl", "inch", "cm", "mm")),
    ("UOMs",       ("unit", "each", "pack", "case", "box", "roll", "dozen", "pair")),
    ("Sterility",  ("steril", "non-steril", "non sterile", "non-sterile", "aseptic")),
    ("Materials", ("cotton", "rayon", "latex", "nitrile", "vinyl", "poly", "gauze",
                    "non-woven", "woven")),
    ("Forms",      ("liquid", "powder", "gel", "spray", "cream", "wipe", "lotion",
                    "foam", "tablet", "capsule", "stick", "bar", "bandage",
                    "dressing", "sponge")),
    ("Scents",     ("scent", "scented", "fragrance", "unscented", "lavender",
                    "citrus", "mint")),
    ("Flavors",    ("flavor", "cherry", "grape", "orange", "lemon", "vanilla",
                    "chocolate")),
    ("Packaging", ("bag", "bottle", "jar", "tube", "carton", "wrap", "pouch",
                    "packaging")),
]


def _guess_abbr_category(full: str) -> str:
    """Pick a library category for a GPT-proposed expansion.

    We only look at the expanded form (``full``) because the abbreviation
    itself carries no signal. Falls back to "Product Attributes" which is the
    generic bucket already used for AI-learned product types and forms.
    """
    s = (full or "").lower()
    for cat, needles in _CAT_HINTS:
        if any(n in s for n in needles):
            return cat
    return "Product Attributes"


# --------------------------------------------------------------------------- #
# Preview — used by the 4-step import wizard
# --------------------------------------------------------------------------- #

@router.post("/scans/preview")
async def scan_preview(catalog_file: UploadFile = File(...)) -> dict:
    """Return the first 10 rows + detected headers for the import wizard."""
    try:
        data = await read_upload_limited(catalog_file)
        headers, rows = parse_file(catalog_file.filename or "", data)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Could not parse file: {exc}")
    if not headers:
        raise HTTPException(status_code=400, detail="File appears to be empty")

    preview = []
    for i, row in enumerate(rows[:10]):
        preview.append({
            "row_number": i + 2,   # 1-indexed, accounting for header row at 1
            "cells": [str(v) if v is not None else "" for v in row]
                     + [""] * max(0, len(headers) - len(row)),
        })

    return {
        "filename": catalog_file.filename,
        "size": len(data),
        "headers": headers,
        "preview": preview,
        "total_rows": len(rows),
    }


# --------------------------------------------------------------------------- #
# Scan CRUD
# --------------------------------------------------------------------------- #

@router.post("/scans")
async def create_scan(
    catalog_file: UploadFile = File(...),
    mapping: str = Form(...),
    name: str = Form(""),
    marketplace: str = Form("US"),
    condition: str = Form("New"),
    ai_mode: str = Form("false"),
    match_from_keepa: str = Form("false"),
    match_methods: str = Form("[]"),
) -> dict:
    try:
        mapping_obj = _json.loads(mapping)
    except _json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Malformed mapping JSON")

    try:
        methods_list = _json.loads(match_methods)
        if not isinstance(methods_list, list):
            methods_list = []
    except _json.JSONDecodeError:
        methods_list = []

    data = await read_upload_limited(catalog_file)
    try:
        headers, rows = parse_file(catalog_file.filename or "", data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Could not parse file: {exc}")

    match_mode = str(match_from_keepa).lower() in ("1", "true", "yes", "on")

    # ASIN column is only required in normal mode; match-from-Keepa doesn't need it
    if match_mode:
        required_cols = ("upc_col", "item_id_col", "title_col")
    else:
        required_cols = ("upc_col", "item_id_col", "title_col", "asin_col")
    missing = [k for k in required_cols if not mapping_obj.get(k)]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Mapping missing required columns: {', '.join(missing)}",
        )

    mapped_rows = _apply_mapping(headers, rows, mapping_obj)
    if not mapped_rows:
        raise HTTPException(status_code=400, detail="No data rows after applying start offset")

    scan_name = name.strip() or Path(catalog_file.filename or "Scan").stem
    ai_on = str(ai_mode).lower() in ("1", "true", "yes", "on")

    scan_id = database.create_scan(
        name=scan_name,
        mapping=mapping_obj,
        marketplace=marketplace or "US",
        condition=condition or "New",
        catalog_filename=catalog_file.filename,
        catalog_count=len(mapped_rows),
        ai_mode=ai_on,
        match_from_keepa=match_mode,
        match_methods=methods_list if match_mode else [],
    )
    database.save_scan_catalog_rows(scan_id, mapped_rows)

    return {"scan": database.get_scan(scan_id)}


@router.get("/scans")
async def list_scans() -> dict:
    return {"scans": database.list_scans()}


@router.get("/scans/{scan_id}")
async def get_scan(scan_id: int, include_results: bool = True) -> dict:
    scan = database.get_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")
    payload = {"scan": scan}
    if include_results:
        payload["results"] = database.load_scan_results(scan_id)
        payload["thresholds"] = database.get_thresholds()
    return payload


@router.delete("/scans/{scan_id}")
async def delete_scan_endpoint(scan_id: int) -> dict:
    deleted = database.delete_scan(scan_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Scan not found")
    return {"deleted": True}


# --------------------------------------------------------------------------- #
# Amazon attach
# --------------------------------------------------------------------------- #

@router.post("/scans/{scan_id}/amazon")
async def attach_amazon(
    scan_id: int,
    amazon_file: UploadFile = File(...),
    amazon_source: str = Form(...),
) -> dict:
    if amazon_source not in ("keepa", "amazon"):
        raise HTTPException(
            status_code=400, detail="amazon_source must be 'keepa' or 'amazon'",
        )
    scan = database.get_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")

    data = await read_upload_limited(amazon_file)
    try:
        headers, rows = parse_file(amazon_file.filename or "", data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Could not parse file: {exc}")

    body = [dict(zip(headers, r + [None] * (len(headers) - len(r)))) for r in rows]

    saved = database.save_scan_amazon_rows(scan_id, body)
    database.update_scan(
        scan_id,
        amazon_filename=amazon_file.filename,
        amazon_source=amazon_source,
        amazon_count=saved,
        status="ready",
    )
    # Keep the legacy global pool populated for cross-scan caching.
    try:
        target_table = "keepa_imports" if amazon_source == "keepa" else "amazon_imports"
        database.save_imports(target_table, body)
    except Exception:  # noqa: BLE001
        pass

    return {"scan": database.get_scan(scan_id), "amazon_count": saved}


# --------------------------------------------------------------------------- #
# Run verification
# --------------------------------------------------------------------------- #

@router.post("/scans/{scan_id}/verify")
async def run_verify(scan_id: int) -> dict:
    scan = database.get_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")
    if scan["status"] == "pending":
        raise HTTPException(status_code=400, detail="Amazon data not yet attached")

    database.update_scan(scan_id, status="verifying")

    abbr_list = database.flat_library()
    thresholds = database.get_thresholds()
    catalog_rows = database.load_scan_catalog_rows(scan_id)
    amazon_index = database.load_scan_amazon_rows(scan_id)
    use_ai = bool(scan.get("ai_mode"))

    dupes = _find_duplicates(catalog_rows, "Item ID")
    # Load pair-manager sets once — avoids per-row DB queries.
    blacklist_set = database.load_blacklist_set()
    verified_set  = database.load_verified_set()
    # Build a token set from the library for O(1) membership checks instead of
    # per-token DB queries inside the AI learning loop.
    known_tokens: set[str] = {e["abbr"].lower() for e in abbr_list}
    ai_added: list[dict] = []

    # ------------------------------------------------------------------ #
    # AI extraction (optional) — parallelised across rows with a thread
    # pool so the event loop isn't blocked for each OpenAI call.
    # ------------------------------------------------------------------ #
    ai_results: dict[int, dict] = {}
    if use_ai:
        def _ai_one(idx: int, row: dict, amz: dict | None) -> tuple[int, dict]:
            return idx, ai_extract(
                row.get("Vendor Title") or "",
                abbreviations=abbr_list,
                extra_context=_safe_amz_context(amz),
            )

        workers = min(6, max(1, len(catalog_rows)))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futs = {
                pool.submit(
                    _ai_one, i, r,
                    amazon_index.get(str(r.get("ASIN") or "").strip().upper()),
                ): i
                for i, r in enumerate(catalog_rows)
            }
            for fut in as_completed(futs):
                try:
                    idx, res = fut.result()
                    ai_results[idx] = res
                except Exception:  # noqa: BLE001
                    pass

    results = []
    for i, row in enumerate(catalog_rows):
        asin = str(row.get("ASIN") or "").strip().upper()
        upc = str(row.get("UPC/EAN") or "").strip()
        amz = amazon_index.get(asin)
        score = score_row(row, amz, abbr_list, thresholds=thresholds)

        raw_title = row.get("Vendor Title") or ""
        title_expanded = apply_abbreviations(raw_title, abbr_list)

        try:
            attrs = rule_extract(raw_title, abbr_list)
            if use_ai:
                ai_result = ai_results.get(i) or {}
                if isinstance(ai_result, dict) and "error" not in ai_result:
                    attrs.update({k: v for k, v in ai_result.items() if v})
                    ai_expanded = ai_result.get("expanded_title")
                    if isinstance(ai_expanded, str) and ai_expanded.strip() \
                            and ai_expanded.strip().lower() != title_expanded.strip().lower():
                        title_expanded = ai_expanded.strip()
                    # Auto-learn unknown product types / forms — use in-memory
                    # set to skip DB round-trips for already-known tokens.
                    for key in ("form", "product_type"):
                        val = ai_result.get(key)
                        if isinstance(val, str) and val.strip() \
                                and val.strip().lower() not in known_tokens:
                            created, entry = database.add_library_entry(
                                "Product Attributes", val.strip(), val.strip(),
                                added_by="ai",
                            )
                            if created and entry:
                                known_tokens.add(val.strip().lower())
                                ai_added.append({
                                    "abbr": entry["abbr"], "full": entry["full"],
                                    "category": "Product Attributes",
                                })
                    for na in ai_result.get("new_abbreviations") or []:
                        if not isinstance(na, dict):
                            continue
                        abbr = str(na.get("abbr") or "").strip()
                        full = str(na.get("full") or "").strip()
                        if not abbr or not full or abbr.lower() == full.lower():
                            continue
                        if abbr.lower() in known_tokens:
                            continue
                        category = _guess_abbr_category(full)
                        created, entry = database.add_library_entry(
                            category, abbr, full, added_by="ai",
                        )
                        if created and entry:
                            known_tokens.add(abbr.lower())
                            ai_added.append({
                                "abbr": entry["abbr"], "full": entry["full"],
                                "category": category,
                            })
            if upc and asin:
                database.cache_attributes(
                    upc, asin, attrs,
                    source="ai+rule" if use_ai else "rule",
                )
        except Exception:  # noqa: BLE001
            pass

        amz_title = ""
        if amz:
            for k in ("Title", "item_name", "Item Name", "Product Title"):
                if amz.get(k):
                    amz_title = str(amz[k]); break

        title_expanded_out = (
            title_expanded.strip()
            if title_expanded and title_expanded.strip().lower() != raw_title.strip().lower()
            else None
        )

        # Pair manager overrides — applied after scoring.
        pair_verdict = score["verdict"]
        pair_status  = ""
        is_blacklisted = upc and asin and (upc, asin) in blacklist_set
        is_verified    = upc and asin and (upc, asin) in verified_set
        if is_blacklisted:
            pair_verdict = "Not Approved"
            pair_status  = "Pair Blacklisted"
        elif is_verified and score["confidence"] >= 40 and score["verdict"] != "Not Approved":
            pair_verdict = "Approved"
            pair_status  = "Pair Verified"

        results.append({
            "UPC": row.get("UPC/EAN"),
            "ItemID": row.get("Item ID"),
            "Title": row.get("Vendor Title"),
            "TitleExpanded": title_expanded_out,
            "AmzTitle": amz_title or None,
            "Brand": row.get("Brand"),
            "ASIN": row.get("ASIN"),
            "Confidence": score["confidence"],
            "Verdict": pair_verdict,
            "original_verdict": score["verdict"],
            "review_status": pair_status,
            "signals": score["signals"],
            "amz_pack": score["amz_pack"],
            "duplicate": str(row.get("Item ID") or "").strip() in dupes,
            "blacklisted": bool(is_blacklisted),
            "notes": score["notes"],
            "_attributes": row.get("_attributes") or {},
            "ai_suggestion": None,
            "ai_reason": None,
            "ai_model": None,
            "ai_checked_at": None,
        })

    database.save_scan_results(scan_id, results)
    stats = database.recompute_scan_stats(scan_id)
    database.update_scan(scan_id, status="verified_unreviewed")

    return {
        "scan": database.get_scan(scan_id),
        "results": database.load_scan_results(scan_id),
        "thresholds": thresholds,
        "duplicate_item_ids": sorted(dupes),
        "ai_added": ai_added,
        "stats": stats,
    }


# --------------------------------------------------------------------------- #
# Match-from-Keepa pipeline (separate from normal verify)
# --------------------------------------------------------------------------- #

@router.post("/scans/{scan_id}/match")
async def run_match(scan_id: int) -> dict:
    """Run the match-from-Keepa pipeline for scans created with match_from_keepa=True."""
    scan = database.get_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")
    if not scan.get("match_from_keepa"):
        raise HTTPException(status_code=400, detail="Scan was not created in match-from-Keepa mode")
    if scan["status"] == "pending":
        raise HTTPException(status_code=400, detail="Keepa data not yet attached")

    database.update_scan(scan_id, status="verifying")

    abbr_list  = database.flat_library()
    thresholds = database.get_thresholds()
    catalog_rows = database.load_scan_catalog_rows(scan_id)
    keepa_index  = database.load_scan_amazon_rows(scan_id)   # asin → row
    methods      = scan.get("match_methods") or ["upc", "item_id", "title"]

    candidates = find_candidates(
        catalog_rows=catalog_rows,
        keepa_rows=keepa_index,
        methods=methods,
        abbr_list=abbr_list,
        thresholds=thresholds,
    )

    database.save_scan_candidates(scan_id, candidates)

    # Populate scan_results with best candidate per row for stats/export compat.
    best_by_row: dict[int, dict] = {}
    for c in candidates:
        idx = c["row_idx"]
        if idx not in best_by_row or c["Confidence"] > best_by_row[idx]["Confidence"]:
            best_by_row[idx] = c

    results = [best_by_row[i] for i in sorted(best_by_row)]
    database.save_scan_results(scan_id, results)
    stats = database.recompute_scan_stats(scan_id)
    database.update_scan(scan_id, status="verified_unreviewed")

    return {
        "scan":       database.get_scan(scan_id),
        "candidates": database.load_scan_candidates(scan_id),
        "thresholds": thresholds,
        "stats":      stats,
    }


# --------------------------------------------------------------------------- #
# Candidate-level verdict (match-from-Keepa mode)
# --------------------------------------------------------------------------- #

class CandidateVerdict(BaseModel):
    verdict: str        # "Approved" | "Not Approved"
    review_status: str = ""


@router.post("/scans/{scan_id}/candidates/{cand_id}/verdict")
async def set_candidate_verdict(
    scan_id: int, cand_id: int, body: CandidateVerdict,
) -> dict:
    """
    Approve or discard a single candidate.

    Approving a candidate:
      - Sets its verdict to Approved and review_status to 'Approved'
      - Discards all other candidates for the same row
      - Prevents approving the same ASIN for a different row in this scan
      - Updates scan_results[row_idx] to this candidate (for export compat)

    Discarding a candidate:
      - Sets its verdict to Not Approved and review_status to 'Discarded'
    """
    scan = database.get_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")

    cand = database.get_scan_candidate(cand_id)
    if not cand or cand["scan_id"] != scan_id:
        raise HTTPException(status_code=404, detail="Candidate not found")

    verdict = body.verdict.strip()
    if verdict not in ("Approved", "Not Approved"):
        raise HTTPException(status_code=400, detail="verdict must be Approved or Not Approved")

    row_idx = cand["row_idx"]
    asin    = cand["asin"]

    if verdict == "Approved":
        # Enforce: one ASIN per scan — check if already approved elsewhere
        already_row = database.get_asin_approved_for_scan(scan_id, asin)
        if already_row is not None and already_row != row_idx:
            raise HTTPException(
                status_code=409,
                detail=f"ASIN {asin} is already approved for row {already_row} in this scan",
            )

        # Update this candidate
        updated = dict(cand)
        updated["Verdict"] = "Approved"
        updated["review_status"] = "Approved"
        database.update_scan_candidate(cand_id, "Approved", "Approved", updated)

        # Discard all other candidates for this row with a single targeted UPDATE
        with database._LOCK, database._connect() as conn:
            conn.execute(
                "UPDATE scan_candidates SET verdict='Not Approved', review_status='Discarded' "
                "WHERE scan_id=? AND row_idx=? AND id!=? AND review_status!='Discarded'",
                (scan_id, row_idx, cand_id),
            )

        # Sync scan_results row so export and stats reflect the approval
        result_data = dict(updated)
        database.update_scan_result_row(scan_id, row_idx, "Approved", "Approved", result_data)

    else:
        # Discard
        updated = dict(cand)
        updated["Verdict"] = "Not Approved"
        updated["review_status"] = body.review_status or "Discarded"
        database.update_scan_candidate(
            cand_id, "Not Approved", updated["review_status"], updated,
        )
        row_candidates = [
            c for c in database.load_scan_candidates(scan_id)
            if c.get("row_idx") == row_idx and c.get("review_status") != "Discarded"
        ]
        replacement = max(
            row_candidates,
            key=lambda c: float(c.get("Confidence", c.get("confidence", 0)) or 0),
            default=updated,
        )
        replacement_verdict = replacement.get("Verdict") or replacement.get("verdict") or "Not Approved"
        replacement_status = replacement.get("review_status") or ""
        database.update_scan_result_row(
            scan_id, row_idx, replacement_verdict, replacement_status, replacement,
        )

    stats = database.recompute_scan_stats(scan_id)
    database.update_scan(scan_id, status="verified_partial")
    return {"ok": True, "stats": stats}


@router.get("/scans/{scan_id}/candidates")
async def list_candidates(scan_id: int) -> dict:
    scan = database.get_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")
    catalog_rows = database.load_scan_catalog_rows(scan_id)
    candidates   = database.load_scan_candidates(scan_id)
    return {
        "scan":        scan,
        "candidates":  candidates,
        "catalog_rows": catalog_rows,
    }


# --------------------------------------------------------------------------- #
# Row-level override
# --------------------------------------------------------------------------- #

class RowOverride(BaseModel):
    verdict: str
    review_status: str = ""
    data: dict


@router.post("/scans/{scan_id}/rows/{row_idx}")
async def update_row(scan_id: int, row_idx: int, body: RowOverride) -> dict:
    scan = database.get_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")
    database.update_scan_result_row(
        scan_id, row_idx, body.verdict, body.review_status, body.data,
    )
    stats = database.recompute_scan_stats(scan_id)
    # Status flip: fully reviewed → verified_complete (after export); partial
    # otherwise. 'verified_complete' is only set via mark-exported.
    if scan["status"].startswith("verified"):
        new_status = (
            "verified_partial"
            if stats["reviewed"] < (stats["verified"] + stats["review"] + stats["not_approved"])
            else "verified_complete"
        )
        database.update_scan(scan_id, status=new_status)
    return {"ok": True, "stats": stats}


@router.post("/scans/{scan_id}/mark-exported")
async def mark_exported(scan_id: int) -> dict:
    scan = database.get_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")
    # Single atomic update — status + exported_at set together to avoid race condition
    with database._LOCK, database._connect() as conn:
        conn.execute(
            "UPDATE scans SET status='verified_complete', "
            "exported_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=?",
            (scan_id,),
        )
    return {"scan": database.get_scan(scan_id)}


# --------------------------------------------------------------------------- #
# AI re-check — second-pass OpenAI verdict suggestions
# --------------------------------------------------------------------------- #
#
# Two endpoints:
#   POST /scans/{id}/ai-estimate → cost + duration preview, no API calls made.
#   POST /scans/{id}/ai-recheck  → actually calls OpenAI for each matching row.
#
# The re-check updates the row's JSON blob with ai_suggestion / ai_reason /
# ai_model / ai_checked_at fields. It never changes the Verdict column — the
# user has to accept each suggestion manually (POST /scans/{id}/rows/{idx}).
#
# As a bonus the pass harvests unfamiliar product_type / form / variant tokens
# into the abbreviation library under "Product Attributes" — the same channel
# the existing AI-mode extractor uses.


class AIEstimateBody(BaseModel):
    buckets: list[str]
    model: str = ai_recheck.DEFAULT_MODEL


@router.post("/scans/{scan_id}/ai-estimate")
async def ai_recheck_estimate(scan_id: int, body: AIEstimateBody) -> dict:
    scan = database.get_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")
    if body.model not in ai_recheck.ALLOWED_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported model: {body.model}",
        )

    results = database.load_scan_results(scan_id)
    want = {b.lower() for b in (body.buckets or [])}
    # Treat "Approved" and "Verified" as the same bucket — the UI renamed one
    # to the other, but the engine still writes "Verified" and older scans
    # may have rows labelled either way.
    if "approved" in want:
        want.add("verified")

    targeted = [r for r in results if (r.get("Verdict") or "").lower() in want]
    by_bucket: dict[str, int] = {}
    for r in targeted:
        key = (r.get("Verdict") or "").strip() or "Unknown"
        by_bucket[key] = by_bucket.get(key, 0) + 1

    est = ai_recheck.estimate_cost(len(targeted), body.model)
    est["by_bucket"] = by_bucket
    est["openai_configured"] = bool(os.getenv("OPENAI_API_KEY"))
    return est


class AIRecheckBody(BaseModel):
    buckets: list[str]
    model: str = ai_recheck.DEFAULT_MODEL
    max_cost_usd: float | None = None   # hard cap; refuse if estimate exceeds


@router.post("/scans/{scan_id}/ai-recheck")
async def ai_recheck_run(scan_id: int, body: AIRecheckBody) -> dict:
    scan = database.get_scan(scan_id)
    if not scan:
        raise HTTPException(status_code=404, detail="Scan not found")
    if not str(scan["status"]).startswith("verified"):
        raise HTTPException(
            status_code=400,
            detail="Scan must be verified before running AI re-check",
        )
    if body.model not in ai_recheck.ALLOWED_MODELS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported model: {body.model}",
        )

    client = ai_recheck.make_client()
    if client is None:
        raise HTTPException(
            status_code=400,
            detail="OPENAI_API_KEY is not configured on the server.",
        )

    results = database.load_scan_results(scan_id)
    want = {b.lower() for b in (body.buckets or [])}
    if "approved" in want:
        want.add("verified")
    target_idxs = [
        i for i, r in enumerate(results)
        if (r.get("Verdict") or "").lower() in want
    ]

    pre_est = ai_recheck.estimate_cost(len(target_idxs), body.model)
    if body.max_cost_usd is not None and pre_est["cost_usd_est"] > body.max_cost_usd:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Estimated cost ${pre_est['cost_usd_est']:.4f} exceeds cap "
                f"${body.max_cost_usd:.4f}. Raise the cap or narrow the bucket "
                f"selection."
            ),
        )

    amazon_index = database.load_scan_amazon_rows(scan_id)
    abbr_list = database.flat_library()
    known_tokens = {a["abbr"].lower() for a in abbr_list if a.get("abbr")}
    tokens_added: list[dict] = []
    failures: list[dict] = []
    tot_in = tot_out = 0
    updated_rows = 0
    from datetime import datetime, timezone
    from concurrent.futures import ThreadPoolExecutor, as_completed
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Parallelize OpenAI calls — each row is an independent HTTP call, so
    # running them serially means 40 rows × ~850ms = ~34s of wall time. A
    # pool of 6 workers keeps us well under OpenAI's tier-1 rate limit while
    # cutting that to ~6-9s.
    def _one(idx: int) -> tuple[int, dict]:
        row = results[idx]
        amz = amazon_index.get(str(row.get("ASIN") or "").strip().upper())
        return idx, ai_recheck.recheck_row(row, amz, client, body.model)

    max_workers = min(6, max(1, len(target_idxs)))
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = [pool.submit(_one, idx) for idx in target_idxs]
        for fut in as_completed(futures):
            idx, out = fut.result()
            if not out.get("ok"):
                failures.append({"row_idx": idx, "error": out.get("error")})
                continue

            tot_in  += out.get("input_tokens",  0)
            tot_out += out.get("output_tokens", 0)
            updated_rows += 1

            row = results[idx]
            row["ai_suggestion"] = out["suggested_verdict"]
            row["ai_reason"]     = out["reason"]
            row["ai_model"]      = body.model
            row["ai_checked_at"] = now_iso

            # Harvest novel attribute tokens into the library.
            attrs = out.get("attributes") or {}
            for key in ("product_type", "form"):
                val = attrs.get(key)
                if isinstance(val, str) and val.strip():
                    token = val.strip()
                    if token.lower() not in known_tokens and not database.has_library_entry(token):
                        created, entry = database.add_library_entry(
                            "Product Attributes", token, token, added_by="ai",
                        )
                        if created and entry:
                            known_tokens.add(token.lower())
                            tokens_added.append({
                                "abbr": entry["abbr"], "full": entry["full"],
                                "category": "Product Attributes",
                            })

    database.save_scan_results(scan_id, results)
    cost = ai_recheck.actual_cost(tot_in, tot_out, body.model)

    return {
        "rechecked": len(target_idxs) - len(failures),
        "failed": failures,
        "cost_usd": cost,
        "input_tokens":  tot_in,
        "output_tokens": tot_out,
        "tokens_added":  tokens_added,
        "model": body.model,
        "results": database.load_scan_results(scan_id),
    }


# --------------------------------------------------------------------------- #
# Template download
# --------------------------------------------------------------------------- #

@router.get("/template")
async def download_template() -> StreamingResponse:
    """Return a pre-formatted catalog template .xlsx."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Catalog"

    headers = [
        "Vendor Title", "Brand", "UPC/EAN", "Item ID", "ASIN",
        "Size", "Color", "Scent", "Pack Count",
    ]
    ws.append(headers)

    # Header styling — teal bar, bold white text.
    header_font = Font(bold=True, color="FFFFFF", size=11)
    header_fill = PatternFill("solid", fgColor="0D9488")
    center = Alignment(horizontal="left", vertical="center")
    for col_idx, _ in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = center

    # Example row so users can see how a filled-in row looks.
    example = [
        "Dawn Ultra Dishwashing Liquid Original Scent 19.4 fl oz",
        "Dawn",
        "037000988120",
        "DN-19-ORIG",
        "B00V6D5RGY",
        "19.4 fl oz",
        "Blue",
        "Original",
        "1",
    ]
    ws.append(example)
    example_fill = PatternFill("solid", fgColor="F0FDFA")
    for col_idx in range(1, len(headers) + 1):
        cell = ws.cell(row=2, column=col_idx)
        cell.fill = example_fill
        cell.font = Font(italic=True, color="0F766E")

    # Clear out the example a bit lower so the user can paste their real data.
    widths = [52, 16, 18, 18, 14, 14, 14, 16, 12]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[chr(64 + i)].width = w

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type=(
            "application/vnd.openxmlformats-officedocument"
            ".spreadsheetml.sheet"
        ),
        headers={"Content-Disposition":
                 'attachment; filename="catalog-template.xlsx"'},
    )
