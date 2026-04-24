"""
Confidence scoring — ported verbatim (logic-wise) from the reference
project `AmazonAsinResearch1/app/services/matcher.py`.

The original scorer consumed SQLAlchemy ORM objects. This port takes
plain dicts so it can run against the rows we already store in the
catalog-verifier's SQLite (`analytics_catalog_rows.data_json`) and
against freshly-parsed Amazon SP-API items.

Weights (unchanged from the reference):

    UPC exact match    -> automatic 100
    Brand              -> 70 points
      * substring in Amazon title / brand field / description = full 70
      * otherwise fuzzy match (>= 70%) across those three fields
      * additional keywords can contribute up to 70 toward the brand score
    Title similarity   -> 20 points
      * brand + MPN both present in Amazon title or description = auto 20
      * otherwise best-of-4 candidate token_set_ratio against Amazon title
    MPN (dash-norm)    -> 10 points

Totals are capped at 100. This is intentionally the same envelope the
reference project's UI shows so the numbers are directly comparable.

Source / Amazon dict shapes
---------------------------
`source` dict keys (all optional except you probably want title/brand
or UPC):
    upc, mpn, itemid, title, brand, manufacturer

`amazon` dict keys (produced by runner.normalize_amazon_item):
    asin, title, brand, manufacturer, mpn, upc, ean,
    description, bullet_points, item_package_quantity, number_of_items,
    attributes  (dict; optional — color/size/material if available)
"""
from __future__ import annotations

from typing import Any, Iterable

try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover - rapidfuzz is in requirements.txt
    fuzz = None  # type: ignore


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


def _s(value: Any) -> str:
    """Coerce to a trimmed string. Handles None and non-string types."""
    if value is None:
        return ""
    return str(value).strip()


def _lower(value: Any) -> str:
    return _s(value).lower()


def _ratio(a: str, b: str) -> int:
    if not a or not b or fuzz is None:
        return 0
    return int(fuzz.ratio(a, b))


def _partial_ratio(a: str, b: str) -> int:
    if not a or not b or fuzz is None:
        return 0
    return int(fuzz.partial_ratio(a, b))


def _token_set_ratio(a: str, b: str) -> int:
    if not a or not b or fuzz is None:
        return 0
    return int(fuzz.token_set_ratio(a, b))


# --------------------------------------------------------------------------- #
# calculate_confidence — 1:1 port of the reference scorer
# --------------------------------------------------------------------------- #


def calculate_confidence(
    source: dict,
    amazon: dict,
    additional_keywords: Iterable[str] | None = None,
) -> dict:
    """
    Return a breakdown of the confidence score for one source-to-Amazon pair.

    Shape:
        {
          "confidence_score": 0-100,
          "brand_score":      0-70,
          "product_type_similarity": 0-20,
          "mpn_score":        0-10,
          "upc_match":        bool,
        }
    """
    src_brand = _s(source.get("brand") or source.get("manufacturer"))
    amz_brand = _s(amazon.get("brand") or amazon.get("manufacturer"))
    amz_title = _lower(amazon.get("title"))
    amz_desc = _lower(amazon.get("description"))

    # Flatten bullet points into the description haystack — the reference
    # project only had description_text but Amazon often keeps the useful
    # signal in bullets.
    bullets = amazon.get("bullet_points") or []
    if bullets:
        amz_desc = (amz_desc + " " + " ".join(_lower(b) for b in bullets)).strip()

    # ----- UPC exact match → auto 100 --------------------------------------
    src_upc = _s(source.get("upc"))
    amz_upc = _s(amazon.get("upc"))
    if src_upc and amz_upc and src_upc == amz_upc:
        return {
            "confidence_score": 100.0,
            "brand_score": 0.0,
            "product_type_similarity": 0.0,
            "mpn_score": 0.0,
            "upc_match": True,
        }

    # ----- Brand (70 pts) --------------------------------------------------
    brand_score = 0.0
    if src_brand:
        src_brand_lower = src_brand.lower()
        brand_in_title = bool(amz_title) and src_brand_lower in amz_title
        brand_in_field = bool(amz_brand) and src_brand_lower in amz_brand.lower()
        brand_in_desc  = bool(amz_desc)  and src_brand_lower in amz_desc
        if brand_in_title or brand_in_field or brand_in_desc:
            brand_score = 70.0
        else:
            # Fuzzy fallback — require >= 70% similarity on best of 3 fields.
            best = max(
                _ratio(src_brand_lower, amz_brand.lower()) if amz_brand else 0,
                _partial_ratio(src_brand_lower, amz_title) if amz_title else 0,
                _partial_ratio(src_brand_lower, amz_desc) if amz_desc else 0,
            )
            if best >= 70:
                brand_score = (best / 100.0) * 70.0

    # ----- Additional-keyword boost (up to 70 toward brand_score) ----------
    if brand_score < 70 and additional_keywords:
        for kw in additional_keywords:
            kw_lower = (kw or "").strip().lower()
            if not kw_lower:
                continue
            kw_in_title = bool(amz_title) and kw_lower in amz_title
            kw_in_field = bool(amz_brand) and kw_lower in amz_brand.lower()
            kw_in_desc  = bool(amz_desc)  and kw_lower in amz_desc
            if kw_in_title or kw_in_field or kw_in_desc:
                brand_score = max(brand_score, 70.0)
                break
            best = max(
                _ratio(kw_lower, amz_brand.lower()) if amz_brand else 0,
                _partial_ratio(kw_lower, amz_title) if amz_title else 0,
                _partial_ratio(kw_lower, amz_desc) if amz_desc else 0,
            )
            if best >= 70:
                brand_score = max(brand_score, (best / 100.0) * 70.0)

    # ----- Title similarity (20 pts) ---------------------------------------
    src_title = _s(source.get("title"))
    src_brand_str = src_brand
    src_mpn_str = _s(source.get("mpn") or source.get("itemid"))

    product_type_sim = 0.0
    if src_brand_str and src_mpn_str:
        sb = src_brand_str.lower()
        sm = src_mpn_str.lower()
        # brand+MPN both present in title OR description = auto 20
        if (amz_title and sb in amz_title and sm in amz_title) or (
            amz_desc and sb in amz_desc and sm in amz_desc
        ):
            product_type_sim = 20.0

    if product_type_sim == 0.0 and src_title and amz_title:
        candidates = [
            src_title,
            f"{src_brand_str} {src_title}" if src_brand_str else None,
            f"{src_title} {src_mpn_str}" if src_mpn_str else None,
            f"{src_brand_str} {src_title} {src_mpn_str}"
            if src_brand_str and src_mpn_str
            else None,
        ]
        ratios = [_token_set_ratio(c.lower(), amz_title) for c in candidates if c]
        best_ratio = max(ratios) if ratios else 0
        product_type_sim = (best_ratio / 100.0) * 20.0

    # ----- MPN (10 pts, dash-normalised) -----------------------------------
    mpn_score = 0.0
    src_mpn = _s(source.get("mpn") or source.get("itemid"))
    amz_mpn = _s(amazon.get("mpn"))
    if src_mpn and amz_mpn:
        s = src_mpn.lower().replace("-", "")
        a = amz_mpn.lower().replace("-", "")
        ratio = _ratio(s, a)
        mpn_score = (ratio / 100.0) * 10.0

    total = min(brand_score + product_type_sim + mpn_score, 100.0)

    return {
        "confidence_score": round(total, 1),
        "brand_score": round(brand_score, 1),
        "product_type_similarity": round(product_type_sim, 1),
        "mpn_score": round(mpn_score, 1),
        "upc_match": False,
    }


# --------------------------------------------------------------------------- #
# Higher-level helper
# --------------------------------------------------------------------------- #


def score_amazon_item(
    source: dict,
    amazon: dict,
    additional_keywords: Iterable[str] | None = None,
) -> dict:
    """
    Return the breakdown dict plus a string verdict for the UI:
        confidence >= 90  -> "verified"
        confidence >= 35  -> "review"
        otherwise          -> "not_approved"

    The thresholds match the defaults in the catalog-verifier `settings`
    table (keys `threshold_verified` / `threshold_review`); callers that
    want different thresholds should recompute the verdict themselves.
    """
    scores = calculate_confidence(source, amazon, additional_keywords)
    conf = scores["confidence_score"]
    if conf >= 90 or scores["upc_match"]:
        verdict = "verified"
    elif conf >= 35:
        verdict = "review"
    else:
        verdict = "not_approved"
    scores["verdict"] = verdict
    return scores
