from __future__ import annotations

import re

from rapidfuzz import fuzz

from services.analytics.matching_core.matching.flags import CLEAN_TITLES
from services.analytics.matching_core.matching.patterns import (
    APOSTROPHE_RE,
    BRAND_NOISE_WORDS,
    DIGITS_ONLY_RE,
    MIN_BASE_LEN,
    MPN_SUFFIX_RE,
    NON_ALNUM_RE,
    NON_ALNUM_SPACE_RE,
    NON_DIGIT_RE,
    PACK_COUNT_RE,
    WHITESPACE_RE,
)


def base_mpn(mpn: str | None) -> str:
    """Reduce an MPN to its base part number: strip seller/pack suffixes, then
    lowercase and drop punctuation.

        '0354-150H-2PK'      -> '0354150h'
        '0311-150F - AMZ3PK' -> '0311150f'
        '0354-150H'          -> '0354150h'   (no suffix, untouched)

    Keeps the original when stripping would leave almost nothing, so an MPN is
    never reduced to noise.
    """
    if not mpn:
        return ""
    s = str(mpn).strip()
    stripped = MPN_SUFFIX_RE.sub("", s)
    if len(NON_ALNUM_RE.sub("", stripped)) < MIN_BASE_LEN:
        return NON_ALNUM_RE.sub("", s).lower()
    return NON_ALNUM_RE.sub("", stripped).lower()


def normalize_upc(upc: str | None) -> str:
    """Digits only; zero-pad 11-digit UPCs to 12 (vendor feeds often strip the
    leading zero)."""
    if not upc:
        return ""
    digits = NON_DIGIT_RE.sub("", str(upc))
    return "0" + digits if len(digits) == 11 else digits


def normalize_text(text: str | None) -> str:
    """Lowercase, collapse whitespace. For coarse comparison, not for building
    search queries."""
    if not text:
        return ""
    return WHITESPACE_RE.sub(" ", str(text).lower().strip())


# --------------------------------------------------------------------------- #
# Title comparison - ONE place, used by the weighted, cascade and classifier
# scorers so both sides of every pair are cleaned the same way.
# --------------------------------------------------------------------------- #

# a number (decimals kept: "6.5") or a run of letters. Splitting digits from letters
# turns "4PCS" into "4 pcs" and "56oz" into "56 oz", which is how Amazon writes them.
_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?|[^\W\d_]+")


def clean_for_compare(text: str | None) -> str:
    """Lowercase; punctuation, hyphens and pipes become spaces; numbers and words
    are split apart.

        'Oral-B CrossAction, Pack of 4 | Black' -> 'oral b crossaction pack of 4 black'
        'ORAL B HEADS 4PCS'                     -> 'oral b heads 4 pcs'
    """
    if not text:
        return ""
    return " ".join(_TOKEN_RE.findall(str(text).lower()))


def title_similarity(a: str | None, b: str | None) -> float:
    """Title similarity on a 0-100 scale. With MATCH_CLEAN_TITLES=0 this is the old
    behaviour (lowercase only)."""
    if not a or not b:
        return 0.0
    if CLEAN_TITLES:
        a, b = clean_for_compare(a), clean_for_compare(b)
    else:
        a, b = str(a).lower(), str(b).lower()
    if not a or not b:
        return 0.0
    return float(fuzz.token_set_ratio(a, b))


def _ean13_check_digit(d12: str) -> str:
    total = sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(d12))
    return str((10 - (total % 10)) % 10)


def _is_valid_upca(v: str) -> bool:
    if len(v) != 12 or not v.isdigit():
        return False
    s = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(v[:11]))
    return str((10 - (s % 10)) % 10) == v[11]


def all_id_forms(value: str | None) -> list[str]:
    """Every plausible barcode form of an identifier, so a match succeeds
    whether the value is stored as UPC-12, EAN-13 or GTIN-14 — including
    recovery of an EAN-13 whose trailing check digit was dropped.

    Ported from the old runner; barcode reconciliation is genuinely fiddly and
    not worth rediscovering."""
    if not value:
        return []
    v = DIGITS_ONLY_RE.sub("", str(value))
    if not v:
        return []
    forms = [v]
    n = len(v)
    if n == 12:
        forms += ["0" + v, "00" + v]
        if not _is_valid_upca(v):
            forms.append(v + _ean13_check_digit(v))
    elif n == 13:
        forms.append("0" + v)
        if v.startswith("0"):
            forms.append(v[1:])
        forms.append(v[:12])
    elif n == 14:
        forms.append(v[1:])
        if v.startswith("00"):
            forms.append(v[2:])
    return list(dict.fromkeys(forms))


def upc_matches(a: str | None, b: str | None) -> bool:
    """True when two identifiers share any barcode form."""
    fa, fb = set(all_id_forms(a)), set(all_id_forms(b))
    return bool(fa and fb and (fa & fb))

def pack_count(text: str) -> int:
    """Extract a pack/count number from a title. Returns 1 if none found.
    'Container 36 count' -> 36, 'pack of 72' -> 72."""
    for m in PACK_COUNT_RE.finditer(text or ""):
        n = next((g for g in m.groups() if g is not None), None)
        if n:
            return int(n)
    return 1


def effective_pack(offer_title: str, cand_title: str) -> tuple[int, bool]:
    """How many offer units one candidate listing represents, and whether
    that's a mismatch (more than one offer unit per listing).

    'Container 36 count' + 'pack of 36'  -> (1, False)   same unit
    'Container 36 count' + 'pack of 72'  -> (2, True)    2x bundle
    'Cleaner 8 oz'        + 'pack of 6'   -> (6, True)    6x bundle
    A pack difference alone doesn't mean a different SKU - it's used as a
    soft signal (cap toward review), not a hard reject.
    """
    src_count = pack_count(offer_title)
    amz_count = pack_count(cand_title)
    if src_count > 1:
        if amz_count == 0 or amz_count == src_count:
            return 1, False
        if amz_count > src_count and amz_count % src_count == 0:
            ep = amz_count // src_count
            return ep, ep > 1
        return (amz_count if amz_count > 0 else 1), amz_count > src_count
    return (amz_count, amz_count > 1) if amz_count > 1 else (1, False)


def normalize_brand(brand: str | None) -> str:
    """Strip corporate suffixes/generic words, collapse spacing/punctuation.
    'Cardinal Health' -> 'cardinal', 'Shea Moisture' -> 'sheamoisture'."""
    if not brand:
        return ""
    s = APOSTROPHE_RE.sub("", brand.lower())
    s = NON_ALNUM_SPACE_RE.sub(" ", s)
    tokens = [t for t in s.split() if t]
    kept = [t for t in tokens if t not in BRAND_NOISE_WORDS]
    return "".join(kept) if kept else "".join(tokens)


def brands_match(a: str | None, b: str | None) -> bool:
    """True when two brand strings are the same brand once normalized -
    handles spacing ('Shea Moisture'='SheaMoisture'), corporate suffixes
    ('Cardinal Health'='Cardinal'), and minor spelling variants (fuzzy>=88)."""
    na, nb = normalize_brand(a), normalize_brand(b)
    if not na or not nb:
        return False
    if na == nb:
        return True
    if len(na) >= 5 and len(nb) >= 5 and (nb.startswith(na) or na.startswith(nb)):
        return True
    return fuzz.ratio(na, nb) >= 88