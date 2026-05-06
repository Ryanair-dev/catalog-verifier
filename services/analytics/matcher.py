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
    r'|\b(\d+)\s*[-\s]?piece\b'        # "36 piece", "36-piece"
    r'|\b(\d+)\s*[-\s]?pcs\b',         # "36pcs", "36 pcs"
    re.IGNORECASE,
)

# Volume size extraction — oz / fl oz / ounce / ml / l / gal
_SIZE_RE = re.compile(
    r'\b(\d+(?:\.\d+)?)\s*'
    r'(fl\.?\s*oz|fluid\s+ounce[s]?|ounce[s]?|oz|milliliter[s]?|ml'
    r'|liter[s]?|litre[s]?|gallon[s]?|gal)\b',
    re.IGNORECASE,
)

# Weight extraction — lb / kg only (avoids false positives from "g" or "oz" alone)
_WEIGHT_RE = re.compile(
    r'\b(\d+(?:\.\d+)?)\s*'
    r'(pound[s]?|lbs?|kilogram[s]?|kg)\b',
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


def _size_mismatch(src_title: str, amz_title: str, amz_size_attr: str = "") -> bool:
    """
    Return True when vendor and Amazon both specify a size and they differ by
    more than 10 %.

    Checks volume (oz, ml, l, gal) and weight (lb, kg) separately — a volume
    from one side is never compared against a weight from the other.  The
    Amazon `attributes.size` field is checked when the Amazon title yields no
    parseable size, giving better coverage for SP-API data.

    A 10 % tolerance covers minor rounding (355 ml ≈ 12 oz).  Returns False
    when either side has no parseable size.
    """
    # --- Volume comparison ---
    src_ml = _extract_volume_ml(src_title)
    if src_ml is not None and src_ml > 0:
        amz_ml = _extract_volume_ml(amz_title)
        if amz_ml is None and amz_size_attr:
            amz_ml = _extract_volume_ml(amz_size_attr)
        if amz_ml is not None and amz_ml > 0:
            return max(src_ml, amz_ml) / min(src_ml, amz_ml) > 1.10

    # --- Weight comparison (lb / kg) ---
    src_g = _extract_weight_g(src_title)
    if src_g is not None and src_g > 0:
        amz_g = _extract_weight_g(amz_title)
        if amz_g is None and amz_size_attr:
            amz_g = _extract_weight_g(amz_size_attr)
        if amz_g is not None and amz_g > 0:
            return max(src_g, amz_g) / min(src_g, amz_g) > 1.10

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


def _gender_mismatch(title_a: str, title_b: str) -> bool:
    """
    Return True only when BOTH titles carry explicit gender markers that
    contradict each other (one male, one female).

    When only one title specifies gender (e.g. Amazon says 'for Women' but
    the vendor title is unisex), we do NOT flag it — the match is left to
    the normal title similarity score and may still reach Review or Approved.
    """
    ta = _gender_words(title_a)
    tb = _gender_words(title_b)
    a_male   = bool(ta & _MALE_TERMS)
    a_female = bool(ta & _FEMALE_TERMS)
    b_male   = bool(tb & _MALE_TERMS)
    b_female = bool(tb & _FEMALE_TERMS)
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

    Priority for Amazon color source:
      1. Structured SP-API color attribute (most reliable)
      2. Amazon title
      3. Full Amazon text (description + bullet points) — only as last resort

    Examples that DO fire:
      vendor "Crocs Classic Clog Black"  + amz color attr "White"    → True
      vendor "Shampoo Red Bottle"        + amz title "…Blue Bottle"  → True

    Examples that do NOT fire:
      vendor "Gold Bond Powder"   + amz attr ""                      → False
        (vendor has no color word → no flag)
      vendor "Eucerin Cream"      + amz attr "Blue"                  → False
        (vendor has no unambiguous color word → no flag)
      vendor "Crocs Black"        + amz attr "Black"                 → False
        (same color → no contradiction)
    """
    src_colors = set(re.findall(r'[a-z]+', src_title.lower())) & _COLOR_WORDS
    if not src_colors:
        return False

    # Structured SP-API color attribute is the most reliable source — when it
    # contains a recognised color word, use it exclusively.
    if amz_color_attr.strip():
        amz_colors = set(re.findall(r'[a-z]+', amz_color_attr.lower())) & _COLOR_WORDS
        if amz_colors:
            return not bool(src_colors & amz_colors)

    # No color in the structured attr → check title first, then full text.
    # This cascade means a color word in the description/bullets is only used
    # when the title itself has no detectable color.
    amz_colors = set(re.findall(r'[a-z]+', amz_title.lower())) & _COLOR_WORDS
    if not amz_colors and amz_full_text:
        amz_colors = set(re.findall(r'[a-z]+', amz_full_text.lower())) & _COLOR_WORDS

    if not amz_colors:
        return False

    return not bool(src_colors & amz_colors)


def _ratio(a: str, b: str) -> int:
    if not a or not b or fuzz is None:
        return 0
    return int(fuzz.ratio(a, b))


def _partial_ratio(a: str, b: str) -> int:
    if not a or not b or fuzz is None:
        return 0
    return int(fuzz.partial_ratio(a, b))


def _token_set_ratio(a: str, b: str) -> int:
    if not a or not b or fuzz is None:
        return 0
    return int(fuzz.token_set_ratio(a, b))


# --------------------------------------------------------------------------- #
# calculate_confidence — 1:1 port of the reference scorer
# --------------------------------------------------------------------------- #


def calculate_confidence(
    source: dict,
    amazon: dict,
    additional_keywords: Iterable[str] | None = None,
    upc_search_hit: bool = False,
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
        }
    """
    src_brand = _s(source.get("brand") or source.get("manufacturer"))
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
    if src_brand:
        src_brand_lower = src_brand.lower()
        brand_in_title = bool(amz_title) and src_brand_lower in amz_title
        brand_in_field = bool(amz_brand) and src_brand_lower in amz_brand.lower()
        brand_in_desc  = bool(amz_desc)  and src_brand_lower in amz_desc
        if brand_in_title or brand_in_field or brand_in_desc:
            brand_score = BRAND_MAX
        else:
            # Fuzzy fallback — require >= 70% similarity on best of 3 fields.
            best = max(
                _ratio(src_brand_lower, amz_brand.lower()) if amz_brand else 0,
                _partial_ratio(src_brand_lower, amz_title) if amz_title else 0,
                _partial_ratio(src_brand_lower, amz_desc) if amz_desc else 0,
            )
            if best >= 70:
                brand_score = (best / 100.0) * BRAND_MAX

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
        # brand+MPN both present in title OR description = auto TITLE_MAX
        if (amz_title and sb in amz_title and sm in amz_title) or (
            amz_desc and sb in amz_desc and sm in amz_desc
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

    # Detect multi-pack mismatches (same per-unit size, different count).
    # Cap at 80 → Review so the user can decide whether it's the right listing.
    effective_pack, pack_mismatch = _effective_pack(src_title, _s(amazon.get("title")))
    if pack_mismatch:
        total = min(total, 80.0)

    # Build Amazon's full text (description + bullets) for attribute checks.
    _amz_desc_str = _s(amazon.get("description"))
    _amz_bullets  = amazon.get("bullet_points") or []
    amz_full_text = " ".join(
        p for p in [_amz_desc_str] + [_s(b) for b in _amz_bullets] if p
    )
    amz_attrs      = amazon.get("attributes") or {}
    amz_size_attr  = _s(amz_attrs.get("size"))
    amz_color_attr = _s(amz_attrs.get("color"))

    # Detect per-unit size mismatches (e.g. 26.2 oz vs 12.1 oz, 2 lb vs 5 lb).
    # Also checks the Amazon structured size attribute when the title has no size.
    # Different products → push below Review floor so they land in Not Approved.
    size_mismatch = _size_mismatch(
        _s(source.get("title")), _s(amazon.get("title")), amz_size_attr
    )
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

    return {
        "confidence_score": round(total, 1),
        "brand_score": round(brand_score, 1),
        "product_type_similarity": round(product_type_sim, 1),
        "mpn_score": round(mpn_score, 1),
        "upc_match": upc_match,
        "pack_mismatch": pack_mismatch,
        "effective_pack": effective_pack,
        "size_mismatch": size_mismatch,
        "gender_mismatch": gender_mismatch,
        "color_mismatch": color_mismatch,
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
