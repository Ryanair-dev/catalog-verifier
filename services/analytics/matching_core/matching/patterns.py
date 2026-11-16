"""Regex patterns and constants for identifier/text normalization."""
from __future__ import annotations

import re

MPN_SUFFIX_RE = re.compile(
    r"(?:"
    r"[\s\-]*amz\d*(?:pk)?"
    r"|[\s\-]*fba"
    r"|[\s\-]*\d+\s?pk"
    r"|[\s\-]*pk\s?\d+"
    r")+$",
    re.IGNORECASE,
)

NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")
NON_DIGIT_RE = re.compile(r"\D")
WHITESPACE_RE = re.compile(r"\s+")

# Below this many alphanumerics, a stripped MPN base is too short to trust.
MIN_BASE_LEN = 3

# --- size / volume / weight extraction ---
SIZE_RE = re.compile(
    r"(?<![.\d])(\d+(?:\.\d+)?|\.\d+)[\s-]*"
    r"(fl\.?\s*oz|fluid\s+ounce[s]?|ounce[s]?|oz|milliliter[s]?|ml"
    r"|liter[s]?|litre[s]?|gallon[s]?|gal|quart[s]?|qt|l)\b",
    re.IGNORECASE,
)
WEIGHT_RE = re.compile(
    r"(?<![.\d])(\d+(?:\.\d+)?|\.\d+)\s*(pound[s]?|lbs?|kilogram[s]?|kg|gram[s]?|g)\b",
    re.IGNORECASE,
)

# volume unit -> millilitres
VOLUME_TO_ML = {
    "oz": 29.5735, "ounce": 29.5735, "ounces": 29.5735,
    "floz": 29.5735, "fluidounce": 29.5735, "fluidounces": 29.5735,
    "ml": 1.0, "milliliter": 1.0, "milliliters": 1.0,
    "l": 1000.0, "liter": 1000.0, "liters": 1000.0, "litre": 1000.0, "litres": 1000.0,
    "gal": 3785.41, "gallon": 3785.41, "gallons": 3785.41,
    "qt": 946.353, "quart": 946.353, "quarts": 946.353,
}
# weight unit -> grams
WEIGHT_TO_G = {
    "lb": 453.592, "lbs": 453.592, "pound": 453.592, "pounds": 453.592,
    "kg": 1000.0, "kilogram": 1000.0, "kilograms": 1000.0,
    "g": 1.0, "gram": 1.0, "grams": 1.0,
}

# --- garment sizes (longest first so "x-large" beats "large") ---
GARMENT_SIZE_RE = re.compile(
    r"\b(xxx-?large|3x-?large|xxxl|3xl|xx-?large|2x-?large|xxl|2xl"
    r"|x-?large|extra[\s-]?large|xl|xx-?small|2x-?small|xxs"
    r"|x-?small|extra[\s-]?small|xs|small|medium|large)\b",
    re.IGNORECASE,
)
GARMENT_NORM = {
    "xxs": "XXS", "xxsmall": "XXS", "2xsmall": "XXS",
    "xs": "XS", "xsmall": "XS", "extrasmall": "XS",
    "small": "S", "medium": "M", "large": "L",
    "xl": "XL", "xlarge": "XL", "extralarge": "XL",
    "xxl": "XXL", "2xl": "XXL", "xxlarge": "XXL", "2xlarge": "XXL",
    "xxxl": "XXXL", "3xl": "XXXL", "xxxlarge": "XXXL", "3xlarge": "XXXL",
}

# Full-calf sizes (compression wear, e.g. JOBST): "XLFC", "X-Large Full Calf", "Large Full Calf".
# A full-calf size is its own product, so it gets its own code (XLFC is not XL).
FULL_CALF_RE = re.compile(
    r"\b(?:(xxl|2xl|xl|l|m|s)fc"
    r"|(xx-?large|2x-?large|x-?large|extra[\s-]?large|large|medium|small)[\s-]+full[\s-]+calf)\b",
    re.IGNORECASE,
)
_FULL_CALF_LETTERS = {"xxl": "XXL", "2xl": "XXL", "xl": "XL", "l": "L", "m": "M", "s": "S"}

# A lone S / M / L counts as a size only where a size is written: "- M / Regular", "- L -", "Size: L".
# Never "(S)" (Morakniv: stainless), "1 L" (litre) or "10 m" (metre).
SIZE_SLOT_RE = re.compile(r"(?:(?<=[-\u2013|,]\s)|(?<=[-\u2013|,])|\b[Ss]ize:?\s+)([SML])(?=\s*(?:/|[-\u2013|,]|$))")


def full_calf_sizes(text: str) -> list[str]:
    """'X-Large Full Calf' -> ['XLFC']."""
    out: list[str] = []
    for m in FULL_CALF_RE.finditer(text or ""):
        if m.group(1):
            base = _FULL_CALF_LETTERS[m.group(1).lower()]
        else:
            base = GARMENT_NORM.get(m.group(2).lower().replace("-", "").replace(" ", ""), "")
        code = f"{base}FC" if base else ""
        if code and code not in out:
            out.append(code)
    return out


def garment_relation(a: set[str], b: set[str]) -> str | None:
    """'match' / 'mismatch', None if a side has no size. Full calf must match full calf (LFC is not L)."""
    if not a or not b:
        return None
    a_fc = {x for x in a if x.endswith("FC")}
    b_fc = {x for x in b if x.endswith("FC")}
    if a_fc or b_fc:
        return "match" if (a_fc & b_fc) else "mismatch"
    return "match" if (a & b) else "mismatch"


def garment_sizes(text: str) -> list[str]:
    """All garment sizes in the text: full calf (XLFC), size words (Medium, XL), lone S/M/L in a size position."""
    text = text or ""
    found = full_calf_sizes(text)
    rest = FULL_CALF_RE.sub(" ", text)          # 'X-Large Full Calf' is XLFC, not also XL
    for m in GARMENT_SIZE_RE.finditer(rest):
        code = GARMENT_NORM.get(m.group(1).lower().replace("-", "").replace(" ", ""))
        if code and code not in found:
            found.append(code)
    for m in SIZE_SLOT_RE.finditer(rest):
        if m.group(1) not in found:
            found.append(m.group(1))
    return found


# --- fractions ---
UNICODE_FRACS = {
    "½": "1/2", "⅓": "1/3", "⅔": "2/3", "¼": "1/4", "¾": "3/4",
    "⅕": "1/5", "⅖": "2/5", "⅗": "3/5", "⅘": "4/5",
    "⅙": "1/6", "⅚": "5/6", "⅛": "1/8", "⅜": "3/8", "⅝": "5/8", "⅞": "7/8",
}
MIXED_FRAC_RE = re.compile(r"(\d+)[\s-](\d{1,2})/(2|3|4|5|6|7|8|10|12|16|32|64)(?!\d)")
SIMPLE_FRAC_RE = re.compile(r"(?<!\d)(\d{1,2})/(2|3|4|5|6|7|8|10|12|16|32|64)(?!\d)")

# --- linear dimensions (run AFTER fraction normalization) ---
INCH_RE = re.compile(
    r'(?<![.\d])(\d+(?:\.\d+)?)(?:["\u2033]|[\s-]*inch(?:es)?\b|[\s-]*in\.)',
    re.IGNORECASE,
)
CM_RE = re.compile(r"(?<![.\d])(\d+(?:\.\d+)?)[\s-]*(?:centimeters?|centimetres?|cm)\b", re.IGNORECASE)
MM_RE = re.compile(r"(?<![.\d])(\d+(?:\.\d+)?)[\s-]*(?:millimeters?|millimetres?|mm)\b", re.IGNORECASE)

# --- barcode helpers ---
DIGITS_ONLY_RE = re.compile(r"\D")

# --- pack/count detection (for effective_pack) ---
PACK_COUNT_RE = re.compile(
    r'\bpack\s+of\s+(\d+)\b|\bbox\s+of\s+(\d+)\b|\bcase\s+of\s+(\d+)\b'
    r'|\b(\d+)\s*[-\s]?count\b|\b(\d+)\s*[-\s]?ct\b'
    r'|\b(\d+)\s*[-\s]?pack\b|\b(\d+)\s*[-\s]?pk\b|\b(\d+)/(?=\d)'
    r'|\b(\d+)\s*[-\s]?cs\b',   # "36cs" = 36 case
    re.IGNORECASE,
)

# --- brand normalization ---
BRAND_NOISE_WORDS = frozenset({
    "inc", "incorporated", "llc", "llp", "ltd", "limited", "corp", "corporation",
    "co", "company", "health", "healthcare", "medical", "products", "product",
    "industries", "brands", "brand", "group", "the",
})
APOSTROPHE_RE = re.compile(r"['\u2019`]")
NON_ALNUM_SPACE_RE = re.compile(r"[^a-z0-9]+")

COLOR_WORDS: frozenset[str] = frozenset({
    "red", "blue", "green", "yellow", "orange", "purple", "pink",
    "black", "white", "gray", "grey", "brown",
    "navy", "teal", "turquoise", "maroon", "burgundy",
    "clear", "transparent", "translucent",
})

BUNDLE_COUNT_RE = re.compile(
    r'\bpack\s+of\s+(\d+)\b|\bbox\s+of\s+(\d+)\b'
    r'|\b(\d+)\s*[-\s]?pack\b|\b(\d+)\s*[-\s]?pk\b',
    re.IGNORECASE,
)

# Wider bundle notation, used when MATCH_EXTENDED_COUNTS is on: the same forms as
# above plus "4PCS", "4 pc", "2 pieces", "12 ct", "4 Count". "cs" (vendor cases per
# carton) is still deliberately left out - see cmp_count.
BUNDLE_COUNT_EXT_RE = re.compile(
    r'\bpack\s+of\s+(\d+)\b|\bbox\s+of\s+(\d+)\b'
    r'|\b(\d+)\s*[-\s]?pack\b|\b(\d+)\s*[-\s]?pk\b'
    r'|\b(\d+)\s*[-\s]?(?:pcs?|pieces?|ct|count)\b',
    re.IGNORECASE,
)

TRANSPARENCY_WORDS: frozenset[str] = frozenset({"clear", "transparent", "translucent"})