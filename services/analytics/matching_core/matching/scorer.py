"""Deterministic matcher: score a candidate against an offer.
"""
from __future__ import annotations

from rapidfuzz import fuzz
import re

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
MISMATCH_CAP = 29.0  

UPC_SCORE = 100.0
MPN_EXACT_SCORE = 90.0
BRAND_MAX = 10.0
TITLE_MAX = 50.0

BRAND_DISAGREE_BELOW = 70

WEIGHTS = {"upc": 55.0, "mpn": 20.0, "brand": 10.0, "title": 15.0}
UPC_ALONE = 90.0      # UPC equal and nothing else known 
NO_ID_CAP = 89.0      # no UPC and no part number to compare: never auto-approve

_BARCODE_RE = re.compile(r"^(\d{8}|\d{11,14})$")


def _barcode(value: str | None) -> str | None:
    """A UPC/EAN/GTIN only counts if it looks like one: digits only (spaces, hyphens and a
    trailing .0 ignored), 8 or 11-14 digits. 'TBA', 'N/A' or '1050588V6UK' are placeholders
    or internal codes, not barcodes: they count as MISSING, not as 'different'."""
    s = re.sub(r"[\s\-]", "", str(value or ""))
    s = re.sub(r"\.0+$", "", s)
    return s if _BARCODE_RE.match(s) else None


def _strengths(offer: Offer, cand: Candidate, upc_hit: bool) -> dict[str, float]:
    """0..1 strength of every signal that exists on BOTH sides. A missing value is left
    out (not penalised); a value that is present but different counts as 0."""
    s: dict[str, float] = {}
    if _barcode(offer.upc) and _barcode(cand.upc):
        s["upc"] = 1.0 if upc_hit else 0.0
    if offer.mpn and cand.mpn:
        s["mpn"] = 1.0 if base_mpn(offer.mpn) == base_mpn(cand.mpn) else 0.0
    if offer.brand and cand.brand:
        s["brand"] = _brand_score(offer, cand) / BRAND_MAX
    src, amz = offer.title or offer.raw_text or "", cand.title or ""
    if src and amz:
        s["title"] = title_similarity(src, amz) / 100.0
    return s

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


def _strengths(offer: Offer, cand: Candidate, upc_hit: bool) -> dict[str, float]:
    """0..1 strength of every signal that exists on BOTH sides. A missing value is left
    out (not penalised); a value that is present but different counts as 0."""
    s: dict[str, float] = {}
    if _barcode(offer.upc) and _barcode(cand.upc):
        s["upc"] = 1.0 if upc_hit else 0.0
    if offer.mpn and cand.mpn:
        s["mpn"] = 1.0 if base_mpn(offer.mpn) == base_mpn(cand.mpn) else 0.0
    if offer.brand and cand.brand:
        s["brand"] = _brand_score(offer, cand) / BRAND_MAX
    src, amz = offer.title or offer.raw_text or "", cand.title or ""
    if src and amz:
        s["title"] = title_similarity(src, amz) / 100.0
    return s


def verdict_for(confidence: float) -> Verdict:
    if confidence >= VERIFIED_MIN:
        return Verdict.VERIFIED
    if confidence >= REVIEW_MIN:
        return Verdict.REVIEW
    return Verdict.REJECTED


def score(offer: Offer, cand: Candidate) -> MatchResult:
    """Score one offer against one candidate. Pure function - no I/O.
 
    Every signal that exists on BOTH sides gets its weight. Signals that are missing are
    left out and their weight is shared by the others, so a missing UPC is not a penalty.
    A signal that is present but different counts as 0."""
    upc_hit = bool(
        _barcode(offer.upc) and _barcode(cand.upc)
        and upc_matches(normalize_upc(offer.upc), normalize_upc(cand.upc))
    )
    s = _strengths(offer, cand, upc_hit)
    total_w = sum(WEIGHTS[k] for k in s)
    # each signal's share of 100, after the missing signals' weight is handed to the rest
    signals = {k: 100.0 * WEIGHTS[k] * v / total_w for k, v in s.items()} if total_w else {}
    confidence = sum(signals.values())
 
    if upc_hit:                                    # UPC alone = 90, UPC + same brand = 100
        confidence = 100.0 if s.get("brand", 0.0) >= 1.0 else max(confidence, UPC_ALONE)
    elif "upc" not in s and "mpn" not in s:        # no identifier to compare: cannot be approved
        confidence = min(confidence, NO_ID_CAP)
 
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
 