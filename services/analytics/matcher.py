"""
Confidence scoring for analytics runs.

Weights:

    UPC exact match    -> automatic 100
    Brand              -> 40 points
      * substring in Amazon title / brand field / description = full 40
      * otherwise fuzzy match (>= 70%) across those three fields
      * additional keywords can contribute up to 40 toward the brand score
    Title similarity   -> 50 points
      * brand + MPN both present in Amazon title or description = auto 50
      * otherwise best-of-4 candidate token_set_ratio against Amazon title
    MPN (dash-norm)    -> 10 points

Totals are capped at 100.

Source / Amazon dict shapes
---------------------------
`source` dict keys (all optional except you probably want title/brand
or UPC):
    upc, mpn, itemid, title, brand, manufacturer

`amazon` dict keys (produced by runner.normalize_amazon_item):
    asin, title, brand, manufacturer, mpn, upc, ean,
    description, bullet_points, item_package_quantity, number_of_items,
    attributes  (dict; optional — color/size/material if available)
"""
from __future__ import annotations

from typing import Any, Iterable

import re

try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover - rapidfuzz is in requirements.txt
    fuzz = None  # type: ignore


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #

BRAND_MAX  = 40.0   # was 70 — brand still matters but title is now the primary signal
TITLE_MAX  = 50.0   # was 20 — title similarity is the most reliable CPG signal
MPN_MAX    = 10.0

_PACK_RE = re.compile(
    r'\bpack\s+of\s+(\d+)\b'           # "pack of 36"
    r'|\bbox\s+of\s+(\d+)\b'           # "box of 36"
    r'|\bset\s+of\s+(\d+)\b'           # "set of 36"
    r'|\bcount\s+of\s+(\d+)\b'         # "count of 36"
    r'|\b(\d+)\s*[-\s]?count\b'        # "36 count", "36-count", "36count"
    r'|\b(\d+)\s*[-\s]?ct\b'           # "36ct", "36 ct", "36-ct"
    r'|\b(\d+)\s*[-\s]?pack\b'         # "36 pack", "36-pack"
    r'|\b(\d+)\s*[-\s]?pk\b'           # "36pk", "36 pk"
    r'|\b(\d+)\s*[-\s]?piece[s]?\b'    # "36 piece", "36-piece", "36 pieces"
    r'|\b(\d+)\s*[-\s]?pcs\b'          # "36pcs", "36 pcs"
    r'|\b(\d+)\s*[-\s]?rolls?\b'       # "2 rolls", "2-roll"  (tape, paper products)
    r'|\b(\d+)\s*[-\s]?tubes?\b'       # "3 tubes"  (creams, ointments)
    r'|\b(\d+)\s*[-\s]?pairs?\b'       # "6 pairs"  (gloves, socks)
    r'|\b(\d+)\s*[-\s]?vials?\b'       # "10 vials"  (medical)
    r'|\b(\d+)\s*[-\s]?sachets?\b'     # "30 sachets"
    r'|\b(\d+)/(?=\d)',                 # "2/1200ML", "12/8OZ" — CPG slash-pack format
    re.IGNORECASE,
)

# Volume size extraction — oz / fl oz / ounce / ml / l / gal
# Uses negative lookbehind (?<![.\d]) instead of \b so that a leading-decimal
# size like ".5 oz" is captured as 0.5 rather than 5 (which \b would give by
# matching the word boundary between the preceding "." and the digit "5").
_SIZE_RE = re.compile(
    r'(?<![.\d])(\d+(?:\.\d+)?|\.\d+)[\s-]*'
    r'(fl\.?\s*oz|fluid\s+ounce[s]?|ounce[s]?|oz|milliliter[s]?|ml'
    r'|liter[s]?|litre[s]?|gallon[s]?|gal|l)\b',
    re.IGNORECASE,
)

# Weight extraction — lb / kg only (avoids false positives from "g" or "oz" alone)
_WEIGHT_RE = re.compile(
    r'(?<![.\d])(\d+(?:\.\d+)?|\.\d+)\s*'
    r'(pound[s]?|lbs?|kilogram[s]?|kg)\b',
    re.IGNORECASE,
)

# Linear dimension extraction — inches (via " or ″ symbol) and yards.
# The " pattern is deliberately tight (number + quote, no space) to avoid
# false positives from regular prose quotes.
_INCH_RE = re.compile(r'(?<![.\d])(\d+(?:\.\d+)?)["″]', re.IGNORECASE)
_YARD_RE = re.compile(
    r'(?<![.\d])(\d+(?:\.\d+)?)[\s-]*(?:yd\.?s?|yard\.?s?)\b',
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Fraction normalisation helpers
# ---------------------------------------------------------------------------

# Unicode vulgar-fraction characters → ASCII N/D form
_UNICODE_FRACS: dict[str, str] = {
    '½': '1/2',  # ½
    '⅓': '1/3',  # ⅓   '⅔': '2/3',  # ⅔
    '¼': '1/4',  # ¼
    '¾': '3/4',  # ¾
    '⅕': '1/5',  # ⅕
    '⅖': '2/5',  # ⅖
    '⅗': '3/5',  # ⅗
    '⅘': '4/5',  # ⅘
    '⅙': '1/6',  # ⅙
    '⅚': '5/6',  # ⅚
    '⅛': '1/8',  # ⅛
    '⅜': '3/8',  # ⅜
    '⅝': '5/8',  # ⅝
    '⅞': '7/8',  # ⅞
}

# Mixed fraction: "1 3/4" or "1-3/4" → decimal.
# Denominator limited to common measurement fractions to avoid matching
# CPG slash-pack codes like "2/1200ML".
_MIXED_FRAC_RE = re.compile(
    r'(\d+)[\s-](\d{1,2})/(2|3|4|5|6|7|8|10|12|16|32|64)(?!\d)',
)
# Simple fraction (no whole-number prefix): "3/4", "1/8" etc.
_SIMPLE_FRAC_RE = re.compile(
    r'(?<!\d)(\d{1,2})/(2|3|4|5|6|7|8|10|12|16|32|64)(?!\d)',
)


def _normalize_fractions(text: str) -> str:
    """
    Convert all fraction notation in *text* to decimal strings so that the
    dimension regexes work on consistent numeric forms.

    Steps (in order):
      1. Unicode vulgar fractions  → ASCII:  "¾"  → "3/4"
      2. Mixed fractions           → decimal: "1 3/4" / "2-3/8" → "1.75" / "2.375"
      3. Simple fractions          → decimal: "3/4" → "0.75"

    Denominators are restricted to {2,3,4,5,6,7,8,10,12,16,32,64} so that
    CPG slash-pack codes ("2/1200ML") and item numbers are not disturbed.
    """
    # Step 1
    for ch, asc in _UNICODE_FRACS.items():
        text = text.replace(ch, asc)

    # Step 2 — must precede Step 3 so "1 3/4" is not first converted to "1 0.75"
    def _mixed(m: re.Match) -> str:
        whole, num, denom = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if denom == 0:
            return m.group(0)
        result = whole + num / denom
        return f"{result:.6f}".rstrip('0').rstrip('.')

    text = _MIXED_FRAC_RE.sub(_mixed, text)

    # Step 3
    def _simple(m: re.Match) -> str:
        num, denom = int(m.group(1)), int(m.group(2))
        if denom == 0:
            return m.group(0)
        result = num / denom
        return f"{result:.6f}".rstrip('0').rstrip('.')

    text = _SIMPLE_FRAC_RE.sub(_simple, text)
    return text


# ---------------------------------------------------------------------------
# Multi-dimensional linear size patterns
# ---------------------------------------------------------------------------
# All patterns operate on text that has been fraction-normalised first so
# numbers are always in decimal form (no "1 3/4", "2-3/8", "¾" etc.).
#
# Two pattern families per unit:
#   _*_INDIVIDUAL_RE  — each number carries its own unit: 3"  3 inch  3 in.
#   _*_PAIR_RE        — a pair/triple shares a trailing unit:
#                         "6 x 8 inch"  or  "N x M"" (last number carries ")
#
# For pairs, the pattern allows an optional third dimension (N x M x P unit).

# Inches: matches  3"  3″  3 inch  3 inches  3-inch  3 in.  3-in.
# "3 in" (bare, no period) is NOT matched here because "in" is too common
# as a preposition; "in." (with a period) IS safe.
_INCH_IND_RE = re.compile(
    r'(?<![.\d])(\d+(?:\.\d+)?)'
    r'(?:["″]|[\s-]*inches?\b|[\s-]*in\.)',
    re.IGNORECASE,
)
# Pair: trailing unit  — "6 x 8 inch" / "2.375 x 2.75 in."
# Also catches bare "in" when used as a trailing unit after dimensions.
_INCH_PAIR_RE = re.compile(
    r'(?<![.\d])(\d+(?:\.\d+)?)\s*[×xX]\s*(\d+(?:\.\d+)?)'
    r'(?:\s*[×xX]\s*(\d+(?:\.\d+)?))?'
    r'[\s-]*(?:inches?\b|in\.?)',
    re.IGNORECASE,
)
# Mixed pair: last dimension carries "  — "2.125 x 4.75"" / "N x M x P""
_INCH_MIXED_PAIR_RE = re.compile(
    r'(?<![.\d])(\d+(?:\.\d+)?)\s*[×xX]\s*(\d+(?:\.\d+)?)'
    r'(?:\s*[×xX]\s*(\d+(?:\.\d+)?))?'
    r'["″]',
    re.IGNORECASE,
)

# Centimetres
_CM_IND_RE = re.compile(
    r'(?<![.\d])(\d+(?:\.\d+)?)[\s-]*(?:centimeters?|centimetres?|cm)\b',
    re.IGNORECASE,
)
_CM_PAIR_RE = re.compile(
    r'(?<![.\d])(\d+(?:\.\d+)?)\s*[×xX]\s*(\d+(?:\.\d+)?)'
    r'(?:\s*[×xX]\s*(\d+(?:\.\d+)?))?'
    r'[\s-]*(?:centimeters?|centimetres?|cm)\b',
    re.IGNORECASE,
)

# Millimetres
_MM_IND_RE = re.compile(
    r'(?<![.\d])(\d+(?:\.\d+)?)[\s-]*(?:millimeters?|millimetres?|mm)\b',
    re.IGNORECASE,
)
_MM_PAIR_RE = re.compile(
    r'(?<![.\d])(\d+(?:\.\d+)?)\s*[×xX]\s*(\d+(?:\.\d+)?)'
    r'(?:\s*[×xX]\s*(\d+(?:\.\d+)?))?'
    r'[\s-]*(?:millimeters?|millimetres?|mm)\b',
    re.IGNORECASE,
)

# Feet and yards — individual only (pairs are unusual); yards will be
# converted to inches so imperial dims stay in one comparable unit.
_FEET_IND_RE = re.compile(
    r'(?<![.\d])(\d+(?:\.\d+)?)[\s-]*(?:feet|foot|ft\.?)\b',
    re.IGNORECASE,
)
_YARD_IND_RE = re.compile(
    r'(?<![.\d])(\d+(?:\.\d+)?)[\s-]*(?:yards?|yds?\.?)\b',
    re.IGNORECASE,
)

# Unambiguous color words used for contradiction detection.
# Kept deliberately narrow — words like "cream", "olive", "tan", "lime",
# "coral", "amber", "peach" are excluded because they appear routinely as
# product type / scent / ingredient names (hand cream, olive oil, peach
# scent) and would cause false positives.
_COLOR_WORDS: frozenset[str] = frozenset({
    "red", "blue", "green", "yellow", "orange", "purple", "pink",
    "black", "white", "gray", "grey", "brown",
    "navy", "teal", "turquoise", "maroon", "burgundy",
    "lavender", "violet", "indigo", "cyan", "magenta",
    "khaki", "charcoal", "blonde", "brunette",
})


def _s(value: Any) -> str:
    """Coerce to a trimmed string. Handles None and non-string types."""
    if value is None:
        return ""
    return str(value).strip()


def _pack_count(text: str) -> int:
    """
    Extract a count/pack number from a product title. Returns 1 if none found.
    Handles: 'pack of N', 'box of N', 'N count', 'N-count', 'Nct', 'N pack',
             'N pk', 'N piece', 'N pcs', etc.
    """
    m = _PACK_RE.search(text)
    if not m:
        return 1
    return int(next(g for g in m.groups() if g is not None))


def _extract_volume_ml(text: str) -> float | None:
    """
    Extract the primary volume/weight size from a product title and return it
    normalised to millilitres (oz → ml) so two titles can be compared.

    Pack-count tokens are stripped first so "6-Pack 2oz" doesn't confuse the
    parser into reading "6" as a size value.

    Returns None when no parseable size is found.
    """
    # Remove pack-count tokens before size extraction
    clean = _PACK_RE.sub(' ', text.lower())
    m = _SIZE_RE.search(clean)
    if not m:
        return None
    val = float(m.group(1))
    unit = re.sub(r'[\s.]+', '', m.group(2).lower())  # "fl oz" → "floz"
    if unit in ('oz', 'ounce', 'ounces', 'floz', 'fluidounce', 'fluidounces'):
        return val * 29.5735
    if unit in ('ml', 'milliliter', 'milliliters'):
        return val
    if unit in ('l', 'liter', 'liters', 'litre', 'litres'):
        return val * 1000.0
    if unit in ('gal', 'gallon', 'gallons'):
        return val * 3785.41
    return None


def _extract_weight_g(text: str) -> float | None:
    """
    Extract a weight value from text and return it normalised to grams.
    Handles lb/lbs/pounds and kg/kilograms.  Returns None when not found.
    Kept separate from volume extraction to avoid comparing different dimensions.
    """
    clean = _PACK_RE.sub(' ', text.lower())
    m = _WEIGHT_RE.search(clean)
    if not m:
        return None
    val = float(m.group(1))
    unit = re.sub(r'[\s.]+', '', m.group(2).lower())
    if unit in ('lb', 'lbs', 'pound', 'pounds'):
        return val * 453.592
    if unit in ('kg', 'kilogram', 'kilograms'):
        return val * 1000.0
    return None


def _extract_inches(text: str) -> float | None:
    """Extract the first inch measurement (number followed by " or ″)."""
    clean = _PACK_RE.sub(' ', text.lower())
    m = _INCH_RE.search(clean)
    return float(m.group(1)) if m else None


def _extract_yards(text: str) -> float | None:
    """Extract the first yard measurement from text."""
    clean = _PACK_RE.sub(' ', text.lower())
    m = _YARD_RE.search(clean)
    return float(m.group(1)) if m else None


def _within_10pct(a: float, b: float) -> bool:
    """True when a and b are within 10 % of each other."""
    if min(a, b) <= 0:
        return a == b
    return max(a, b) / min(a, b) <= 1.10


def _prep(text: str) -> str:
    """Lower-case, normalise fractions, strip pack-count tokens."""
    return _PACK_RE.sub(' ', _normalize_fractions(text.lower()))


def _collect_from_pairs(clean: str, pair_re: re.Pattern, scale: float = 1.0) -> list[float]:
    """Return all captured dim values (scaled) from a multi-group pair regex."""
    out: list[float] = []
    for m in pair_re.finditer(clean):
        out.extend(float(g) * scale for g in m.groups() if g is not None)
    return out


def _best_dims(pair_dims: list[float], individual: list[float]) -> list[float]:
    """
    Return whichever list covers more dimensions.
    Pair patterns capture N x M unit constructs where each number lacks its
    own unit marker; individual patterns capture the per-number form.
    When counts are equal the pair result is preferred (it was explicitly
    recognised as a dimension expression).
    """
    return sorted(pair_dims) if len(pair_dims) >= len(individual) else sorted(individual)


def _extract_imperial_in(text: str) -> list[float]:
    """
    Extract all imperial linear measurements from *text*, returned as
    inch-equivalent floats (feet × 12, yards × 36).

    Handles fraction formats ("1 3/4 in.", "2-3/8"", "¾") via pre-normalisation.

    Three extraction strategies are tried; the one that captures the most
    dimensions wins:
      A. Trailing-unit pair  —  "6 X 8 Inch"  /  "2.375 x 2.75 in."
      B. Mixed pair          —  "2.125 x 4.75\""  (last num carries ")
      C. Individual          —  "3\""  "3 inch"  "1.75 in."
    Feet and yards are appended after.
    """
    clean = _prep(text)

    pair_dims: list[float] = []
    pair_dims.extend(_collect_from_pairs(clean, _INCH_PAIR_RE))
    pair_dims.extend(_collect_from_pairs(clean, _INCH_MIXED_PAIR_RE))

    individual = [float(v) for v in _INCH_IND_RE.findall(clean)]

    dims = _best_dims(pair_dims, individual)

    # Feet → inches, yards → inches; appended then re-sorted
    dims.extend(float(v) * 12.0 for v in _FEET_IND_RE.findall(clean))
    dims.extend(float(v) * 36.0 for v in _YARD_IND_RE.findall(clean))
    return sorted(dims)


def _extract_metric_mm(text: str) -> list[float]:
    """
    Extract all metric linear measurements from *text*, returned as
    millimetre-equivalent floats (cm × 10).
    """
    clean = _prep(text)

    pair_dims: list[float] = []
    pair_dims.extend(_collect_from_pairs(clean, _CM_PAIR_RE, scale=10.0))
    pair_dims.extend(_collect_from_pairs(clean, _MM_PAIR_RE, scale=1.0))

    individual_cm = [float(v) * 10.0 for v in _CM_IND_RE.findall(clean)]
    individual_mm = [float(v) for v in _MM_IND_RE.findall(clean)]
    individual = sorted(individual_cm + individual_mm)

    return _best_dims(pair_dims, individual)


def _linear_dims_mismatch(src_title: str, amz_title: str) -> bool:
    """
    Return True when both titles have linear dimensions in the same unit
    system (imperial or metric) and those dimensions disagree by > 10 %.

    Imperial system: inches, feet, yards — all converted to inch equivalents.
    Metric system:   mm, cm              — all converted to mm equivalents.
    Units are never cross-compared (inches vs cm → skip).

    Only fires when both sides have the SAME number of dimensions in the
    same system.  Different dimension counts are treated as ambiguous and
    skipped to avoid false positives.

    Fraction normalisation is applied first so that all of the following
    are correctly parsed before extraction:

      Vendor  "1 3/4 in. x 1 3/4 in."        → [1.75, 1.75] in
      Amazon  "2-3/8\\" x 2-3/4\\""           → [2.375, 2.75] in → MISMATCH
      Amazon  "2 3/8 x 2 3/4 Inch"            → [2.375, 2.75] in → MISMATCH
      Amazon  "6 X 8 Inch"                    → [6.0, 8.0] in   → MISMATCH
      Amazon  "2.125 x 4.75\\""               → [2.125, 4.75] in → MISMATCH
      Amazon  "1 ¾\\" x 1 ¾\\""              → [1.75, 1.75] in → match
      Amazon  "10 cm x 12 cm"                 → metric only      → skip (diff system)
    """
    for extractor in (_extract_imperial_in, _extract_metric_mm):
        src_dims = extractor(src_title)
        amz_dims = extractor(amz_title)
        if not src_dims or not amz_dims:
            continue
        if len(src_dims) != len(amz_dims):
            continue
        if any(not _within_10pct(s, a) for s, a in zip(src_dims, amz_dims)):
            return True
    return False


def _size_mismatch(src_title: str, amz_title: str, amz_size_attr: str = "") -> bool:
    """
    Return True when vendor and Amazon both specify a size and they differ by
    more than 10 %.

    Checks, in order:
      1. Volume (oz, fl oz, ml, l, gal) — normalised to ml for comparison.
      2. Weight (lb, kg) — normalised to grams.
      3. Multi-dimensional linear sizes — imperial (inches, feet, yards all
         in inch-equiv.) and metric (mm, cm in mm-equiv.) each checked within
         their own system; units never cross-compared.  Fractions ("1 3/4 in.",
         "2-3/8"", "¾") normalised before extraction.  All dimensions extracted
         as sorted lists, compared element-by-element with 10 % tolerance.

    A volume from one side is never compared against a weight from the other.
    The Amazon `attributes.size` field is used as fallback when the Amazon
    title yields no parseable volume/weight.

    A 10 % tolerance covers minor rounding (355 ml ≈ 12 oz).  Returns False
    when either side has no parseable size in a given category.

    Pack-count adjustment: the SP-API size attribute sometimes reports the
    total weight for a multipack (e.g. "9.51 oz" for a 3-count pack of 3.17 oz
    bars) rather than the per-unit size.  If dividing the Amazon size by its
    pack count brings the two sizes into alignment (within 10 %), the function
    returns False — the sizes match per-unit and the pack difference (if any)
    is already handled by pack_mismatch.
    """
    def _confirms_match(src: float, amz: float) -> bool:
        """True when src and amz are within 10 %, or amz/pack_count is within 10 %."""
        if _within_10pct(src, amz):
            return True
        amz_pack = _pack_count(amz_title)
        if amz_pack > 1:
            per_unit = amz / amz_pack
            if per_unit > 0 and _within_10pct(src, per_unit):
                return True
        return False

    # --- Volume comparison ---
    src_ml = _extract_volume_ml(src_title)
    if src_ml is not None and src_ml > 0:
        amz_ml_t = _extract_volume_ml(amz_title)
        amz_ml_a = _extract_volume_ml(amz_size_attr) if amz_size_attr else None

        if amz_ml_t is not None and amz_ml_t > 0:
            if _confirms_match(src_ml, amz_ml_t):
                return False
            # Title mismatches — check if the structured attr overrides it.
            # Amazon titles sometimes embed a different variant's size while
            # the SP-API attribute correctly reflects the listed product.
            if amz_ml_a is not None and amz_ml_a > 0 and _confirms_match(src_ml, amz_ml_a):
                return False
            return True
        if amz_ml_a is not None and amz_ml_a > 0:
            return not _confirms_match(src_ml, amz_ml_a)

    # --- Weight comparison (lb / kg) ---
    src_g = _extract_weight_g(src_title)
    if src_g is not None and src_g > 0:
        amz_g_t = _extract_weight_g(amz_title)
        amz_g_a = _extract_weight_g(amz_size_attr) if amz_size_attr else None

        if amz_g_t is not None and amz_g_t > 0:
            if _confirms_match(src_g, amz_g_t):
                return False
            if amz_g_a is not None and amz_g_a > 0 and _confirms_match(src_g, amz_g_a):
                return False
            return True
        if amz_g_a is not None and amz_g_a > 0:
            return not _confirms_match(src_g, amz_g_a)

    # --- Multi-dimensional linear size comparison ---
    # Handles all of: inches ("/ ″/ inch/ in.), feet, yards (→ inch equiv.),
    # cm, mm.  Imperial and metric are compared within their own system only.
    # Fractions ("1 3/4 in.", "2-3/8"", "¾") are normalised before extraction.
    # See _linear_dims_mismatch / _extract_imperial_in / _extract_metric_mm.
    if _linear_dims_mismatch(src_title, amz_title):
        return True

    return False


def _size_match(src_title: str, amz_title: str, amz_size_attr: str = "") -> bool:
    """
    Return True when BOTH sides specify a size (volume or weight) and the
    values agree within 10 %.  This is the positive confirmation counterpart
    to `_size_mismatch` — it is used to certify "same item" when paired with a
    brand or UPC match.

    Volume is checked first (oz/fl oz/ml/l/gal → ml), then weight (lb/kg → g).
    A multipack on the Amazon side is divided by its pack count so a per-unit
    vendor size ("3.17 oz") still confirms against a total-pack Amazon size
    ("9.51 oz", 3-count).  The structured `attributes.size` is used as a
    fallback when the Amazon title carries no parseable size.

    Returns False when either side lacks a parseable size in the category
    being compared — absence of evidence is never treated as a match.
    """
    def _confirms(src: float, amz: float) -> bool:
        if src <= 0 or amz <= 0:
            return False
        if _within_10pct(src, amz):
            return True
        amz_pack = _pack_count(amz_title)
        if amz_pack > 1:
            per_unit = amz / amz_pack
            if per_unit > 0 and _within_10pct(src, per_unit):
                return True
        return False

    # --- Volume ---
    src_ml = _extract_volume_ml(src_title)
    if src_ml is not None and src_ml > 0:
        amz_ml = _extract_volume_ml(amz_title)
        if amz_ml is None and amz_size_attr:
            amz_ml = _extract_volume_ml(amz_size_attr)
        if amz_ml is not None and _confirms(src_ml, amz_ml):
            return True

    # --- Weight ---
    src_g = _extract_weight_g(src_title)
    if src_g is not None and src_g > 0:
        amz_g = _extract_weight_g(amz_title)
        if amz_g is None and amz_size_attr:
            amz_g = _extract_weight_g(amz_size_attr)
        if amz_g is not None and _confirms(src_g, amz_g):
            return True

    # --- Multi-dimensional linear sizes (inches / cm / mm) ---
    # Both sides must carry the SAME number of dimensions in the SAME unit
    # system and every dimension must agree within 10 %.  Handles medical
    # supplies sized in inches (gauze, tape, dressings) where volume/weight
    # don't apply.
    for extractor in (_extract_imperial_in, _extract_metric_mm):
        src_dims = extractor(src_title)
        amz_dims = extractor(amz_title)
        if not src_dims or not amz_dims:
            continue
        if len(src_dims) != len(amz_dims):
            continue
        if all(_within_10pct(s, a) for s, a in zip(src_dims, amz_dims)):
            return True

    return False


def _effective_pack(src_title: str, amz_title: str) -> tuple[int, bool]:
    """
    Compute how many vendor units the Amazon listing represents, and whether
    that is a mismatch (i.e. more than one vendor unit per Amazon listing).

    Logic:
      - If the vendor title already states a count (e.g. "36 count crayons"),
        that count is the vendor's base unit.
      - The Amazon title is parsed for its own count.
      - effective = amz_count / src_count  (integer division when evenly divisible)
      - pack_mismatch = effective > 1

    Examples
    --------
    Vendor "36 count crayons"  + Amazon "crayons pack of 36"  → (1, False)
    Vendor "36 count crayons"  + Amazon "crayons pack of 72"  → (2, True)
    Vendor "hair gel 23.5oz"   + Amazon "hair gel pack of 6"  → (6, True)
    Vendor "hair gel 23.5oz"   + Amazon "hair gel 23.5oz"     → (1, False)
    """
    src_count = _pack_count(src_title)
    amz_count = _pack_count(amz_title)

    if src_count > 1:
        if amz_count == 0 or amz_count == src_count:
            # Amazon matches vendor unit exactly — single-unit listing
            return 1, False
        if amz_count > src_count and amz_count % src_count == 0:
            ep = amz_count // src_count
            return ep, ep > 1
        # Counts differ but not a clean multiple — show Amazon count as-is
        return amz_count if amz_count > 0 else 1, amz_count > src_count
    else:
        # Vendor is a single unit (no count specified)
        return (amz_count, amz_count > 1) if amz_count > 1 else (1, False)


def _lower(value: Any) -> str:
    return _s(value).lower()


_MALE_TERMS   = {"men", "mens", "man", "male", "boy", "boys", "him", "his"}
_FEMALE_TERMS = {"women", "womens", "woman", "female", "girl", "girls", "her", "hers"}

def _gender_words(title: str) -> set[str]:
    """Extract lowercase alpha-only words, stripping punctuation like commas."""
    return set(re.findall(r"[a-z]+", re.sub(r"[''`]", "", title.lower())))


# ---------------------------------------------------------------------------
# Unit-count mismatch
# ---------------------------------------------------------------------------

def _unit_count_mismatch(src_title: str, amz_title: str) -> bool:
    """
    Return True when both titles explicitly specify a unit count AND the counts
    differ by more than 10 %.

    In CPG and medical, the unit count printed on the package (e.g. "80 Count"
    for trash bags, "75 Count" for wipes, "100 Count" for lancets) identifies a
    specific SKU.  A vendor listing "80 Count" matched against an Amazon listing
    "110 Count" are different products — a hard reject.

    Only fires when BOTH sides have an explicit count > 1 so that a
    no-explicit-count vendor item is never rejected against a multi-pack Amazon
    listing (that scenario is already handled by pack_mismatch).

    The 10 % tolerance covers rounding artefacts ("48 count" vs "50 count" on a
    differently-titled but otherwise identical product).  Clean-integer multiples
    (36 vs 72) are NOT exempted — "72-count" is a different SKU from "36-count"
    even when they are the same product in different pack sizes.
    """
    src_n = _pack_count(src_title)
    amz_n = _pack_count(amz_title)
    if src_n <= 1 or amz_n <= 1:
        # One or both sides have no explicit count: leave pack_mismatch to handle it.
        return False
    return not _within_10pct(float(src_n), float(amz_n))


# ---------------------------------------------------------------------------
# Scent / variant mismatch
# ---------------------------------------------------------------------------

# Common scent, fragrance, and flavour words used in CPG product naming.
# Deliberately conservative — only unambiguous descriptor words.
# Words like "clean", "fresh", "original" are excluded because they are also
# used as generic product line qualifiers and cause false positives.
_SCENT_WORDS: frozenset[str] = frozenset({
    "lavender", "vanilla", "citrus", "lemon", "lime", "orange", "grapefruit",
    "mint", "peppermint", "spearmint", "eucalyptus", "pine", "cedar", "spruce",
    "apple", "cherry", "strawberry", "raspberry", "blueberry", "mango", "peach",
    "coconut", "tropical", "melon", "watermelon", "grape", "pomegranate", "fig",
    "rose", "jasmine", "floral", "chamomile", "aloe", "bamboo", "sandalwood",
    "freesia", "lilac", "magnolia", "gardenia", "hibiscus", "orchid", "peony",
    "blossom", "tahitian", "hawaiian", "mediterranean", "honeysuckle", "ylang",
    "bergamot", "basil", "sage", "thyme", "rosemary", "cucumber",
    "ocean", "marine", "aqua", "glacier", "arctic", "rain", "breeze", "splash",
    "burst", "dew", "wave", "tide", "mist",
    "cinnamon", "cardamom", "clove", "ginger", "nutmeg", "pumpkin", "caramel",
    "cotton", "linen", "meadow", "garden", "wildflower", "sunflower",
})


def _scent_mismatch(src_title: str, amz_title: str) -> bool:
    """
    Return True when both titles carry scent/variant words AND each side has
    at least one scent word the other side does not (i.e. neither set is a
    subset of the other).

    Logic
    ─────
    Fire when: (src_scents - amz_scents) ≠ ∅  AND  (amz_scents - src_scents) ≠ ∅
    Equivalently: not (src_scents ⊆ amz_scents or amz_scents ⊆ src_scents)

    This catches cases where the scents overlap partially but still describe
    distinctly different variants:

      "Tahitian Grapefruit Splash"  →  {tahitian, grapefruit, splash}
      "Grapefruit and Orange Blossom" →  {grapefruit, orange, blossom}
      src - amz = {tahitian, splash} ≠ ∅
      amz - src = {orange, blossom}  ≠ ∅
      → MISMATCH ✓

    Safe cases that do NOT fire:
      "Lavender"  vs  "Lavender Vanilla"  →  {lavender} ⊆ {lavender, vanilla}  → no mismatch
      "Lavender"  vs  "Lavender"          →  equal sets, both subsets             → no mismatch
      "Lavender Citrus" vs "Lavender Citrus Burst" → {lavender,citrus} ⊆ bigger → no mismatch

    Only fires when BOTH sides have scent words; a vendor title with no
    fragrance descriptor is left to normal scoring.
    """
    src_words = set(re.findall(r"[a-z]+", src_title.lower()))
    amz_words = set(re.findall(r"[a-z]+", amz_title.lower()))

    src_scents = src_words & _SCENT_WORDS
    amz_scents = amz_words & _SCENT_WORDS

    if not src_scents or not amz_scents:
        return False  # at least one side has no scent descriptor

    # Mismatch when each side has unique scent words the other lacks.
    src_only = src_scents - amz_scents
    amz_only = amz_scents - src_scents
    return bool(src_only) and bool(amz_only)


# ---------------------------------------------------------------------------
# Shade / colour-variant mismatch  (hair colour, cosmetics, etc.)
# ---------------------------------------------------------------------------

# Tonal qualifiers that distinguish shades along a light↔dark axis.  Kept tight
# and unambiguous so they don't fire on unrelated copy.  Deliberately excludes
# "natural", "warm", "cool", "rich", "soft" — those appear too often as generic
# marketing words.
_TONE_WORDS: frozenset[str] = frozenset({
    "light", "lightest", "lighter", "medium", "dark", "darkest", "darker",
    "deep", "deepest",
})

# Explicit shade codes: "#46", "#46a", "No. 47", "No 47", "Shade 3N",
# "Color 46", and a bare number that immediately precedes a tone word
# ("48 Dark Chestnut").  All numeric groups are collected and compared.
_SHADE_NUM_RE = re.compile(
    r'#\s*(\d{1,3}[a-z]?)\b'
    r'|\bno\.?\s*(\d{1,3}[a-z]?)\b'
    r'|\bshade\s*(\d{1,3}[a-z]?)\b'
    r'|\bcolou?r\s+(\d{1,3}[a-z]?)\b'
    r'|\b(\d{1,3})\s+(?:light|lightest|medium|dark|darkest|deep|ash|blonde|blond|brown|chestnut|auburn|burgundy)\b',
    re.IGNORECASE,
)


def _shade_codes(title: str) -> set[str]:
    """Collect every explicit shade code found in *title* (lower-cased)."""
    out: set[str] = set()
    for m in _SHADE_NUM_RE.finditer(title):
        for g in m.groups():
            if g:
                out.add(g.lower())
    return out


def _shade_mismatch(src_title: str, amz_title: str) -> bool:
    """
    Return True when both titles identify a specific shade / colour-variant and
    those shades differ.  Targets shade-keyed products — hair colour, cosmetics
    — where brand, size, and product type are identical across the entire line
    and ONLY the shade distinguishes one SKU from another.

    Two independent signals; either one firing is a mismatch:
      1. Explicit shade CODES differ — Bigen "#46" vs "#48", "No. 7" vs "No. 5".
      2. Tonal qualifiers are present on BOTH sides and are disjoint —
         "Light Chestnut" vs "Dark Chestnut", "Medium" vs "Light".

    Only fires when BOTH sides carry the signal, so an item with no shade
    descriptor (most CPG) is never penalised.  This is what stops e.g.
    Bigen #46 Light Chestnut from being matched to #48 Dark Chestnut.
    """
    src_codes = _shade_codes(src_title)
    amz_codes = _shade_codes(amz_title)
    if src_codes and amz_codes and src_codes.isdisjoint(amz_codes):
        return True

    src_words = set(re.findall(r"[a-z]+", src_title.lower()))
    amz_words = set(re.findall(r"[a-z]+", amz_title.lower()))
    src_tone = src_words & _TONE_WORDS
    amz_tone = amz_words & _TONE_WORDS
    if src_tone and amz_tone and src_tone.isdisjoint(amz_tone):
        return True

    return False


def _gender_mismatch(title_a: str, title_b: str) -> bool:
    """
    Return True only when BOTH titles carry explicit gender markers that
    contradict each other (one male, one female).

    When only one title specifies gender (e.g. Amazon says 'for Women' but
    the vendor title is unisex), we do NOT flag it — the match is left to
    the normal title similarity score and may still reach Review or Approved.

    Titles that contain BOTH male and female terms ("for Women and Men",
    "Boys & Girls") are treated as unisex and never trigger a mismatch.
    """
    ta = _gender_words(title_a)
    tb = _gender_words(title_b)
    a_male   = bool(ta & _MALE_TERMS)
    a_female = bool(ta & _FEMALE_TERMS)
    b_male   = bool(tb & _MALE_TERMS)
    b_female = bool(tb & _FEMALE_TERMS)
    # If either title explicitly covers both genders, treat it as unisex.
    if (a_male and a_female) or (b_male and b_female):
        return False
    # Only act when both sides make an explicit gender claim that contradict
    if not ((a_male or a_female) and (b_male or b_female)):
        return False
    return (a_male and b_female) or (a_female and b_male)


def _color_mismatch(
    src_title: str,
    amz_title: str,
    amz_color_attr: str,
    amz_full_text: str = "",
) -> bool:
    """
    Return True when both sides name a color and they clearly contradict.

    Amazon color source: structured SP-API color attribute ONLY.
    Falling through to the Amazon title or description produces too many false
    positives for CPG products where color words appear as product-line names
    ("Red Collection") or ingredient/scent descriptors ("Black Currant",
    "Green Tea").  When the SP-API color attribute is empty, we assume color
    is not a meaningful differentiator for this product and return False.

    Examples that DO fire:
      vendor "Crocs Classic Clog Black"  + amz color attr "White"    → True

    Examples that do NOT fire:
      vendor "Old Spice Red Collection"  + amz attr ""               → False
        (no structured color attr → no flag)
      vendor "Eucerin Cream"             + amz attr "Blue"           → False
        (vendor has no unambiguous color word → no flag)
      vendor "Crocs Black"               + amz attr "Black"          → False
        (same color → no contradiction)
    """
    src_colors = set(re.findall(r'[a-z]+', src_title.lower())) & _COLOR_WORDS
    if not src_colors:
        return False

    # Amazon colours: prefer the structured SP-API color attribute; if it is
    # empty (very common), fall back to the Amazon TITLE so an explicit colour
    # in the title ("...Black", "...White") is still compared.  We only fire
    # when BOTH sides name a colour AND the sets are disjoint, which keeps the
    # CPG false-positives the attribute-only rule was guarding against in check
    # ("Black" vs "Black/Gray" → no fire) while catching "Black" vs "White".
    amz_src_text = amz_color_attr if amz_color_attr.strip() else (amz_title or "")
    amz_colors = set(re.findall(r'[a-z]+', amz_src_text.lower())) & _COLOR_WORDS
    if not amz_colors:
        return False
    return src_colors.isdisjoint(amz_colors)


# ---------------------------------------------------------------------------
# Apparel / garment size mismatch  (S / M / L / XL …)
# ---------------------------------------------------------------------------
# Distinct from _size_mismatch, which compares physical dimensions (oz, ml,
# inches).  This compares clothing/wearable sizes — "X-Large" vs "Medium",
# "Small/Medium" vs "Large/X-Large" — which are a different SKU and a hard no.
# Longest tokens first so "X-Large" matches the XL form, not bare "Large".
_GARMENT_SIZE_RE = re.compile(
    r'\b('
    r'xxx-?large|3x-?large|xxxl|3xl'
    r'|xx-?large|2x-?large|xxl|2xl'
    r'|x-?large|extra[\s-]?large|xl'
    r'|xx-?small|2x-?small|xxs'
    r'|x-?small|extra[\s-]?small|xs'
    r'|small|medium|large'
    r')\b',
    re.IGNORECASE,
)

_GARMENT_NORM = {
    "xxs": "XXS", "xxsmall": "XXS", "2xsmall": "XXS",
    "xs": "XS", "xsmall": "XS", "extrasmall": "XS",
    "small": "S",
    "medium": "M",
    "large": "L",
    "xl": "XL", "xlarge": "XL", "extralarge": "XL",
    "xxl": "XXL", "2xl": "XXL", "xxlarge": "XXL", "2xlarge": "XXL",
    "xxxl": "XXXL", "3xl": "XXXL", "xxxlarge": "XXXL", "3xlarge": "XXXL",
}


def _garment_sizes(title: str) -> set[str]:
    """Collect normalized apparel sizes (S/M/L/XL/…) found in *title*."""
    out: set[str] = set()
    for m in _GARMENT_SIZE_RE.finditer(title or ""):
        key = re.sub(r"[\s-]", "", m.group(1).lower())
        norm = _GARMENT_NORM.get(key)
        if norm:
            out.add(norm)
    return out


def _apparel_size_mismatch(src_title: str, amz_title: str) -> bool:
    """
    Return True when BOTH titles state an apparel/garment size and the two size
    sets are disjoint — e.g. vendor "X-Large" vs Amazon "Medium" (a different
    SKU → hard reject).  Overlapping ranges ("Large" vs "Large/X-Large", which
    share "L") do NOT fire.  Only fires when both sides carry a size, so an
    item with no garment size is never penalised.
    """
    src = _garment_sizes(src_title)
    amz = _garment_sizes(amz_title)
    if not src or not amz:
        return False
    return src.isdisjoint(amz)


# ---------------------------------------------------------------------------
# Brand normalisation + equality
# ---------------------------------------------------------------------------

# Corporate/legal suffixes and generic descriptors that do NOT distinguish one
# brand from another.  "Cardinal Health" and "Cardinal" are the same brand;
# "Medline Industries" and "Medline" are the same brand.  Matched as whole
# tokens only (never substrings) so real brand words are never stripped.
_BRAND_NOISE_WORDS: frozenset[str] = frozenset({
    "inc", "incorporated", "llc", "llp", "ltd", "limited", "corp",
    "corporation", "co", "company", "gmbh", "ag", "sa", "plc", "kg",
    "health", "healthcare", "medical", "pharma", "pharmaceutical",
    "pharmaceuticals", "labs", "laboratories", "laboratory",
    "industries", "industrial", "products", "product", "brands", "brand",
    "group", "holdings", "international", "intl", "usa", "global", "the",
})


def _normalize_brand(brand: str) -> str:
    """
    Normalise a brand string for equality comparison:
      - lowercase, drop apostrophes ("Palmer's" → "palmers")
      - split on any non-alphanumeric run
      - drop corporate/legal suffixes and generic descriptors (whole tokens)
      - concatenate remaining tokens so spacing no longer matters
        ("Shea Moisture" → "sheamoisture", "SheaMoisture" → "sheamoisture")

    Returns '' when the brand is empty.  If every token is a noise word
    (e.g. a brand literally named "Health"), the raw alphanumeric tokens are
    kept so the comparison still has something to work with.
    """
    if not brand:
        return ""
    s = re.sub(r"[''`]", "", brand.lower())
    s = re.sub(r"[^a-z0-9]+", " ", s)
    raw_tokens = [t for t in s.split() if t]
    tokens = [t for t in raw_tokens if t not in _BRAND_NOISE_WORDS]
    if not tokens:
        tokens = raw_tokens
    return "".join(tokens)


def _brands_match(src_brand: str, amz_brand: str) -> bool:
    """
    True when two brand strings refer to the same brand once normalised.

    Catches the three real-world variations the vendor catalog and Amazon
    disagree on:
      - spacing / punctuation:  "Shea Moisture"  == "SheaMoisture"
      - corporate suffix:       "Cardinal Health" == "Cardinal"
      - minor spelling variant: "Moleskine"      == "Moleskin"  (fuzzy ≥ 88)

    Deliberately conservative: a bare 4-letter brand like "Dove" is never
    matched by the prefix rule (min length 5) so it can't collide with
    unrelated brands such as "Doverland".
    """
    ns = _normalize_brand(src_brand)
    na = _normalize_brand(amz_brand)
    if not ns or not na:
        return False
    if ns == na:
        return True
    # Prefix containment for compound brands ("SheaMoisture" vs
    # "SheaMoistureOrganic") — require ≥ 5 chars and a real prefix relationship.
    if len(ns) >= 5 and len(na) >= 5 and (na.startswith(ns) or ns.startswith(na)):
        return True
    # Fuzzy on the normalised forms for spelling variants.
    return _ratio(ns, na) >= 88


def _ratio(a: str, b: str) -> int:
    if not a or not b or fuzz is None:
        return 0
    return int(fuzz.ratio(a, b))


def _extract_base_mpn(mpn: str) -> str:
    """Strip trailing single-letter suffix from MPN to get base form.
    '1113-M' -> '1113', 'KE1961W' -> 'KE1961', '5000-L' -> '5000'
    """
    return re.sub(r'-?[A-Za-z]$', '', mpn.strip())


def _mpn_variation_score(src_mpn: str, amz_mpn: str) -> float:
    """Return 0-100 similarity between two MPNs.

    Uses dash-normalized fuzzy ratio, base-form comparison (strip trailing
    letter suffixes), and containment check — so '1113-M' ≈ '1113' scores ≥70.
    """
    if not src_mpn or not amz_mpn:
        return 0.0
    s = src_mpn.lower().replace("-", "")
    a = amz_mpn.lower().replace("-", "")
    base_score = float(_ratio(s, a))

    s_base = _extract_base_mpn(s)
    a_base = _extract_base_mpn(a)
    base_form_score = float(_ratio(s_base, a_base)) if (s_base != s or a_base != a) else 0.0

    containment_score = 80.0 if (s and a and (s in a or a in s)) else 0.0

    return max(base_score, base_form_score, containment_score)


def _partial_ratio(a: str, b: str) -> int:
    if not a or not b or fuzz is None:
        return 0
    return int(fuzz.partial_ratio(a, b))


def _token_set_ratio(a: str, b: str) -> int:
    if not a or not b or fuzz is None:
        return 0
    return int(fuzz.token_set_ratio(a, b))


# ---------------------------------------------------------------------------
# Model / part-number confirmation
# ---------------------------------------------------------------------------

def _model_in_title(src_mpn: str, amz_title: str) -> bool:
    """
    True when the vendor's part/model number appears verbatim in the Amazon
    title (compared alphanumerics-only, so "1530-1" matches "...Tape 1530-1...").

    Requires a SPECIFIC model token — ≥ 4 alphanumeric chars including at least
    one digit — so generic SKUs ("1", "AB", "Kit") never trigger it.  An exact
    model number is the most reliable same-SKU signal there is: 3M "1530-1"
    matches Amazon "...1530-1..." but not "...1527-1..." (different product).
    """
    s = re.sub(r"[^a-z0-9]", "", (src_mpn or "").lower())
    if len(s) < 4 or not any(c.isdigit() for c in s):
        return False
    a = re.sub(r"[^a-z0-9]", "", (amz_title or "").lower())
    return s in a


def _has_vol_or_weight(src_title: str, amz_title: str) -> bool:
    """True when either title states a parseable volume or weight — a reliable
    size signal (oz/ml/l/gal, lb/kg), as opposed to a linear dimension that is
    prone to parsing artifacts (width-only vs W×L, unit typos like '1 inc')."""
    for t in (src_title, amz_title):
        if _extract_volume_ml(t) is not None or _extract_weight_g(t) is not None:
            return True
    return False


# --------------------------------------------------------------------------- #
# calculate_confidence — 1:1 port of the reference scorer
# --------------------------------------------------------------------------- #


def calculate_confidence(
    source: dict,
    amazon: dict,
    additional_keywords: Iterable[str] | None = None,
    upc_search_hit: bool = False,
    extracted: dict | None = None,
    mpn_search_hit: bool = False,
    mode: str = "cpg",
) -> dict:
    """
    Return a breakdown of the confidence score for one source-to-Amazon pair.

    Shape:
        {
          "confidence_score": 0-100,
          "brand_score":      0-70,
          "product_type_similarity": 0-20,
          "mpn_score":        0-10,
          "upc_match":        bool,
        "brand_confirmed":  bool,
        "size_match":       bool,
        }
    """
    # Extracted brand (from GPT-4o-mini) is cleaner than raw catalog text —
    # prefer it when available.  extracted_product_type and model are injected
    # as additional keywords so the scorer can reward product-type matches.
    _ext = extracted or {}
    src_brand = _s(_ext.get("brand")) or _s(source.get("brand") or source.get("manufacturer"))

    _ext_kws: list[str] = [
        k for k in [_ext.get("product_type"), _ext.get("model")] if k
    ]
    if _ext_kws:
        additional_keywords = list(additional_keywords or []) + _ext_kws

    amz_brand = _s(amazon.get("brand") or amazon.get("manufacturer"))
    amz_title = _lower(amazon.get("title"))
    amz_desc = _lower(amazon.get("description"))

    # Flatten bullet points into the description haystack — the reference
    # project only had description_text but Amazon often keeps the useful
    # signal in bullets.
    bullets = amazon.get("bullet_points") or []
    if bullets:
        amz_desc = (amz_desc + " " + " ".join(_lower(b) for b in bullets)).strip()

    src_title = _s(source.get("title"))

    # ----- UPC match detection -----------------------------------------------
    # UPC match is a strong signal but NOT sufficient alone for verification.
    # It adds UPC_BONUS points on top of the normal brand/title score so that
    # both identifier AND content signals must agree for a "verified" verdict.
    # This catches Amazon catalog errors where a UPC is cross-attached to the
    # wrong product — those items get the bonus but score near-zero on brand
    # and title, landing in Review rather than silently auto-approving.
    src_upc = _s(source.get("upc"))
    amz_upc = _s(amazon.get("upc"))
    amz_ean = _s(amazon.get("ean"))
    upc_match = (
        upc_search_hit
        or (src_upc and amz_upc and src_upc == amz_upc)
        or (src_upc and amz_ean and src_upc == amz_ean)
    )

    # ----- Brand (BRAND_MAX pts) -------------------------------------------
    brand_score = 0.0
    brand_confirmed = False   # True when we are confident it is the same brand
    if src_brand:
        src_brand_lower = src_brand.lower()
        brand_in_title = bool(amz_title) and src_brand_lower in amz_title
        brand_in_field = bool(amz_brand) and src_brand_lower in amz_brand.lower()
        brand_in_desc  = bool(amz_desc)  and src_brand_lower in amz_desc
        if brand_in_title or brand_in_field or brand_in_desc:
            brand_score = BRAND_MAX
            brand_confirmed = True
        else:
            # Word-level exact match: any significant vendor brand word (≥4 chars)
            # exactly equals the Amazon brand field — handles multi-word vendor
            # brands like "Gojo Purell" where Amazon only says "PURELL".
            brand_word_exact = bool(amz_brand) and any(
                w == amz_brand.lower()
                for w in src_brand_lower.split() if len(w) >= 4
            )
            if brand_word_exact:
                brand_score = BRAND_MAX
                brand_confirmed = True
            elif _brands_match(src_brand, amz_brand):
                # Normalised / fuzzy brand equality — handles spacing
                # ("Shea Moisture" = "SheaMoisture"), corporate suffixes
                # ("Cardinal Health" = "Cardinal"), and minor spelling
                # variants ("Moleskine" = "Moleskin").  These are the same
                # brand and must score full marks.
                brand_score = BRAND_MAX
                brand_confirmed = True
            else:
                # Fuzzy fallback — require >= 70% similarity on best of 3 fields.
                best = max(
                    _ratio(src_brand_lower, amz_brand.lower()) if amz_brand else 0,
                    _partial_ratio(src_brand_lower, amz_title) if amz_title else 0,
                    _partial_ratio(src_brand_lower, amz_desc) if amz_desc else 0,
                )
                if best >= 70:
                    brand_score = (best / 100.0) * BRAND_MAX
                    if best >= 90:
                        brand_confirmed = True

    # ----- Additional-keyword boost (up to BRAND_MAX toward brand_score) ---
    if brand_score < BRAND_MAX and additional_keywords:
        for kw in additional_keywords:
            kw_lower = (kw or "").strip().lower()
            if not kw_lower:
                continue
            kw_in_title = bool(amz_title) and kw_lower in amz_title
            kw_in_field = bool(amz_brand) and kw_lower in amz_brand.lower()
            kw_in_desc  = bool(amz_desc)  and kw_lower in amz_desc
            if kw_in_title or kw_in_field or kw_in_desc:
                brand_score = max(brand_score, BRAND_MAX)
                break
            best = max(
                _ratio(kw_lower, amz_brand.lower()) if amz_brand else 0,
                _partial_ratio(kw_lower, amz_title) if amz_title else 0,
                _partial_ratio(kw_lower, amz_desc) if amz_desc else 0,
            )
            if best >= 70:
                brand_score = max(brand_score, (best / 100.0) * BRAND_MAX)

    # ----- Title similarity (TITLE_MAX pts) --------------------------------
    src_brand_str = src_brand
    src_mpn_str = _s(source.get("mpn") or source.get("itemid"))

    product_type_sim = 0.0
    if src_brand_str and src_mpn_str:
        sb = src_brand_str.lower()
        sm = src_mpn_str.lower()
        # brand+MPN both present in title OR description = auto TITLE_MAX.
        # Also fires when a significant brand word (≥4 chars) appears in the
        # Amazon brand field AND the item ID appears in the Amazon title —
        # handles multi-word brands like "Gojo Purell" where Amazon only says
        # "PURELL" in the brand field but the vendor SKU is in the title suffix.
        amz_brand_lower = amz_brand.lower() if amz_brand else ""
        sb_words_in_amz_brand = any(
            w in amz_brand_lower for w in sb.split() if len(w) >= 4
        )
        if (
            (amz_title and sb in amz_title and sm in amz_title)
            or (amz_desc and sb in amz_desc and sm in amz_desc)
            or (sb_words_in_amz_brand and amz_title and sm in amz_title)
            or (sb_words_in_amz_brand and amz_desc and sm in amz_desc)
        ):
            product_type_sim = TITLE_MAX

    best_title_ratio = 100 if product_type_sim == TITLE_MAX else 0
    if product_type_sim == 0.0 and src_title and amz_title:
        candidates = [
            src_title,
            f"{src_brand_str} {src_title}" if src_brand_str else None,
            f"{src_title} {src_mpn_str}" if src_mpn_str else None,
            f"{src_brand_str} {src_title} {src_mpn_str}"
            if src_brand_str and src_mpn_str
            else None,
        ]
        ratios = [_token_set_ratio(c.lower(), amz_title) for c in candidates if c]
        best_title_ratio = max(ratios) if ratios else 0
        product_type_sim = (best_title_ratio / 100.0) * TITLE_MAX

    # ----- MPN (MPN_MAX pts, dash-normalised) ------------------------------
    mpn_score = 0.0
    src_mpn = _s(source.get("mpn") or source.get("itemid"))
    amz_mpn = _s(amazon.get("mpn"))
    if src_mpn and amz_mpn:
        s = src_mpn.lower().replace("-", "")
        a = amz_mpn.lower().replace("-", "")
        ratio = _ratio(s, a)
        mpn_score = (ratio / 100.0) * MPN_MAX

    # When vendor brand is not explicitly provided:
    #   1. Try to infer it from the first word of the vendor title. If the
    #      inferred word appears in Amazon's brand field (exact or fuzzy) or
    #      in the Amazon title, award partial brand credit so same-brand items
    #      reach Review while cross-brand false matches stay Not Approved.
    #   2. Normalize title + MPN up to a 75-pt ceiling (well below the 90-pt
    #      auto-verify threshold) so a strong title alone can only reach Review.
    _TITLE_BRAND_SKIP = {
        "the", "a", "an", "for", "and", "of", "in", "on", "my", "our",
        "new", "best", "top", "pro", "premium", "original", "natural",
        "organic", "professional", "advanced", "extra", "ultra", "super",
        "pure", "fresh", "daily", "kids", "baby", "mini", "just",
    }
    if not src_brand:
        words = _s(source.get("title")).split()
        inferred = words[0].lower().rstrip(".,!") if words else ""
        if len(inferred) >= 3 and inferred not in _TITLE_BRAND_SKIP:
            ib_in_brand = amz_brand and inferred in amz_brand.lower()
            ib_in_title = amz_title and inferred in amz_title
            if ib_in_brand:
                brand_score = BRAND_MAX * 0.40   # 16 pts — exact hit in Amazon brand field
            elif ib_in_title:
                brand_score = BRAND_MAX * 0.25   # 10 pts — found in title but not brand field
            else:
                # Fuzzy fallback for "palmers"→"palmer's" style variants.
                fuzzy_best = max(
                    _ratio(inferred, amz_brand.lower()) if amz_brand else 0,
                    _partial_ratio(inferred, amz_brand.lower()) if amz_brand else 0,
                )
                if fuzzy_best >= 80:
                    brand_score = BRAND_MAX * 0.20  # 8 pts — fuzzy brand-field match

        # When the inferred brand matched AND title similarity is very high
        # (≥80%), treat it as a confirmed brand — upgrade to full BRAND_MAX.
        # This lets near-perfect title+brand matches cross the 90-pt threshold
        # and auto-verify instead of sitting just below it in Review.
        if brand_score > 0 and best_title_ratio >= 80:
            brand_score = BRAND_MAX

        # Scale title + MPN up to a 75-pt ceiling so brand-less items can reach
        # Review (≥35) but never auto-verify (≥90) on title alone.
        available = TITLE_MAX + MPN_MAX
        ceiling   = 75.0
        if available > 0 and available < ceiling:
            scale = ceiling / available
            product_type_sim = min(product_type_sim * scale, TITLE_MAX * scale)
            mpn_score        = min(mpn_score * scale, MPN_MAX * scale)

    total = min(brand_score + product_type_sim + mpn_score, 100.0)

    # UPC match adds 50 bonus points on top of the normal brand+title score.
    # UPC alone (50 pts) stays in Review; UPC + brand (50+40=90) auto-verifies;
    # UPC + decent title also reaches 90+. Wrong-product UPC hits score near 50.
    UPC_BONUS = 50.0
    if upc_match:
        total = min(total + UPC_BONUS, 100.0)

    # MPN search hit bonus — applied when the ASIN was found via Item ID / MPN search.
    # Medical mode: MPN is the primary identifier, so a strong MPN match gets a large bonus.
    # CPG mode: UPC is primary, so MPN gets a smaller bonus.
    # All title vetting (size/pack/color/gender hard rejects) still applies after this bonus.
    # Guards:
    #   1. brand_score > 0 — cross-brand coincidental ID matches must not be boosted.
    #   2. best_title_ratio >= 25 — if the titles are almost completely unrelated the
    #      products are different types; no identifier bonus can save them.
    mpn_variation_score = 0.0
    if mpn_search_hit and brand_score > 0 and best_title_ratio >= 25:
        src_mpn_b = _s(source.get("mpn") or source.get("itemid"))
        amz_mpn_b = _s(amazon.get("mpn"))
        if src_mpn_b and amz_mpn_b:
            mpn_variation_score = _mpn_variation_score(src_mpn_b, amz_mpn_b)
        elif src_mpn_b and not amz_mpn_b:
            # Amazon's structured MPN field is empty — check if the catalog
            # item ID appears verbatim in the Amazon title (e.g. "Dawnmist
            # ASL02 2 oz After Shave Lotion" when catalog item ID = "ASL02").
            src_norm = src_mpn_b.lower().replace("-", "")
            amz_title_norm = (amz_title or "").replace("-", "")
            if src_norm in amz_title_norm:
                mpn_variation_score = 100.0
        mpn_hit_bonus = 0.0
        if mode == "medical":
            if mpn_variation_score >= 95:
                mpn_hit_bonus = 40.0
            elif mpn_variation_score >= 70:
                mpn_hit_bonus = 25.0
        else:  # cpg
            if mpn_variation_score >= 70:
                mpn_hit_bonus = 20.0
        if mpn_hit_bonus > 0:
            total = min(total + mpn_hit_bonus, 100.0)

    # Build Amazon's full text (description + bullets) for attribute checks.
    _amz_desc_str = _s(amazon.get("description"))
    _amz_bullets  = amazon.get("bullet_points") or []
    amz_full_text = " ".join(
        p for p in [_amz_desc_str] + [_s(b) for b in _amz_bullets] if p
    )
    amz_attrs      = amazon.get("attributes") or {}
    amz_size_attr  = _s(amz_attrs.get("size"))
    amz_color_attr = _s(amz_attrs.get("color"))

    # Model/part-number confirmation: same brand + the vendor's exact part
    # number printed in the Amazon title = the same SKU.  When that holds, a
    # pack/quantity difference (vendor "Each" vs Amazon "12 Rolls/Carton") is
    # just bundling and a linear "size mismatch" is a parsing artifact — both
    # are overridden below so the pair still verifies.  (e.g. 3M "1530-1" tape.)
    src_model = _s(source.get("mpn") or source.get("itemid"))
    model_confirmed = brand_confirmed and _model_in_title(src_model, _s(amazon.get("title")))

    # Detect multi-pack / bundle mismatches (same per-unit product, Amazon lists
    # a different bundle count — e.g. vendor case of 144 vs Amazon "3 Count").
    # Soft-cap at 80 → Review.  This is computed BEFORE the floors below so that
    # a confirmed same-item match (brand+size, brand+title, UPC, or exact model)
    # can override it: a pure pack/bundle difference does NOT change product
    # identity, so the item still belongs in Approved.  A genuine *unit-count*
    # SKU difference ("80 Count" vs "110 Count") is caught separately by
    # count_mismatch below and stays a hard reject.
    effective_pack, pack_mismatch = _effective_pack(src_title, _s(amazon.get("title")))
    if pack_mismatch and not model_confirmed:
        total = min(total, 80.0)

    # Full brand match + strong title similarity (≥ 80 %) → floor at verified.
    # Extra words like "1000/cs" or "Case of 1000" — or a pack/bundle difference
    # — shouldn't block approval when brand and core title tokens clearly agree.
    # Hard-reject caps (size / gender / colour / count / scent / shade) below
    # still override this.
    VERIFIED_FLOOR = 90.0
    if brand_score >= BRAND_MAX and best_title_ratio >= 80:
        total = max(total, VERIFIED_FLOOR)

    # ----- Same-item confirmation floors -----------------------------------
    # A confirmed brand match + a confirmed per-unit size match means the two
    # listings are the same physical product.  This overrides the pack soft-cap
    # above (bundle differences don't change identity) but still runs BEFORE the
    # hard-reject caps below, so a genuine contradiction (different actual size /
    # scent / colour / gender / unit-count / shade) can still push it down.
    #   * same UPC + same brand   → 100 (definitive)
    #   * same UPC + same size    → 100 (definitive)
    #   * same brand + same size  → floor at the verified line (same item)
    size_confirmed = _size_match(
        _s(source.get("title")), _s(amazon.get("title")), amz_size_attr
    )
    if upc_match and (brand_confirmed or size_confirmed):
        total = 100.0
    elif brand_confirmed and (size_confirmed or model_confirmed):
        # Same brand + (same per-unit size OR exact model number) = same item.
        total = max(total, VERIFIED_FLOOR)

    # Detect per-unit size mismatches (e.g. 26.2 oz vs 12.1 oz, 2 lb vs 5 lb).
    # Also checks the Amazon structured size attribute when the title has no size.
    # Different products → push below Review floor so they land in Not Approved.
    size_mismatch = _size_mismatch(
        _s(source.get("title")), _s(amazon.get("title")), amz_size_attr
    )
    # An exact model-number match means it's the same SKU, so a detected size
    # difference is a linear-dimension parsing artifact (vendor states width
    # only vs Amazon's W×L, unit typos) — suppress it UNLESS the mismatch rests
    # on a reliable volume/weight signal, which model match should not override.
    if size_mismatch and model_confirmed and not _has_vol_or_weight(
        _s(source.get("title")), _s(amazon.get("title"))
    ):
        size_mismatch = False
    if size_mismatch:
        total = min(total, 29.0)

    # Detect explicit gender contradictions: both titles carry a gender marker
    # and they disagree (one men's, one women's).  If only one title specifies
    # gender the pair is left to normal scoring — we do not penalise.
    gender_mismatch = _gender_mismatch(
        _s(source.get("title")), _s(amazon.get("title"))
    )
    if gender_mismatch:
        total = min(total, 29.0)

    # Detect colour contradictions: both sides specify a colour and they
    # disagree.  Amazon structured attribute preferred; falls through to title
    # and then full description/bullets text.
    color_mismatch = _color_mismatch(
        _s(source.get("title")), _s(amazon.get("title")),
        amz_color_attr, amz_full_text,
    )
    if color_mismatch:
        total = min(total, 29.0)

    # Detect unit-count contradictions (CPG & medical): both sides state an
    # explicit item count (e.g. "80 Count" vs "110 Count") and they differ
    # by more than 10 %.  This is a hard reject ONLY when the per-unit size is
    # NOT independently confirmed.  When brand + size confirm the same per-unit
    # product (e.g. vendor "24/6oz" case vs Amazon "6 oz, Pack of 6"), a
    # differing count is just a pack/case-size difference — not a different SKU
    # — so per the user's "only the pack differs → approve" rule it must not
    # reject.  Items with no per-unit size (wipes "80 Count" vs "110 Count")
    # have size_confirmed=False, so the count difference still hard-rejects.
    count_mismatch = (
        _unit_count_mismatch(_s(source.get("title")), _s(amazon.get("title")))
        and not size_confirmed
    )
    if count_mismatch:
        total = min(total, 29.0)

    # Detect scent/fragrance/flavour variant contradictions: both sides carry
    # scent words from the known list AND those sets are completely disjoint
    # (e.g. "Lavender" vs "Citrus" → mismatch; "Lavender" vs "Lavender Vanilla"
    # → shared "lavender" → no mismatch).
    scent_mismatch = _scent_mismatch(
        _s(source.get("title")), _s(amazon.get("title"))
    )
    if scent_mismatch:
        total = min(total, 29.0)

    # Detect shade / colour-variant contradictions (hair colour, cosmetics):
    # both sides name a specific shade and they differ — Bigen "#46 Light
    # Chestnut" vs "#48 Dark Chestnut", "Medium" vs "Light".  Different shade =
    # different SKU.  This is the safety net that stops same-brand+same-size
    # shade variants from being wrongly approved.
    shade_mismatch = _shade_mismatch(
        _s(source.get("title")), _s(amazon.get("title"))
    )
    if shade_mismatch:
        total = min(total, 29.0)

    # Detect apparel/garment size contradictions (S/M/L/XL): both titles state
    # a clothing size and they differ — vendor "X-Large" vs Amazon "Medium" is
    # a different SKU and a hard reject.  Runs after the floors so it overrides
    # even a UPC match (different sizes carry different UPCs anyway).
    apparel_size_mismatch = _apparel_size_mismatch(
        _s(source.get("title")), _s(amazon.get("title"))
    )
    if apparel_size_mismatch:
        total = min(total, 29.0)

    return {
        "confidence_score": round(total, 1),
        "brand_score": round(brand_score, 1),
        "product_type_similarity": round(product_type_sim, 1),
        "mpn_score": round(mpn_score, 1),
        "upc_match": upc_match,
        "brand_confirmed": brand_confirmed,
        "size_match": size_confirmed,
        "model_confirmed": model_confirmed,
        "mpn_variation_score": round(mpn_variation_score, 1),
        "pack_mismatch": pack_mismatch,
        "effective_pack": effective_pack,
        "size_mismatch": size_mismatch,
        "gender_mismatch": gender_mismatch,
        "color_mismatch": color_mismatch,
        "count_mismatch": count_mismatch,
        "scent_mismatch": scent_mismatch,
        "shade_mismatch": shade_mismatch,
        "apparel_size_mismatch": apparel_size_mismatch,
    }


# --------------------------------------------------------------------------- #
# Higher-level helper
# --------------------------------------------------------------------------- #


def score_amazon_item(
    source: dict,
    amazon: dict,
    additional_keywords: Iterable[str] | None = None,
) -> dict:
    """
    Return the breakdown dict plus a string verdict for the UI:
        confidence >= 90  -> "verified"
        confidence >= 35  -> "review"
        otherwise          -> "not_approved"

    The thresholds match the defaults in the catalog-verifier `settings`
    table (keys `threshold_verified` / `threshold_review`); callers that
    want different thresholds should recompute the verdict themselves.
    """
    scores = calculate_confidence(source, amazon, additional_keywords)
    conf = scores["confidence_score"]
    # Size or gender contradiction overrides pack_mismatch — these are hard
    # rejects regardless of any other signal.
    hard_reject = scores.get("size_mismatch") or scores.get("gender_mismatch") or scores.get("color_mismatch")
    if hard_reject:
        verdict = "not_approved"
    elif conf >= 90:
        verdict = "verified"
    elif conf >= 35 or scores.get("pack_mismatch"):
        verdict = "review"
    else:
        verdict = "not_approved"
    scores["verdict"] = verdict
    return scores
