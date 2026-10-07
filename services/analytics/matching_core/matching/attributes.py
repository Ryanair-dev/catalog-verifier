from __future__ import annotations

from services.analytics.matching_core.matching.patterns import (
    ALNUM_WORD_RE,
    GARMENT_NORM,
    GARMENT_SIZE_RE,
    SIZE_RE,
    VOLUME_TO_ML,
    WEIGHT_RE,
    WEIGHT_TO_G,
)
from services.analytics.matching_core.models import Candidate, Offer

_TOLERANCE = 0.10  # 10% covers rounding (355 ml ≈ 12 oz)


def _within_tolerance(a: float, b: float) -> bool:
    if min(a, b) <= 0:
        return a == b
    return max(a, b) / min(a, b) <= 1 + _TOLERANCE


def _extract_volume_ml(text: str) -> float | None:
    m = SIZE_RE.search(text.lower())
    if not m:
        return None
    unit = m.group(2).replace(" ", "").replace(".", "")
    factor = VOLUME_TO_ML.get(unit)
    return float(m.group(1)) * factor if factor else None


def _extract_weight_g(text: str) -> float | None:
    m = WEIGHT_RE.search(text.lower())
    if not m:
        return None
    unit = m.group(2).replace(" ", "").replace(".", "")
    factor = WEIGHT_TO_G.get(unit)
    return float(m.group(1)) * factor if factor else None


def _garment_sizes(text: str) -> set[str]:
    out: set[str] = set()
    for m in GARMENT_SIZE_RE.finditer(text or ""):
        key = m.group(1).lower().replace("-", "").replace(" ", "")
        norm = GARMENT_NORM.get(key)
        if norm:
            out.add(norm)
    return out


def _both_titles(offer: Offer, cand: Candidate) -> tuple[str, str]:
    return (offer.title or offer.raw_text or ""), (cand.title or "")


def size_mismatch(offer: Offer, cand: Candidate) -> bool:
    """True when both sides state a physical size (volume or weight) and they
    differ by more than the tolerance. Volume is never compared to weight."""
    src, amz = _both_titles(offer, cand)

    sv, av = _extract_volume_ml(src), _extract_volume_ml(amz)
    if sv and av:
        return not _within_tolerance(sv, av)

    sw, aw = _extract_weight_g(src), _extract_weight_g(amz)
    if sw and aw:
        return not _within_tolerance(sw, aw)

    return False


def apparel_size_mismatch(offer: Offer, cand: Candidate) -> bool:
    """True when both sides state a garment size (S/M/L/XL…) and the sets are
    disjoint. 'Large' vs 'Large/X-Large' overlaps -> not a mismatch."""
    src, amz = _both_titles(offer, cand)
    s, a = _garment_sizes(src), _garment_sizes(amz)
    if not s or not a:
        return False
    return s.isdisjoint(a)


def variant_mismatch(offer: Offer, cand: Candidate, vocab: set[str]) -> bool:
    """Generic variant-word disagreement. Given a vocabulary of meaningful
    descriptor words (colours, scents…), fire when each side names a word from
    that vocab the other lacks — i.e. neither set is a subset of the other.

    Vocab is passed in, not hardcoded, so the caller controls which descriptors
    matter for a given run (colours for apparel, scents for CPG, etc.)."""
    src, amz = _both_titles(offer, cand)
    s_words = set(ALNUM_WORD_RE.findall(src.lower())) & vocab
    a_words = set(ALNUM_WORD_RE.findall(amz.lower())) & vocab
    if not s_words or not a_words:
        return False
    return bool(s_words - a_words) and bool(a_words - s_words)

from services.analytics.matching_core.matching.patterns import (
    CM_RE, INCH_RE, MM_RE, MIXED_FRAC_RE, SIMPLE_FRAC_RE, UNICODE_FRACS,
)


def _normalize_fractions(text: str) -> str:
    """'1 3/4 in' -> '1.75 in', '¾' -> '0.75'. Run before dimension parsing so
    numbers are always decimal. Denominators limited to real measurement
    fractions so CPG slash-pack codes (2/1200ML) aren't disturbed."""
    for ch, asc in UNICODE_FRACS.items():
        text = text.replace(ch, asc)

    def _mixed(m):
        whole, num, den = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return f"{whole + num / den:.6f}".rstrip("0").rstrip(".")

    def _simple(m):
        num, den = int(m.group(1)), int(m.group(2))
        return f"{num / den:.6f}".rstrip("0").rstrip(".")

    text = MIXED_FRAC_RE.sub(_mixed, text)
    return SIMPLE_FRAC_RE.sub(_simple, text)


def _dims(text: str, pattern) -> list[float]:
    clean = _normalize_fractions(text.lower())
    return sorted(float(v) for v in pattern.findall(clean))


def linear_size_mismatch(offer: Offer, cand: Candidate) -> bool:
    """True when both sides give linear dimensions in the SAME unit system and
    they disagree. Imperial (inches) and metric (cm/mm) are never cross-compared.
    Only fires when both have the same number of dimensions."""
    src, amz = _both_titles(offer, cand)
    for pattern in (INCH_RE, CM_RE, MM_RE):
        s, a = _dims(src, pattern), _dims(amz, pattern)
        if not s or not a or len(s) != len(a):
            continue
        if any(not _within_tolerance(x, y) for x, y in zip(s, a)):
            return True
    return False