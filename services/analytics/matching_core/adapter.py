"""Adapt Catalog Verifier's normalized records to the matcher scoring core."""
from __future__ import annotations

import logging
import threading
from typing import Any

from services.analytics.matching_core.models import Candidate, MatchResult, Offer, Verdict
from services.analytics.matching_core.matching.cascade import score as score_cascade
from services.analytics.matching_core.matching.attribute_compare import (
    Cmp,
    compare_attributes,
)
from services.analytics.matching_core.matching.classifier import (
    explain as explain_classifier,
    score as score_classifier,
)
from services.analytics.matching_core.matching.normalize import (
    brands_match,
    normalize_upc,
    upc_matches,
)
from services.analytics.matching_core.matching.scorer import score as score_weighted

_log = logging.getLogger(__name__)
_logged_scorers: set[str] = set()
_log_lock = threading.Lock()

SCORING_METHODS = frozenset({"weighted", "cascade", "classifier"})
_SCORERS = {
    "weighted": score_weighted,
    "cascade": score_cascade,
    "classifier": score_classifier,
}

_OFFER_FIELDS = {
    "upc": ("UPC/EAN", "upc", "UPC", "EAN", "GTIN"),
    "mpn": ("Item ID", "itemid", "mpn", "MPN", "Part Number", "part_number"),
    "title": ("Vendor Title", "title", "Title", "Item Name", "item_name"),
    "brand": ("Brand", "brand"),
}
_CANDIDATE_FIELDS = {
    "title": ("Title", "title", "Item Name", "item_name", "Product Title"),
    "brand": ("Brand", "brand", "Manufacturer", "manufacturer"),
    "mpn": (
        "Product Codes: PartNumber", "Part Number", "part_number",
        "Model", "model_number", "MPN", "mpn",
    ),
    "upc": (
        "Product Codes: UPC", "Product Codes: EAN", "Product Codes: GTIN",
        "upc", "UPC", "ean", "EAN", "gtin", "GTIN", "identifier_value",
    ),
}
_ATTRIBUTE_FIELDS = {
    "color": ("Color", "color"),
    "size": ("Size", "size"),
    "material": ("Material", "material"),
    "scent": ("Scent", "scent", "Fragrance", "fragrance"),
    "flavor": ("Flavor", "flavor"),
}


def _first_value(record: dict[str, Any], fields: tuple[str, ...]) -> str:
    for field in fields:
        value = record.get(field)
        if isinstance(value, (list, tuple)):
            value = next((part for part in value if part not in (None, "")), "")
        if isinstance(value, dict):
            value = value.get("value") or ""
        text = str(value or "").strip()
        if text:
            return text.replace(";", "|").replace(",", "|").split("|", 1)[0].strip()
    return ""


def _mapped_attributes(
    record: dict[str, Any], aliases: dict[str, tuple[str, ...]],
) -> dict[str, str]:
    found: dict[str, str] = {}
    existing = record.get("attributes") or record.get("_attributes")
    if isinstance(existing, dict):
        for key, value in existing.items():
            if value not in (None, ""):
                found[str(key).strip().lower()] = str(value).strip()
    for canonical, fields in aliases.items():
        value = _first_value(record, fields)
        if value:
            found[canonical] = value
    return found


def build_export_pair(
    source: dict[str, Any],
    amazon: dict[str, Any],
    *,
    sources: list[str] | None = None,
) -> tuple[Offer, Candidate]:
    """Map Verification/Keepa export rows into the matcher's input models."""
    offer = Offer(
        offer_id=str(source.get("row_idx", "")),
        title=_first_value(source, _OFFER_FIELDS["title"]) or None,
        raw_text=_first_value(source, _OFFER_FIELDS["title"]) or None,
        brand=_first_value(source, _OFFER_FIELDS["brand"]) or None,
        upc=_first_value(source, _OFFER_FIELDS["upc"]) or None,
        mpn=_first_value(source, _OFFER_FIELDS["mpn"]) or None,
        attributes=_mapped_attributes(source, {}),
    )
    candidate = Candidate(
        asin=(
            _first_value(amazon, ("ASIN", "asin", "Asin"))
            or _first_value(source, ("ASIN", "asin", "Asin"))
        ),
        title=_first_value(amazon, _CANDIDATE_FIELDS["title"]) or None,
        brand=_first_value(amazon, _CANDIDATE_FIELDS["brand"]) or None,
        upc=_first_value(amazon, _CANDIDATE_FIELDS["upc"]) or None,
        mpn=_first_value(amazon, _CANDIDATE_FIELDS["mpn"]) or None,
        attributes={
            key: [value]
            for key, value in _mapped_attributes(amazon, _ATTRIBUTE_FIELDS).items()
        },
        bullet_points=[
            str(value).strip()
            for value in (amazon.get("bullet_point") or amazon.get("bullet_points") or [])
            if value not in (None, "")
        ] if isinstance(amazon.get("bullet_point") or amazon.get("bullet_points") or [], (list, tuple)) else [],
        description=_first_value(amazon, ("Description", "description", "product_description")) or None,
        found_via=",".join(sources or []),
    )
    return offer, candidate


def score_export_pair(
    source: dict[str, Any],
    amazon: dict[str, Any],
    *,
    method: str = "weighted",
    mode: str = "cpg",
    sources: list[str] | None = None,
) -> tuple[dict[str, Any], str]:
    """Score old-format catalog/Amazon rows with the selected matcher scorer."""
    if method not in SCORING_METHODS:
        raise ValueError(f"Unsupported scoring method: {method}")
    if not (
        _first_value(amazon, ("ASIN", "asin", "Asin"))
        or _first_value(source, ("ASIN", "asin", "Asin"))
    ):
        return {
            "confidence_score": 0.0,
            "scoring_method": method,
            "signals": {},
            "reasons": ["No candidate ASIN"],
            "upc_match": False,
            "brand_confirmed": False,
            "effective_pack": None,
        }, "not_approved"
    offer, candidate = build_export_pair(source, amazon, sources=sources)
    result = score_models(offer, candidate, method=method, mode=mode)
    return result_to_scores(result, method, offer, candidate), (
        "not_approved" if result.verdict is Verdict.REJECTED else result.verdict.value
    )


def score_pair(
    source: dict[str, Any],
    normalized: dict[str, Any],
    *,
    method: str = "weighted",
    mode: str = "cpg",
    extracted: dict[str, Any] | None = None,
    sources: list[str] | None = None,
) -> tuple[dict[str, Any], str]:
    """Score a retrieved Amazon item without changing Catalog Verifier retrieval."""
    offer, candidate = build_pair(
        source, normalized, extracted=extracted, sources=sources,
    )
    result = score_models(offer, candidate, method=method, mode=mode)
    scores = result_to_scores(result, method, offer, candidate)
    verdict = result.verdict.value
    return scores, "not_approved" if verdict == "rejected" else verdict


def build_pair(
    source: dict[str, Any],
    normalized: dict[str, Any],
    *,
    extracted: dict[str, Any] | None = None,
    sources: list[str] | None = None,
) -> tuple[Offer, Candidate]:
    extracted = extracted or {}
    offer = Offer(
        offer_id=str(source.get("row_idx", "")),
        title=str(source.get("title") or ""),
        raw_text=str(source.get("title") or ""),
        brand=str(source.get("brand") or extracted.get("brand") or "") or None,
        upc=str(source.get("upc") or "") or None,
        mpn=str(source.get("mpn") or source.get("itemid") or "") or None,
    )
    candidate_attrs = dict(normalized.get("attributes") or {})
    for pack_key in ("item_package_quantity", "number_of_items"):
        pack_value = normalized.get(pack_key)
        if pack_value not in (None, "", 0, "0"):
            candidate_attrs[pack_key] = pack_value
    candidate = Candidate(
        asin=str(normalized.get("asin") or ""),
        title=str(normalized.get("title") or ""),
        brand=str(normalized.get("brand") or normalized.get("manufacturer") or "") or None,
        upc=str(normalized.get("upc") or normalized.get("ean") or normalized.get("gtin") or "") or None,
        mpn=str(normalized.get("mpn") or "") or None,
        attributes={
            str(key): [str(value)]
            for key, value in candidate_attrs.items()
            if value is not None and str(value).strip()
        },
        bullet_points=[
            str(point) for point in (normalized.get("bullet_points") or [])
            if point is not None and str(point).strip()
        ],
        description=str(normalized.get("description") or "") or None,
        found_via=",".join(sources or []),
    )
    return offer, candidate
 


def score_models(offer: Offer, candidate: Candidate, *, method: str, mode: str = "cpg") -> MatchResult:
    if method not in SCORING_METHODS:
        raise ValueError(f"Unsupported scoring method: {method}")
    with _log_lock:
        if method not in _logged_scorers:
            _logged_scorers.add(method)
            _log.info(
                "[matching-core] scorer invoked: method=%s mode=%s",
                method,
                mode,
            )
    scorer = _SCORERS[method]
    return scorer(offer, candidate, mode=mode) if method == "cascade" else scorer(offer, candidate)


def result_to_scores(
    result: MatchResult, method: str, offer: Offer, candidate: Candidate,
) -> dict[str, Any]:
    signals = dict(result.signals)
    reasons = list(result.reasons)
    confidence = float(result.confidence)
    mismatches = [
        reason.removesuffix("_mismatch").replace("_", " ")
        for reason in reasons
        if reason.endswith("_mismatch")
    ]
    explanation: dict[str, Any]
    if method == "weighted":
        if signals.get("upc", 0) > 0:
            summary = "Exact UPC match set the score to 100 before mismatch checks."
        else:
            components = (
                f"MPN {signals.get('mpn', 0):.1f} + "
                f"brand {signals.get('brand', 0):.1f} + "
                f"title {signals.get('title', 0):.1f}"
            )
            raw_total = sum(float(signals.get(key, 0) or 0) for key in ("mpn", "brand", "title"))
            summary = (
                f"Weighted sum: {components} = {raw_total:.1f}, "
                f"capped at {min(raw_total, 100.0):.1f}."
            )
        if mismatches:
            summary += f" Hard mismatch cap applied ({', '.join(mismatches)}); final score {confidence:.1f}."
        explanation = {
            "summary": summary,
            "factors": [
                f"UPC: {signals.get('upc', 0):.1f}/100",
                f"MPN: {signals.get('mpn', 0):.1f}/90",
                f"Brand: {signals.get('brand', 0):.1f}/10",
                f"Title: {signals.get('title', 0):.1f}/50",
            ] + ([f"Hard mismatch: {', '.join(mismatches)}"] if mismatches else []),
        }
    elif method == "cascade":
        rule = next(
            (reason.removeprefix("rule:") for reason in reasons if reason.startswith("rule:")),
            "fallback_fuzzy",
        )
        blocked_by_mismatch = rule.endswith("+blocked_by_mismatch")
        if blocked_by_mismatch:
            rule = rule.removesuffix("+blocked_by_mismatch")
        rule_labels = {
            "1_brand+upc": "Rule 1: brand + UPC",
            "1_brand+mpn": "Rule 1: brand + MPN",
            "2_upc+1attr": "Rule 2: UPC + at least one matching attribute",
            "2_mpn+1attr": "Rule 2: MPN + at least one matching attribute",
            "3_brand+all_attrs": "Rule 3: brand + all comparable attributes",
            "4_all_attrs+fuzzy70": "Rule 4: all comparable attributes + title similarity",
            "5_fuzzy72": "Rule 5: title similarity",
            "6_all_attrs": "Rule 6: all comparable attributes",
            "7_mpn_in_amz_text": "Rule 7: MPN found in Amazon text",
            "7_mpn_in_amz_text+1attr": "Rule 7: MPN in Amazon text + matching attribute",
            "fallback_fuzzy": "Fallback: half of fuzzy title similarity (capped at 60)",
        }
        attributes = compare_attributes(offer, candidate)
        attr_labels = {"volume": "Volume", "color": "Color", "size": "Apparel size", "count": "Bundle count"}
        attr_factors = [
            f"{attr_labels[name]}: {value.value}"
            for name, value in attributes.items()
        ]
        summary = f"{rule_labels.get(rule, rule)} produced {confidence:.1f}."
        if blocked_by_mismatch:
            summary += f" A hard attribute mismatch capped the score at {confidence:.1f}."
        explanation = {
            "summary": summary,
            "factors": [
                f"Brand agreement: {'yes' if signals.get('brand') else 'no'}",
                f"UPC match: {'yes' if signals.get('upc') else 'no'}",
                f"MPN match: {'yes' if signals.get('mpn') else 'no'}",
                f"Title similarity: {signals.get('fuzzy', 0):.1f}%",
                *attr_factors,
            ],
        }
    else:
        classifier_explanation = explain_classifier(offer, candidate)
        probability = float(signals.get("match_probability", confidence / 100))
        summary = (
            f"Classifier predicted {probability * 100:.1f}% match probability "
            f"({result.verdict.value}). This is a model probability, not a weighted point sum."
        )
        explanation = {
            "summary": summary,
            "factors": classifier_explanation["input_factors"],
            "global_importance": classifier_explanation["global_importance"],
        }

    scores: dict[str, Any] = {
        "confidence_score": confidence,
        "scoring_method": method,
        "scoring_verdict": "not_approved" if result.verdict is Verdict.REJECTED else result.verdict.value,
        "signals": signals,
        "reasons": reasons,
        "explanation": explanation,
        "upc_match": bool(
            offer.upc and candidate.upc
            and upc_matches(normalize_upc(offer.upc), normalize_upc(candidate.upc))
        ),
        "brand_confirmed": brands_match(offer.brand, candidate.brand),
        "effective_pack": None,
    }
    for key in (
        "size_mismatch", "apparel_size_mismatch", "linear_size_mismatch",
        "count_mismatch", "color_mismatch", "brand_mismatch",
    ):
        scores[key] = any(reason == key for reason in reasons)
    scores["size_mismatch"] = scores["size_mismatch"] or any(
        reason == "volume_mismatch" for reason in reasons
    )
    return scores
