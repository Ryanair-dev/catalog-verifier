"""
Match-from-Keepa engine.

Given a set of catalog rows (no ASINs) and a Keepa export (acting as the
local Amazon database), find up to MAX_PER_ROW candidate ASINs for each
catalog row using the selected match methods:

  upc      — exact digit match on UPC/EAN fields
  item_id  — fuzzy match on model number / part number fields
  title    — token-set-ratio fuzzy match on product title

Each candidate is scored using the same confidence.score_row() engine used
by the normal verify flow.  Results are returned as a flat list sorted by
(row_idx ASC, confidence DESC) so callers can store them directly.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from rapidfuzz import fuzz

from .confidence import score_row

MAX_PER_ROW = 8
TITLE_MIN_SCORE = 40   # minimum token_set_ratio to include a title candidate


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _digits(v: Any) -> str:
    return re.sub(r"\D+", "", str(v or ""))


def _norm(v: Any) -> str:
    return str(v or "").strip().lower()


def _keepa_upcs(row: dict) -> list[str]:
    candidates = [
        row.get("Product Codes: UPC"),
        row.get("Product Codes: EAN"),
        row.get("Product Codes: GTIN"),
        row.get("upc"), row.get("ean"),
    ]
    out = []
    for v in candidates:
        d = _digits(v)
        if d:
            out.append(d)
            out.append(d.lstrip("0"))
            if len(d) == 12:
                out.append("0" + d)
            elif len(d) == 13 and d.startswith("0"):
                out.append(d[1:])
    return list(dict.fromkeys(filter(None, out)))


def _keepa_mpns(row: dict) -> list[str]:
    candidates = [
        row.get("Product Codes: PartNumber"),
        row.get("Model"),
        row.get("model_number"),
        row.get("part_number"),
    ]
    out = []
    for v in candidates:
        n = _norm(v)
        if n:
            out.append(n)
            out.append(n.replace("-", "").replace(" ", ""))
    return list(dict.fromkeys(filter(None, out)))


def _keepa_title(row: dict) -> str:
    for k in ("Title", "item_name", "Item Name", "Product Title"):
        v = row.get(k)
        if v:
            return _norm(v)
    return ""


def _keepa_asin(row: dict) -> str:
    for k in ("ASIN", "asin", "Asin"):
        v = row.get(k)
        if v:
            return str(v).strip().upper()
    return ""


# ---------------------------------------------------------------------------
# Index builders
# ---------------------------------------------------------------------------

def _build_upc_index(keepa_rows: dict[str, dict]) -> dict[str, list[str]]:
    """UPC/EAN digit string → [asin, ...]"""
    idx: dict[str, list[str]] = defaultdict(list)
    for asin, row in keepa_rows.items():
        for d in _keepa_upcs(row):
            if d and asin not in idx[d]:
                idx[d].append(asin)
    return idx


def _build_mpn_index(keepa_rows: dict[str, dict]) -> dict[str, list[str]]:
    """Normalised MPN string → [asin, ...]"""
    idx: dict[str, list[str]] = defaultdict(list)
    for asin, row in keepa_rows.items():
        for m in _keepa_mpns(row):
            if m and asin not in idx[m]:
                idx[m].append(asin)
    return idx


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def find_candidates(
    catalog_rows: list[dict],
    keepa_rows: dict[str, dict],   # asin → keepa row dict
    methods: list[str],
    abbr_list: list[dict],
    thresholds: dict | None = None,
    max_per_row: int = MAX_PER_ROW,
) -> list[dict]:
    """
    Return a flat list of candidate dicts ready for save_scan_candidates().

    Each dict has the same shape as a scan_results payload plus:
      row_idx      — 0-based catalog row index
      match_method — 'upc' | 'item_id' | 'title' (highest-priority method that found the ASIN)
    """
    methods_set = {m.lower() for m in (methods or [])}
    use_upc    = "upc"     in methods_set
    use_itemid = "item_id" in methods_set
    use_title  = "title"   in methods_set

    upc_idx = _build_upc_index(keepa_rows)   if use_upc    else {}
    mpn_idx = _build_mpn_index(keepa_rows)   if use_itemid else {}

    # Pre-build title list once for title search
    title_list: list[tuple[str, str]] = []   # [(asin, normalised_title), ...]
    if use_title:
        for asin, row in keepa_rows.items():
            t = _keepa_title(row)
            if t:
                title_list.append((asin, t))

    all_candidates: list[dict] = []

    for row_idx, cat_row in enumerate(catalog_rows):
        cat_upc   = _digits(cat_row.get("UPC/EAN") or "")
        cat_itemid = _norm(cat_row.get("Item ID") or "")
        cat_title  = _norm(cat_row.get("Vendor Title") or "")

        # asin → best match_method priority (upc > item_id > title)
        found: dict[str, str] = {}

        # --- UPC exact match ---
        if use_upc and cat_upc:
            for variant in [cat_upc, cat_upc.lstrip("0")]:
                for asin in upc_idx.get(variant, []):
                    if asin not in found:
                        found[asin] = "upc"

        # --- Item ID / MPN fuzzy match ---
        if use_itemid and cat_itemid:
            cat_norm_nohyphen = cat_itemid.replace("-", "").replace(" ", "")
            for mpn_key, asins in mpn_idx.items():
                # exact on normalised key
                if mpn_key == cat_itemid or mpn_key == cat_norm_nohyphen:
                    for asin in asins:
                        if asin not in found:
                            found[asin] = "item_id"
                    continue
                # fuzzy — only bother if strings are similar length
                ratio = fuzz.ratio(cat_itemid, mpn_key)
                if ratio >= 80:
                    for asin in asins:
                        if asin not in found:
                            found[asin] = "item_id"

        # --- Title fuzzy match (top N) ---
        if use_title and cat_title:
            scored_titles: list[tuple[float, str]] = []
            for asin, amz_title in title_list:
                score = fuzz.token_set_ratio(cat_title, amz_title)
                if score >= TITLE_MIN_SCORE:
                    scored_titles.append((score, asin))
            scored_titles.sort(reverse=True)
            for _, asin in scored_titles[:max_per_row]:
                if asin not in found:
                    found[asin] = "title"

        if not found:
            # No candidates — placeholder Not Approved row
            all_candidates.append(_make_no_match(row_idx, cat_row))
            continue

        # Score each candidate and keep top N
        scored: list[tuple[float, str, str]] = []
        for asin, method in found.items():
            keepa_row = keepa_rows.get(asin)
            if not keepa_row:
                continue
            # Inject ASIN into catalog row so score_row can apply pair logic
            enriched = dict(cat_row)
            enriched["ASIN"] = asin
            result = score_row(enriched, keepa_row, abbr_list, thresholds=thresholds)
            scored.append((result["confidence"], asin, method, result, keepa_row))

        if not scored:
            all_candidates.append(_make_no_match(row_idx, cat_row))
            continue

        scored.sort(key=lambda x: x[0], reverse=True)

        for conf, asin, method, score_result, keepa_row in scored[:max_per_row]:
            amz_title = ""
            for k in ("Title", "item_name", "Item Name", "Product Title"):
                if keepa_row.get(k):
                    amz_title = str(keepa_row[k]); break

            all_candidates.append({
                "row_idx":      row_idx,
                "match_method": method,
                # catalog fields
                "UPC":    cat_row.get("UPC/EAN"),
                "ItemID": cat_row.get("Item ID"),
                "Title":  cat_row.get("Vendor Title"),
                "Brand":  cat_row.get("Brand"),
                "ASIN":   asin,
                # scoring
                "Confidence":       score_result["confidence"],
                "Verdict":          score_result["verdict"],
                "original_verdict": score_result["verdict"],
                "review_status":    "",
                "signals":          score_result["signals"],
                "amz_pack":         score_result["amz_pack"],
                "notes":            score_result["notes"],
                "AmzTitle":         amz_title or None,
                "TitleExpanded":    None,
                "duplicate":        False,
                "blacklisted":      False,
                "_attributes":      cat_row.get("_attributes") or {},
                "ai_suggestion":    None,
                "ai_reason":        None,
                "ai_model":         None,
                "ai_checked_at":    None,
            })

    return all_candidates


def _make_no_match(row_idx: int, cat_row: dict) -> dict:
    return {
        "row_idx":      row_idx,
        "match_method": "none",
        "UPC":    cat_row.get("UPC/EAN"),
        "ItemID": cat_row.get("Item ID"),
        "Title":  cat_row.get("Vendor Title"),
        "Brand":  cat_row.get("Brand"),
        "ASIN":   None,
        "Confidence":       0.0,
        "Verdict":          "Not Approved",
        "original_verdict": "Not Approved",
        "review_status":    "No match found",
        "signals":          {},
        "amz_pack":         None,
        "notes":            "No matching ASIN found in Keepa export",
        "AmzTitle":         None,
        "TitleExpanded":    None,
        "duplicate":        False,
        "blacklisted":      False,
        "_attributes":      cat_row.get("_attributes") or {},
        "ai_suggestion":    None,
        "ai_reason":        None,
        "ai_model":         None,
        "ai_checked_at":    None,
    }
