"""
Rule-cascade matcher, per the spec.

Verifying CPG - if:
1. Brand + UPC/EAN = 100% correct
2. UPC/EAN + 1 attribute = 100% correct
3. brand + All attributes (size + colour + volume + count) = 100% correct
4. ALL attributes + fuzzy > 70(?) <- lets try with 65-70% = 90% correct
5. fuzzy > 90% = 90% correct
6. all attributes match = 90% correct

Verifying Medical - if:
1. Brand + MPN/PartNumber = 100% correct
2. MPN/PartNumber + 1 attribute = 100% correct
3. brand + All attributes (size + colour + volume + count) = 100% correct
4. ALL attributes + fuzzy > 70(?) <- lets try with 65-70% = 90% correct
5. fuzzy > 90% = 90% correct
6. all attributes match = 90% correct
7. MPN/Partnumber found in the AMZ title and/or in the bullet points and/or in the long description 
= 85% (if at least 1 attribute matches score increase to 95%)
"""
from __future__ import annotations

from rapidfuzz import fuzz

from services.analytics.matching_core.matching.attribute_compare import (
    Cmp,
    compare_attributes,
    summarize,
)
from services.analytics.matching_core.matching.normalize import (
    base_mpn,
    brands_match,
    normalize_upc,
    upc_matches,
)
from services.analytics.matching_core.models import Candidate, MatchResult, Offer, Verdict

VERIFIED_MIN = 90.0
REVIEW_MIN = 35.0
MISMATCH_CAP = 29.0

FUZZY_WITH_ATTRS = 70.0
FUZZY_ALONE = 72.0

# Measured: rules 3 and 6 firing on two known attributes caused 5 of 6 false
# approvals. Set to 1 to reproduce the spec exactly.
MIN_ATTRS_FOR_ALL = 3


def _fuzzy(offer: Offer, cand: Candidate) -> float:
    src = offer.title or offer.raw_text or ""
    amz = cand.title or ""
    if not src or not amz:
        return 0.0
    return float(fuzz.token_set_ratio(src.lower(), amz.lower()))


def _mpn_in_amazon_text(offer: Offer, cand: Candidate) -> bool:
    """Medical rule 7: part number appears in the Amazon title, bullets, or
    description. Requires >=4 alphanumerics incl. a digit so generic SKUs
    can't trigger it."""
    m = base_mpn(offer.mpn)
    if len(m) < 4 or not any(c.isdigit() for c in m):
        return False
    haystack = " ".join([
        cand.title or "",
        *(getattr(cand, "bullet_points", None) or []),
        getattr(cand, "description", "") or "",
    ])
    return m in "".join(ch for ch in haystack.lower() if ch.isalnum())


def score(offer: Offer, cand: Candidate, mode: str = "cpg") -> MatchResult:
    """mode: 'cpg' (keys on UPC) or 'medical' (keys on MPN)."""
    attrs = compare_attributes(offer, cand)
    n_match, n_mismatch, n_known = summarize(attrs)
    fuzzy = _fuzzy(offer, cand)

    brand_ok = bool(offer.brand and cand.brand and brands_match(offer.brand, cand.brand))
    upc_ok = bool(offer.upc and cand.upc
                  and upc_matches(normalize_upc(offer.upc), normalize_upc(cand.upc)))
    mpn_ok = bool(offer.mpn and cand.mpn and base_mpn(offer.mpn) == base_mpn(cand.mpn))

    ident_ok = upc_ok if mode == "cpg" else mpn_ok
    ident_name = "upc" if mode == "cpg" else "mpn"

    all_attrs_match = (
        n_known >= MIN_ATTRS_FOR_ALL and n_mismatch == 0 and n_match == n_known
    )

    signals = {
        "brand": brand_ok, "upc": upc_ok, "mpn": mpn_ok, "fuzzy": round(fuzzy, 1),
        "attrs_matched": n_match, "attrs_mismatched": n_mismatch,
        "attrs_known": n_known,
    }
    reasons = [f"{k}_mismatch" for k, v in attrs.items() if v is Cmp.MISMATCH]

    # --- the cascade: first rule to fire wins ---
    if brand_ok and ident_ok:
        confidence, rule = 100.0, f"1_brand+{ident_name}"
    elif ident_ok and n_match >= 1:
        confidence, rule = 100.0, f"2_{ident_name}+1attr"
    elif brand_ok and all_attrs_match:
        confidence, rule = 100.0, "3_brand+all_attrs"
    elif all_attrs_match and fuzzy > FUZZY_WITH_ATTRS:
        confidence, rule = 90.0, "4_all_attrs+fuzzy70"
    elif fuzzy > FUZZY_ALONE:
        confidence, rule = 90.0, "5_fuzzy72"
    elif all_attrs_match:
        confidence, rule = 90.0, "6_all_attrs"
    elif mode == "medical" and _mpn_in_amazon_text(offer, cand):
        confidence = 95.0 if n_match >= 1 else 85.0
        rule = "7_mpn_in_amz_text" + ("+1attr" if n_match >= 1 else "")
    else:
        # nothing fired - weak fuzzy fallback so a near-miss reaches review
        # rather than being silently rejected
        confidence, rule = min(fuzzy * 0.5, 60.0), "fallback_fuzzy"

    # contradictions override any rule
    if n_mismatch > 0:
        confidence = min(confidence, MISMATCH_CAP)
        rule += "+blocked_by_mismatch"

    verdict = (Verdict.VERIFIED if confidence >= VERIFIED_MIN
               else Verdict.REVIEW if confidence >= REVIEW_MIN
               else Verdict.REJECTED)

    return MatchResult(
        offer_id=offer.offer_id, asin=cand.asin, confidence=round(confidence, 1),
        verdict=verdict, signals=signals, reasons=reasons + [f"rule:{rule}"],
    )