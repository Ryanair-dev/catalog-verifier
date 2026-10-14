"""Attribute extraction + comparison for the rule cascade.
"""
from __future__ import annotations

import re
from enum import Enum

from services.analytics.matching_core.matching.flags import EXTENDED_COUNTS
from services.analytics.matching_core.matching.patterns import (
    COLOR_WORDS,
    GARMENT_NORM,
    GARMENT_SIZE_RE,
    PACK_COUNT_RE,
    SIZE_RE,
    TRANSPARENCY_WORDS,
    VOLUME_TO_ML,
    BUNDLE_COUNT_RE,
    BUNDLE_COUNT_EXT_RE,
)
from services.analytics.matching_core.models import Candidate, Offer

TOLERANCE = 0.10

# Amazon's own pack fields. adapter.build_pair copies them into Candidate.attributes.
_AMAZON_PACK_KEYS = ("item_package_quantity", "number_of_items")


class Cmp(str, Enum):
    MATCH = "match"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


def _texts(offer: Offer, cand: Candidate) -> tuple[str, str]:
    src = " ".join(filter(None, [
        offer.title, offer.raw_text,
        *(str(v) for v in (offer.attributes or {}).values()),
    ]))
    amz = " ".join(filter(None, [
        cand.title,
        *(str(v) for v in (cand.attributes or {}).values()),
    ]))
    return src.lower(), amz.lower()


def _within_tolerance(a: float, b: float) -> bool:
    if min(a, b) <= 0:
        return a == b
    return max(a, b) / min(a, b) <= 1 + TOLERANCE


def _volume_ml(text: str) -> float | None:
    m = SIZE_RE.search(text or "")
    if not m:
        return None
    unit = m.group(2).replace(" ", "").replace(".", "")
    factor = VOLUME_TO_ML.get(unit)
    return float(m.group(1)) * factor if factor else None


def cmp_volume(offer: Offer, cand: Candidate) -> Cmp:
    s, a = _texts(offer, cand)
    sv, av = _volume_ml(s), _volume_ml(a)
    if sv is None or av is None:
        return Cmp.UNKNOWN
    return Cmp.MATCH if _within_tolerance(sv, av) else Cmp.MISMATCH


def cmp_color(offer: Offer, cand: Candidate) -> Cmp:
    s, a = _texts(offer, cand)
    sc = (set(re.findall(r"[a-z]+", s)) & COLOR_WORDS) - TRANSPARENCY_WORDS
    ac = (set(re.findall(r"[a-z]+", a)) & COLOR_WORDS) - TRANSPARENCY_WORDS
    if not sc or not ac:
        return Cmp.UNKNOWN
    return Cmp.MATCH if (sc & ac) else Cmp.MISMATCH


def _garment_sizes(text: str) -> set[str]:
    out: set[str] = set()
    for m in GARMENT_SIZE_RE.finditer(text or ""):
        key = m.group(1).lower().replace("-", "").replace(" ", "")
        norm = GARMENT_NORM.get(key)
        if norm:
            out.add(norm)
    return out


def cmp_size(offer: Offer, cand: Candidate) -> Cmp:
    """Garment/letter sizes (S/M/L/XL). Physical volume is cmp_volume."""
    s, a = _texts(offer, cand)
    ss, aa = _garment_sizes(s), _garment_sizes(a)
    if not ss or not aa:
        return Cmp.UNKNOWN
    return Cmp.MATCH if (ss & aa) else Cmp.MISMATCH


def _pack_count(text: str) -> int | None:
    for m in PACK_COUNT_RE.finditer(text or ""):
        n = next((g for g in m.groups() if g is not None), None)
        if n:
            return int(n)
    return None

def _bundle_count(text: str) -> int | None:
    """Only bundle-style notation ("Pack of 3", "2-Pack"). Vendor
    cases-per-carton ("100cs") is deliberately NOT matched - see
    BUNDLE_COUNT_RE."""
    for m in BUNDLE_COUNT_RE.finditer(text or ""):
        n = next((g for g in m.groups() if g is not None), None)
        if n:
            return int(n)
    return None


def bundle_counts(text: str) -> set[int]:
    """EVERY bundle count written in a text, e.g. '3-Pack, 80 Count' -> {3, 80}.
    Wider notation than _bundle_count: also 4PCS, 4 pc, 2 pieces, 12 ct, 4 Count.
    Vendor case quantity ('40cs') is still not read."""
    out: set[int] = set()
    for m in BUNDLE_COUNT_EXT_RE.finditer(text or ""):
        n = next((g for g in m.groups() if g is not None), None)
        if n and int(n) > 0:
            out.add(int(n))
    return out


def _amazon_pack_numbers(cand: Candidate) -> set[int]:
    """Amazon's item_package_quantity / number_of_items, when present."""
    out: set[int] = set()
    for key in _AMAZON_PACK_KEYS:
        values = (cand.attributes or {}).get(key) or []
        if not isinstance(values, (list, tuple)):
            values = [values]
        for v in values:
            try:
                n = int(float(str(v).strip()))
            except (TypeError, ValueError):
                continue
            if n > 0:
                out.add(n)
    return out


def cmp_count(offer: Offer, cand: Candidate) -> Cmp:
    """Vendor case-quantity ("100cs", "20cs") and Amazon bundle-size
    ("3-Pack") are different concepts - the vendor number is how many units
    ship per carton, the Amazon number is how many are in the listing. They
    are not comparable, and treating them as one attribute produced a false
    mismatch on every true match in the eval set.

    Only compare when BOTH sides use bundle-style notation. A vendor
    case-quantity yields UNKNOWN.

    With MATCH_EXTENDED_COUNTS on (default), "4PCS", "12 ct" and "4 Count" also
    count as bundle notation, every count in the text is collected (not just the
    first), and Amazon's own pack-quantity field is used when the Amazon title
    states no count. A MISMATCH needs exactly one count on each side and the two
    numbers must differ; if either side states several ("Pack of 6, 2 Count
    each") the answer is UNKNOWN, so multipacks are not rejected by mistake.
    """
    s, a = _texts(offer, cand)
    if not EXTENDED_COUNTS:
        sp = _bundle_count(s)   # excludes the "Ncs" case-quantity form
        ap = _bundle_count(a)
        if sp is None or ap is None:
            return Cmp.UNKNOWN
        return Cmp.MATCH if sp == ap else Cmp.MISMATCH

    sp = bundle_counts(s)
    ap = bundle_counts(a) or _amazon_pack_numbers(cand)
    if not sp or not ap:
        return Cmp.UNKNOWN
    if sp & ap:
        return Cmp.MATCH
    return Cmp.MISMATCH if (len(sp) == 1 and len(ap) == 1) else Cmp.UNKNOWN


ATTRIBUTE_COMPARERS = {
    "volume": cmp_volume,
    "color": cmp_color,
    "size": cmp_size,
    "count": cmp_count,
}


def compare_attributes(offer: Offer, cand: Candidate) -> dict[str, Cmp]:
    return {name: fn(offer, cand) for name, fn in ATTRIBUTE_COMPARERS.items()}


def summarize(results: dict[str, Cmp]) -> tuple[int, int, int]:
    """(n_match, n_mismatch, n_known)"""
    m = sum(1 for v in results.values() if v is Cmp.MATCH)
    x = sum(1 for v in results.values() if v is Cmp.MISMATCH)
    return m, x, m + x