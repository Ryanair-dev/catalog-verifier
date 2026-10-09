"""Deterministic matcher: score a candidate against an offer.
"""
from __future__ import annotations

from rapidfuzz import fuzz

from services.analytics.matching_core.matching.attributes import (
    apparel_size_mismatch,
    linear_size_mismatch,
    size_mismatch,
)
from services.analytics.matching_core.matching.normalize import (
    base_mpn,
    brands_match,
    normalize_upc,
    title_similarity,
    upc_matches,
)
from services.analytics.matching_core.models import Candidate, MatchResult, Offer, Verdict

VERIFIED_MIN = 90.0
REVIEW_MIN = 55.0
MISMATCH_CAP = 29.0  # below REVIEW_MIN -> a hard mismatch always lands in rejected

UPC_SCORE = 100.0
MPN_EXACT_SCORE = 90.0
# Was 40, which alone exceeded REVIEW_MIN and floated every same-brand pair
# into review. Kept small but non-zero: it should nudge, never decide.
BRAND_MAX = 10.0
TITLE_MAX = 50.0

# Below this fuzzy ratio on the brand strings, treat the brands as genuinely
# different rather than a spelling variant.
BRAND_DISAGREE_BELOW = 70


def _brand_score(offer: Offer, cand: Candidate) -> float:
    if not offer.brand or not cand.brand:
        return 0.0
    if brands_match(offer.brand, cand.brand):
        return BRAND_MAX
    ratio = fuzz.ratio(offer.brand.lower(), cand.brand.lower())
    return (ratio / 100.0) * BRAND_MAX if ratio >= BRAND_DISAGREE_BELOW else 0.0


def _brand_disagrees(offer: Offer, cand: Candidate) -> bool:
    """True only when both sides state a brand and they're clearly different.
    Absence of a brand is not disagreement."""
    if not offer.brand or not cand.brand:
        return False
    if brands_match(offer.brand, cand.brand):
        return False
    return fuzz.ratio(offer.brand.lower(), cand.brand.lower()) < BRAND_DISAGREE_BELOW


def _title_score(offer: Offer, cand: Candidate) -> float:
    src = offer.title or offer.raw_text or ""
    amz = cand.title or ""
    if not src or not amz:
        return 0.0
    return (title_similarity(src, amz) / 100.0) * TITLE_MAX


def _mpn_score(offer: Offer, cand: Candidate) -> float:
    if not offer.mpn or not cand.mpn:
        return 0.0
    if base_mpn(offer.mpn) == base_mpn(cand.mpn):
        return MPN_EXACT_SCORE
    return 0.0


def _hard_mismatch(offer: Offer, cand: Candidate) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    if size_mismatch(offer, cand):
        reasons.append("size_mismatch")
    if apparel_size_mismatch(offer, cand):
        reasons.append("apparel_size_mismatch")
    if linear_size_mismatch(offer, cand):
        reasons.append("linear_size_mismatch")
    if _brand_disagrees(offer, cand):
        reasons.append("brand_mismatch")
    return bool(reasons), reasons


def verdict_for(confidence: float) -> Verdict:
    if confidence >= VERIFIED_MIN:
        return Verdict.VERIFIED
    if confidence >= REVIEW_MIN:
        return Verdict.REVIEW
    return Verdict.REJECTED


def score(offer: Offer, cand: Candidate) -> MatchResult:
    """Score one offer against one candidate. Pure function - no I/O."""
    signals: dict[str, float] = {}

    upc_hit = bool(
        offer.upc and cand.upc
        and upc_matches(normalize_upc(offer.upc), normalize_upc(cand.upc))
    )
    signals["upc"] = UPC_SCORE if upc_hit else 0.0
    signals["mpn"] = _mpn_score(offer, cand)
    signals["brand"] = _brand_score(offer, cand)
    signals["title"] = _title_score(offer, cand)

    if upc_hit:
        confidence = 100.0
    else:
        confidence = min(signals["mpn"] + signals["brand"] + signals["title"], 100.0)

    is_mismatch, reasons = _hard_mismatch(offer, cand)
    if is_mismatch:
        confidence = min(confidence, MISMATCH_CAP)

    return MatchResult(
        offer_id=offer.offer_id,
        asin=cand.asin,
        confidence=round(confidence, 1),
        verdict=verdict_for(confidence),
        signals=signals,
        reasons=reasons,
        used_agent=False,
    )