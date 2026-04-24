"""
Confidence scoring engine.

Given a catalog row, its matched Amazon row, and extracted attributes,
produce a 0–100 confidence score plus per-signal breakdowns and a verdict.
"""
from __future__ import annotations

import re
from typing import Any

from rapidfuzz import fuzz

from .extractor import amz_extract, rule_extract

# Signal weights — must sum to 100.
# UPC is the gold standard for CPG catalogs; brand matching is unreliable when
# catalog lists a parent company (e.g. "Unilever") rather than sub-brands
# (Degree, Dove, AXE), so brand weight is kept minimal.
WEIGHTS = {
    "upc": 55,
    "item_id": 10,
    "brand": 5,
    "title": 25,
    "pack": 5,
}

# Verdict thresholds — defaults, overridable via services.database.get_thresholds()
# or by passing explicit ``thresholds`` through the scoring API.
# With UPC weight=55: a UPC match alone yields 55 pts, so Approved threshold=65
# means UPC match + any reasonable title (~40%+) → Approved.
# Items without a UPC match top out at ~45 pts → Not Approved.
VERIFIED_MIN = 65
REVIEW_MIN = 40

VERDICT_VERIFIED = "Approved"
VERDICT_REVIEW = "Review"
VERDICT_NOT_APPROVED = "Not Approved"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _norm(value: Any) -> str:
    """Lowercase + strip, tolerate None/numeric."""
    if value is None:
        return ""
    return str(value).strip().lower()


def _digits_only(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or ""))


def _split_ids(value: Any) -> list[str]:
    """Split a pipe/comma/semicolon delimited id field into pieces."""
    if value is None:
        return []
    raw = str(value)
    parts = re.split(r"[|,;\s]+", raw)
    return [p.strip().lower() for p in parts if p.strip()]


# --------------------------------------------------------------------------- #
# Per-signal scorers
# --------------------------------------------------------------------------- #

def score_upc(catalog_upc: Any, amz_upcs: list[Any]) -> dict:
    cat_norm = _digits_only(catalog_upc)
    if not cat_norm:
        return {"score": 0, "detail": "No catalog UPC", "matched": False, "no_data": False}

    # Check whether Amazon actually has any UPC data at all.
    amz_has_data = any(_digits_only(v) for v in amz_upcs if v is not None)
    if not amz_has_data:
        return {"score": 0, "detail": "No UPC in Amazon record", "matched": False, "no_data": True}

    for value in amz_upcs:
        for piece in _split_ids(value):
            piece_digits = _digits_only(piece)
            if not piece_digits:
                continue
            # Tolerate 12 vs 13 digit differences (EAN vs UPC-A).
            if (cat_norm == piece_digits
                    or cat_norm.lstrip("0") == piece_digits.lstrip("0")
                    or cat_norm[-12:] == piece_digits[-12:]):
                return {"score": 100, "detail": f"Match: {piece_digits}", "matched": True, "no_data": False}
    return {"score": 0, "detail": "UPC mismatch", "matched": False, "no_data": False}


def score_item_id(catalog_item_id: Any, amz_candidates: list[Any]) -> dict:
    cat = _norm(catalog_item_id)
    if not cat:
        return {"score": 0, "detail": "No catalog Item ID", "matched": False}

    best_score = 0
    best_detail = "No Amazon identifier"
    for candidate in amz_candidates:
        for piece in _split_ids(candidate):
            if not piece:
                continue
            if piece == cat:
                return {"score": 100, "detail": f"Exact: {piece}", "matched": True}
            # Allow minor suffix variants e.g. 40065 vs 40065-100.
            if cat.startswith(piece) or piece.startswith(cat):
                ratio = fuzz.ratio(cat, piece)
                if ratio > best_score:
                    best_score = max(ratio, 80)
                    best_detail = f"Variant: {piece}"
                continue
            ratio = fuzz.ratio(cat, piece)
            if ratio > best_score:
                best_score = ratio
                best_detail = f"Fuzzy: {piece} ({ratio}%)"

    return {
        "score": best_score,
        "detail": best_detail,
        "matched": best_score >= 80,
    }


def score_brand(catalog_brand: Any, amz_brand: Any, amz_manufacturer: Any) -> dict:
    cat = _norm(catalog_brand)
    if not cat:
        return {"score": 0, "detail": "No catalog brand", "matched": False}

    candidates = [_norm(amz_brand), _norm(amz_manufacturer)]
    best = 0
    best_label = ""
    for value in candidates:
        if not value:
            continue
        if value == cat:
            return {"score": 100, "detail": f"Exact: {value}", "matched": True}
        ratio = fuzz.token_set_ratio(cat, value)
        if ratio > best:
            best = ratio
            best_label = value
    return {
        "score": best,
        "detail": f"Fuzzy: {best_label} ({best}%)" if best_label else "No match",
        "matched": best >= 80,
    }


def score_title(
    catalog_title: Any,
    amz_row: dict | None,
    abbreviations: list[dict],
) -> dict:
    cat_raw = _norm(catalog_title)
    if not cat_raw:
        return {"score": 0, "detail": "Missing title", "matched": False}
    if not amz_row:
        return {"score": 0, "detail": "Missing title", "matched": False}

    amz_title = amz_row.get("Title") or amz_row.get("item_name") or ""
    if not _norm(amz_title):
        return {"score": 0, "detail": "Missing title", "matched": False}

    cat_attr = rule_extract(cat_raw, abbreviations)

    # Use the full Amazon row — combined title + all feature/bullet fields,
    # with dedicated attribute columns (Color, Scent, Size…) overriding
    # anything extracted from text.
    amz_attr = amz_extract(amz_row, abbreviations)

    # Base fuzzy similarity: catalog normalised title vs combined Amazon text.
    base = fuzz.token_set_ratio(
        cat_attr["normalised_title"], amz_attr["normalised_title"]
    )

    penalties = 0
    bonuses = 0
    notes: list[str] = []

    # Product type mismatch penalty.
    if cat_attr["product_type"] and amz_attr["product_type"]:
        if cat_attr["product_type"] != amz_attr["product_type"]:
            t_ratio = fuzz.partial_ratio(
                cat_attr["product_type"], amz_attr["product_type"]
            )
            if t_ratio < 70:
                penalties += 20
                notes.append(
                    f"type {cat_attr['product_type']} vs {amz_attr['product_type']}"
                )

    # Size comparison.
    if cat_attr["size"] and amz_attr["size"]:
        same_unit = cat_attr["size"]["unit"] == amz_attr["size"]["unit"]
        same_value = abs(cat_attr["size"]["value"] - amz_attr["size"]["value"]) < 0.05
        if same_unit and same_value:
            bonuses += 10
        else:
            penalties += 10
            notes.append(
                f"size {cat_attr['size']['raw']} vs {amz_attr['size']['raw']}"
            )

    # Pack count comparison.
    cat_pack = cat_attr.get("pack_count")
    amz_pack = amz_attr.get("pack_count")
    if cat_pack and amz_pack:
        if cat_pack == amz_pack:
            bonuses += 20
        else:
            penalties += 10
            notes.append(f"pack {cat_pack} vs {amz_pack}")

    # Variant comparison (scent, color, flavor).
    for key in ("scent", "color", "flavor"):
        cv = cat_attr["variant"].get(key)
        av = amz_attr["variant"].get(key)
        if cv and av:
            if cv == av:
                bonuses += 10
            else:
                penalties += 8
                notes.append(f"{key} {cv} vs {av}")

    score = min(100, max(0, base - penalties + bonuses))
    detail = f"Similarity {base}%"
    if bonuses:
        detail += f" +{bonuses}pts attrs"
    if notes:
        detail += " — " + "; ".join(notes)

    return {
        "score": score,
        "detail": detail,
        "matched": score >= 80,
        "cat_attrs": cat_attr,
        "amz_attrs": amz_attr,
    }


def score_pack(cat_attrs: dict, amz_row: dict) -> dict:
    """Compare catalog implied pack vs the Amazon pack/quantity fields.

    If Amazon pack is a clean multiple of catalog pack, we record the
    multiplier so the UI can show it in an ``Amz Pack`` column.
    """
    cat_pack = cat_attrs.get("pack_count") if cat_attrs else None

    amz_pack_candidates = []
    for key in (
        "Unit Details: Unit Value",
        "Number of Items",
        "item_package_quantity",
        "unit_count",
        "number_of_items",
        "Package: Quantity",
    ):
        if amz_row.get(key) not in (None, ""):
            try:
                amz_pack_candidates.append(int(float(amz_row[key])))
            except (TypeError, ValueError):
                continue

    amz_pack = amz_pack_candidates[0] if amz_pack_candidates else None

    if cat_pack is None and amz_pack is None:
        return {"score": 70, "detail": "No pack info", "matched": False,
                "amz_pack": None, "multiple": None}

    if cat_pack is None:
        return {"score": 100, "detail": f"Amazon pack {amz_pack}, catalog pack not specified",
                "matched": True, "amz_pack": amz_pack, "multiple": None}

    if amz_pack is None:
        return {"score": 60, "detail": f"Catalog pack {cat_pack}, Amazon unknown",
                "matched": False, "amz_pack": None, "multiple": None}

    if cat_pack == amz_pack:
        return {"score": 100, "detail": f"Exact pack {cat_pack}", "matched": True,
                "amz_pack": amz_pack, "multiple": 1}

    # Multiple-of relationship — still informative, partial credit.
    if cat_pack > 0 and amz_pack % cat_pack == 0:
        multiple = amz_pack // cat_pack
        return {"score": 75,
                "detail": f"Amazon pack is {multiple}× catalog ({cat_pack} → {amz_pack})",
                "matched": True, "amz_pack": amz_pack, "multiple": multiple}
    if amz_pack > 0 and cat_pack % amz_pack == 0:
        multiple = cat_pack // amz_pack
        return {"score": 60,
                "detail": f"Catalog pack is {multiple}× Amazon",
                "matched": False, "amz_pack": amz_pack, "multiple": multiple}

    return {"score": 20,
            "detail": f"Pack mismatch ({cat_pack} vs {amz_pack})",
            "matched": False, "amz_pack": amz_pack, "multiple": None}


# --------------------------------------------------------------------------- #
# Aggregate scoring
# --------------------------------------------------------------------------- #

def verdict_for(score: float, thresholds: dict | None = None) -> str:
    verified_min = (thresholds or {}).get("verified", VERIFIED_MIN)
    review_min = (thresholds or {}).get("review", REVIEW_MIN)
    if score >= verified_min:
        return VERDICT_VERIFIED
    if score >= review_min:
        return VERDICT_REVIEW
    return VERDICT_NOT_APPROVED


def score_row(
    catalog_row: dict,
    amazon_row: dict | None,
    abbreviations: list[dict],
    thresholds: dict | None = None,
) -> dict:
    """Score a single catalog row against its matched Amazon record."""
    if not amazon_row:
        return {
            "confidence": 0.0,
            "verdict": VERDICT_NOT_APPROVED,
            "signals": {
                "upc": {"score": 0, "detail": "No Amazon row found", "matched": False},
                "item_id": {"score": 0, "detail": "No Amazon row", "matched": False},
                "brand": {"score": 0, "detail": "No Amazon row", "matched": False},
                "title": {"score": 0, "detail": "No Amazon row", "matched": False},
                "pack": {"score": 0, "detail": "No Amazon row", "matched": False,
                         "amz_pack": None, "multiple": None},
            },
            "amz_pack": None,
            "notes": "ASIN not found in Amazon data file",
        }

    upc_candidates = [
        amazon_row.get("Product Codes: UPC"),
        amazon_row.get("Product Codes: EAN"),
        amazon_row.get("Product Codes: GTIN"),
        amazon_row.get("upc"),
        amazon_row.get("ean"),
        amazon_row.get("identifier_value"),
    ]
    item_candidates = [
        amazon_row.get("Product Codes: PartNumber"),
        amazon_row.get("Model"),
        amazon_row.get("model_number"),
        amazon_row.get("part_number"),
    ]
    upc = score_upc(catalog_row.get("UPC/EAN"), upc_candidates)
    item = score_item_id(catalog_row.get("Item ID"), item_candidates)
    brand = score_brand(
        catalog_row.get("Brand"),
        amazon_row.get("Brand") or amazon_row.get("brand"),
        amazon_row.get("Manufacturer") or amazon_row.get("manufacturer"),
    )
    title = score_title(catalog_row.get("Vendor Title"), amazon_row, abbreviations)
    pack = score_pack(title.get("cat_attrs"), amazon_row)

    # When Amazon has no UPC data at all (e.g. Keepa Product Viewer export),
    # redistribute the UPC weight to title so title similarity can still approve.
    # Also use a lower approval threshold (50) since the max reachable score is ~83.
    if upc.get("no_data"):
        w_upc, w_title = 0, WEIGHTS["upc"] + WEIGHTS["title"]
        effective_thresholds = {
            "verified": min((thresholds or {}).get("verified", VERIFIED_MIN), 50),
            "review": (thresholds or {}).get("review", REVIEW_MIN),
        }
    else:
        w_upc, w_title = WEIGHTS["upc"], WEIGHTS["title"]
        effective_thresholds = thresholds

    weighted = (
        upc["score"] * w_upc
        + item["score"] * WEIGHTS["item_id"]
        + brand["score"] * WEIGHTS["brand"]
        + title["score"] * w_title
        + pack["score"] * WEIGHTS["pack"]
    ) / 100.0

    confidence = round(weighted, 1)

    # When the catalog supplies an explicit ASIN that matched an Amazon record
    # and the title similarity is ≥ 40%, the item identity is confirmed.
    # Covers two cases:
    #   1. UPC mismatch — typically a multi-pack bundle with a different bundle UPC.
    #   2. no_data (Keepa export lacks UPC) — identity confirmed via ASIN + title.
    catalog_asin = str(catalog_row.get("ASIN") or "").strip()
    if catalog_asin and title["score"] >= 40:
        if not upc["matched"]:  # covers both no_data=True and plain mismatch
            asin_floor = (effective_thresholds or {}).get("verified", VERIFIED_MIN)
            confidence = max(confidence, float(asin_floor))

    notes_bits = []
    if upc.get("no_data"):
        notes_bits.append("No UPC in Amazon record — scored on title")
    elif not upc["matched"]:
        notes_bits.append("UPC mismatch — reviewed")
    if pack.get("multiple") and pack["multiple"] != 1:
        notes_bits.append(f"Amazon pack {pack['multiple']}× catalog")

    return {
        "confidence": confidence,
        "verdict": verdict_for(confidence, effective_thresholds),
        "signals": {
            "upc": upc,
            "item_id": item,
            "brand": brand,
            "title": {k: v for k, v in title.items() if k not in ("cat_attrs", "amz_attrs")},
            "pack": pack,
        },
        "amz_pack": pack.get("amz_pack"),
        "notes": "; ".join(notes_bits) if notes_bits else "",
    }
