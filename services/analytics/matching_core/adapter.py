"""Adapt Catalog Verifier's normalized records to the matcher scoring core."""
from __future__ import annotations

import logging
import threading
from typing import Any

from services.analytics.matching_core.models import Candidate, MatchResult, Offer, Verdict
from services.analytics.matching_core.matching.cascade import score as score_cascade
from services.analytics.matching_core.matching.classifier import score as score_classifier
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
    "brand": ("Brand", "brand"),
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
        brand=str(extracted.get("brand") or source.get("brand") or "") or None,
        upc=str(source.get("upc") or "") or None,
        mpn=str(source.get("mpn") or source.get("itemid") or "") or None,
    )
    candidate_attrs = normalized.get("attributes") or {}
    candidate = Candidate(
        asin=str(normalized.get("asin") or ""),
        title=str(normalized.get("title") or ""),
        brand=str(normalized.get("brand") or "") or None,
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
    scores: dict[str, Any] = {
        "confidence_score": float(result.confidence),
        "scoring_method": method,
        "signals": signals,
        "reasons": reasons,
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
