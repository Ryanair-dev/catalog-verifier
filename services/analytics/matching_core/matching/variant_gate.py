"""
The variant gate: ONE shared check that runs on the result of ALL THREE scorers
(weighted, cascade, classifier).  It uses the unified attribute reader, so every scorer now
checks color, clothing size, units, flavor and scent the same way, on the same boxes.
"""
from __future__ import annotations

from typing import Any

from rapidfuzz import fuzz

from services.analytics.matching_core.matching.attr_extract import extract_amazon, extract_vendor
from services.analytics.matching_core.matching.flags import category_check_enabled
from services.analytics.matching_core.matching.product_line import index_for
from services.analytics.matching_core.matching.lexicon import colors_relation, get_lexicon, variant_boxes_for
from services.analytics.matching_core.matching.normalize import brands_match, normalize_upc, upc_matches
from services.analytics.matching_core.matching.variation import compare_boxes
from services.analytics.matching_core.models import MatchResult, Verdict

MISMATCH_CAP = 29.0     # below every scorer's review line, so a hard difference always rejects
SOFT_CAP = 89.0         # just under the approval line (90)

_LABELS = {
    "apparel_size_mismatch": "clothing size differs",
    "color_mismatch": "color differs",
    "count_mismatch": "the count printed on the package differs",
    "shade_mismatch": "shade differs",
    "flavor_mismatch": "flavor differs",
    "scent_mismatch": "scent differs",
    "color_name_differs": "color name differs (could be a synonym)",
    "variant_not_stated": "variant not stated",
    "brand_mismatch": "brand differs",
    "gender_mismatch": "men's / women's / kids differs",
    "category_far": "the product categories look unrelated",
    "product_line_mismatch": "the product line differs",
    "brand_differs_upc_ok": "brand differs although the UPC matches",
    "size_partial": "clothing size only partly the same (like S vs XS/S)",
}

# hard reason -> the attribute box it rests on (for the "AI value never rejects alone" rule)
_REASON_BOX = {
    "apparel_size_mismatch": ("size",),
    "gender_mismatch": ("gender",),
    "color_mismatch": ("color",),
    "count_mismatch": ("count",),
    "flavor_mismatch": ("flavor",),
    "scent_mismatch": ("scent",),
    "shade_mismatch": ("shade_code", "tone"),
}

BRAND_DISAGREE_BELOW = 70      # same line the weighted scorer uses
CATEGORY_FAR = 5.0             # category distance at or above this = "unrelated" (Medical vs Automotive is 5.4)

try:
    from services.analytics.product_categorizer import ALL_CATEGORIES, category_distance
    _CATEGORIES = {c.name: c for c in ALL_CATEGORIES}
except Exception:  # noqa: BLE001 - without the categorizer these rules simply do nothing
    ALL_CATEGORIES, category_distance, _CATEGORIES = [], None, {}


def category_gap(vendor_category: Any, amazon_category: Any) -> float | None:
    """Distance between two product categories, or None when either one is unknown."""
    a, b = _CATEGORIES.get(vendor_category or ""), _CATEGORIES.get(amazon_category or "")
    if not a or not b or category_distance is None:
        return None
    return round(float(category_distance(a, b)), 1)

_VARIANT_BOXES = ("color", "size", "flavor", "scent")


def _brand_conflict(vendor_brand: Any, amazon_brand: Any) -> bool:
    """True only when BOTH sides state a brand and they are clearly different brands.
    A missing brand is not a conflict (same rule as the weighted scorer)."""
    if not vendor_brand or not amazon_brand:
        return False
    a, b = str(vendor_brand), str(amazon_brand)
    if brands_match(a, b):
        return False
    return fuzz.ratio(a.lower(), b.lower()) < BRAND_DISAGREE_BELOW


def _rests_on_ai(reason: str, vendor: dict[str, dict], amazon: dict[str, dict]) -> bool:
    """True when the box behind a hard reason was read only from the AI-expanded title on either side."""
    return any((vendor.get(b) or {}).get("soft") or (amazon.get(b) or {}).get("soft")
               for b in _REASON_BOX.get(reason, ()))


def gate_from_boxes(vendor: dict[str, dict], amazon: dict[str, dict],
                    barcode_confirmed: bool = False, brand_conflict: bool = False,
                    categories: dict[str, str] | None = None,
                    line_conflict: str | None = None) -> dict[str, Any]:
    boxes = compare_boxes(vendor, amazon)
    vendor_category = (categories or {}).get("vendor") or ""
    amazon_category = (categories or {}).get("amazon") or ""
    # Amazon's category is the stronger signal (it uses the sales-rank category); fall back to the vendor's
    allowed = variant_boxes_for(amazon_category or vendor_category)       # None = every box can vary
    gap = category_gap(vendor_category, amazon_category)
    hard: list[str] = []
    soft: list[str] = []
    open_boxes: list[str] = []
    if brand_conflict:
        # a different brand is a hard "no"; with a confirmed UPC it is "needs a person" instead
        (soft if barcode_confirmed else hard).append(
            "brand_differs_upc_ok" if barcode_confirmed else "brand_mismatch")
    if line_conflict and not barcode_confirmed:
        # a different product LINE ("Sport" vs "Run", "Crew" vs "Mini-Crew"): see product_line.py
        hard.append("product_line_mismatch")
    if boxes.get("size") == "mismatch":
        hard.append("apparel_size_mismatch")
    elif boxes.get("size") == "partial" and not barcode_confirmed:
        soft.append("size_partial")
    if boxes.get("gender") == "mismatch":
        hard.append("gender_mismatch")
    if boxes.get("color") == "mismatch":
        # two known base colors that share nothing (black vs steel->gray) is a solid difference;
        # a color name the word library cannot place (poseidon) only goes to review
        if colors_relation(vendor["color"]["value"], amazon["color"]["value"]) == "different":
            hard.append("color_mismatch")
        else:
            soft.append("color_name_differs")
    if boxes.get("units") == "mismatch":
        hard.append("count_mismatch")
    # flavor / scent / shade only count in product categories where they can really vary
    if boxes.get("flavor") == "mismatch" and (allowed is None or "flavor" in allowed):
        hard.append("flavor_mismatch")
    if boxes.get("scent") == "mismatch" and (allowed is None or "scent" in allowed):
        hard.append("scent_mismatch")
    if ("shade" in allowed if allowed is not None else True) and (
            boxes.get("shade_code") == "mismatch" or boxes.get("tone") == "mismatch"):
        hard.append("shade_mismatch")
    # an AI-derived value (unabbreviation agent) never rejects on its own: such a difference needs a person
    ai_only = [r for r in hard if _rests_on_ai(r, vendor, amazon)]
    if ai_only:
        hard = [r for r in hard if r not in ai_only]
        soft.extend(r for r in ai_only if r not in soft)
    if not barcode_confirmed:                      # a confirmed UPC already pins the exact variant
        open_boxes = [b for b in _VARIANT_BOXES
                      if amazon.get(b) and not vendor.get(b) and (allowed is None or b in allowed)]
        if open_boxes:
            soft.append("variant_not_stated")
        if category_check_enabled() and gap is not None and gap >= CATEGORY_FAR:
            soft.append("category_far")
    return {"hard": hard, "soft": soft, "boxes": boxes, "open": open_boxes,
            "ai_only": ai_only,
            "lexicon": get_lexicon().version,
            "categories": {"vendor": vendor_category, "amazon": amazon_category, "gap": gap},
            "line": line_conflict}


def gate_for_pair(source: dict, normalized: dict, extracted: dict | None,
                  barcode_confirmed: bool = False) -> dict[str, Any] | None:
    """For the analytics pipeline: source = the vendor row dict (title, brand, raw columns),
    normalized = the Amazon item, extracted = the saved AI fields.  Never raises."""
    try:
        v, a = extract_vendor(source, extracted), extract_amazon(normalized)
        conflict = _brand_conflict(
            source.get("brand") or (extracted or {}).get("brand"),
            normalized.get("brand") or normalized.get("manufacturer"),
        )
        categories = {"vendor": v["identity"].get("category"), "amazon": a["identity"].get("category")}
        line = None
        if not barcode_confirmed:
            index = index_for(source.get("run_id"))          # the vendor file's own line vocabulary
            if index:
                line = index.conflict(source.get("title"),
                                      source.get("brand") or (extracted or {}).get("brand"),
                                      normalized.get("title"))
        return gate_from_boxes(v["fields"], a["fields"], barcode_confirmed, conflict, categories, line)
    except Exception:  # noqa: BLE001 - a failure here must never break a run
        return None


def gate_for_models(offer, candidate) -> dict[str, Any] | None:
    """For the eval script and other callers that only have Offer / Candidate objects."""
    try:
        raw = {k: v for k, v in (offer.attributes or {}).items() if isinstance(v, (str, int, float))}
        v = extract_vendor(
            {"title": offer.title or offer.raw_text or "", "brand": offer.brand or "", "raw": raw}, None
        )
        attrs = {k: (v[0] if isinstance(v, (list, tuple)) and v else v)
                 for k, v in (candidate.attributes or {}).items()}
        item = {"title": candidate.title or "", "brand": candidate.brand or "", "attributes": attrs}
        for key in ("unit_count", "unit_count_type", "number_of_items", "item_package_quantity",
                    "flavor", "scent", "style", "item_form"):
            if key in attrs:
                item[key] = attrs[key]
        a = extract_amazon(item)
        confirmed = bool(offer.upc and candidate.upc
                         and upc_matches(normalize_upc(offer.upc), normalize_upc(candidate.upc)))
        categories = {"vendor": v["identity"].get("category"), "amazon": a["identity"].get("category")}
        line = None
        index = None if confirmed else index_for("eval")
        if index:
            line = index.conflict(offer.title or offer.raw_text, offer.brand, candidate.title)
        return gate_from_boxes(v["fields"], a["fields"], confirmed,
                               _brand_conflict(offer.brand, candidate.brand), categories, line)
    except Exception:  # noqa: BLE001
        return None


def _copy(result: MatchResult, **changes: Any) -> MatchResult:
    if hasattr(result, "model_copy"):
        return result.model_copy(update=changes)
    return result.copy(update=changes)


def apply_gate(result: MatchResult, gate: dict[str, Any] | None) -> MatchResult:
    """Return the scorer's result, blocked or downgraded when the gate found a difference."""
    if not gate or not (gate["hard"] or gate["soft"]):
        return result
    reasons = list(result.reasons)
    for reason in gate["hard"] + gate["soft"]:
        if reason not in reasons:
            reasons.append(reason)
    if gate["hard"]:
        return _copy(result, confidence=min(float(result.confidence), MISMATCH_CAP),
                     verdict=Verdict.REJECTED, reasons=reasons)
    if result.verdict is Verdict.VERIFIED:
        return _copy(result, confidence=min(float(result.confidence), SOFT_CAP),
                     verdict=Verdict.REVIEW, reasons=reasons)
    return _copy(result, reasons=reasons)


def describe_gate(gate: dict[str, Any] | None) -> str:
    """One plain sentence for the explanation shown next to the score."""
    if not gate:
        return ""
    if gate["hard"]:
        parts = []
        for r in gate["hard"]:
            if r == "product_line_mismatch" and gate.get("line"):
                parts.append(f"the product line differs ({gate['line']})")
            else:
                parts.append(_LABELS[r])
        return "Variant check: " + ", ".join(parts) + " - rejected."
    if gate["soft"]:
        parts = []
        for r in gate["soft"]:
            if r == "variant_not_stated" and gate.get("open"):
                parts.append("the listing states " + ", ".join(gate["open"]) + " but the vendor row does not")
            elif r in (gate.get("ai_only") or []):
                parts.append(_LABELS[r] + " (only the AI-expanded title says so)")
            else:
                parts.append(_LABELS[r])
        return "Variant check: " + "; ".join(parts) + " - not auto-approved, needs review."
    return ""


FILE_VERSION = "2026-10-22 v9 (size partial, AI values never reject alone)"