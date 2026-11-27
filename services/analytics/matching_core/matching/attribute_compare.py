"""Attribute extraction + comparison for the rule cascade.
"""
from __future__ import annotations

import re
from enum import Enum

from services.analytics.matching_core.matching.attr_extract import read_counts
from services.analytics.matching_core.matching.flags import EXTENDED_COUNTS
from services.analytics.matching_core.matching.patterns import (
    COLOR_WORDS,
    garment_relation,
    garment_sizes,
    PACK_COUNT_RE,
    SIZE_RE,
    TRANSPARENCY_WORDS,
    VOLUME_TO_ML,
    BUNDLE_COUNT_RE,
)
from services.analytics.matching_core.models import Candidate, Offer

TOLERANCE = 0.10

# Amazon's own pack fields. adapter.build_pair copies them into Candidate.attributes.


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


SIZE_UNCLEAR_RATIO = 10.0   # one volume more than 10x the other: probably not a real size


def volume_unclear(offer: Offer, cand: Candidate) -> bool:
    """The two volumes make no sense next to each other (Amazon's 'Compleat 1.4mL' means calories per mL,
    the vendor's '8.45 fl oz' is the bottle).  Such a number is no proof of a different product: unclear."""
    s, a = _texts(offer, cand)
    sv, av = _volume_ml(s), _volume_ml(a)
    if not sv or not av:
        return False
    return max(sv, av) / min(sv, av) > SIZE_UNCLEAR_RATIO


def cmp_volume(offer: Offer, cand: Candidate) -> Cmp:
    s, a = _texts(offer, cand)
    sv, av = _volume_ml(s), _volume_ml(a)
    if sv is None or av is None or volume_unclear(offer, cand):
        return Cmp.UNKNOWN
    return Cmp.MATCH if _within_tolerance(sv, av) else Cmp.MISMATCH


def _spelled(words: set[str]) -> set[str]:
    """One spelling per word, from the word library's spelling table ('grey' -> 'gray')."""
    from services.analytics.matching_core.matching.lexicon import get_lexicon
    spelling = get_lexicon().spelling
    return {spelling.get(w, w) for w in words}


def cmp_color(offer: Offer, cand: Candidate) -> Cmp:
    s, a = _texts(offer, cand)
    # spelling first: "Dark Grey" vs "Dark Gray" used to count as two different colors
    sc = _spelled((set(re.findall(r"[a-z]+", s)) & COLOR_WORDS) - TRANSPARENCY_WORDS)
    ac = _spelled((set(re.findall(r"[a-z]+", a)) & COLOR_WORDS) - TRANSPARENCY_WORDS)
    if not sc or not ac:
        return Cmp.UNKNOWN
    return Cmp.MATCH if (sc & ac) else Cmp.MISMATCH


def _garment_sizes(text: str) -> set[str]:
    # shared reader: size words, full-calf sizes (XLFC) and lone S/M/L in a size position
    return set(garment_sizes(text))


def cmp_size(offer: Offer, cand: Candidate) -> Cmp:
    """Garment/letter sizes (S/M/L/XL). Physical volume is cmp_volume."""
    s, a = _texts(offer, cand)
    rel = garment_relation(_garment_sizes(s), _garment_sizes(a))
    if rel is None:
        return Cmp.UNKNOWN
    return Cmp.MATCH if rel == "match" else Cmp.MISMATCH


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


def cmp_count(offer: Offer, cand: Candidate) -> Cmp:
    """Labeling rule (2026-11-19): a different PACK SIZE of the same item is a match; a different
    count PER PACKAGE is not ("36 Count" vs "50 Count").  So a pack multiplier ("Pack of 2" vs
    "Pack of 6", "2-Pack" vs "6-Pack") never gives MISMATCH here: the same pack supports the pair,
    a different pack is UNKNOWN.  The per-unit count is also checked by the shared variant gate
    (variation.compare_boxes 'units'), which follows the same rule.

    Vendor case-quantity ("100cs", "20cs") and Amazon bundle-size
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
        # Default mode reads pack notation only ("Pack of 3", "2-Pack"): a pack size, never a reason to reject.
        sp = _bundle_count(s)   # excludes the "Ncs" case-quantity form
        ap = _bundle_count(a)
        if sp is None or ap is None:
            return Cmp.UNKNOWN
        return Cmp.MATCH if sp == ap else Cmp.UNKNOWN

    # Extended mode: per-unit counts ("4 Count", "12 ct", "4PCS", the K in "3 Packs of K") kept apart from
    # pack multipliers ("Pack of 6", "6-Pack").  Only per-unit counts can disagree.
    sc, ac = read_counts(s), read_counts(a)
    su, au = set(sc["count"]), set(ac["count"]) or _amazon_unit_numbers(cand)
    if not su or not au:
        sp, ap = set(sc["pack"]) | set(sc["bundle"]), set(ac["pack"]) | set(ac["bundle"])
        return Cmp.MATCH if (not su and not au and sp & ap) else Cmp.UNKNOWN
    if su & au:
        return Cmp.MATCH
    return Cmp.MISMATCH if (len(su) == 1 and len(au) == 1) else Cmp.UNKNOWN


def _amazon_unit_numbers(cand: Candidate) -> set[int]:
    """Amazon's number_of_items when it is above 1 (item_package_quantity is a pack size, not a count)."""
    out: set[int] = set()
    values = (cand.attributes or {}).get("number_of_items") or []
    for v in values if isinstance(values, (list, tuple)) else [values]:
        try:
            n = int(float(str(v).strip()))
        except (TypeError, ValueError):
            continue
        if n > 1:
            out.add(n)
    return out


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
