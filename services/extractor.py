"""
Attribute extraction for CPG products.

Two modes are supported:

1. Rule-based extraction (default). Uses regex + an editable abbreviation
   library to pull product_type, size, pack_count, variant, and form out of
   titles and feature fields.

2. AI extraction (on demand). Calls GPT-4o with a strict JSON schema.

Both modes return the same dict shape, so downstream confidence scoring does
not need to care which was used.
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable

# --------------------------------------------------------------------------- #
# Abbreviation / alias handling
# --------------------------------------------------------------------------- #

def apply_abbreviations(text: str, abbreviations: Iterable[dict]) -> str:
    """Expand abbreviations in ``text`` using the editable alias library.

    Matches are whole-word, case-insensitive. The library is a list of
    ``{"abbr": "...", "full": "..."}`` dicts. Longer abbreviations are applied
    first so e.g. ``fl oz`` beats ``oz``.
    """
    if not text:
        return ""
    cleaned = text
    sorted_abbrs = sorted(
        (a for a in abbreviations if a.get("abbr")),
        key=lambda a: len(a["abbr"]),
        reverse=True,
    )
    for entry in sorted_abbrs:
        abbr = entry["abbr"]
        full = entry.get("full", "")
        pattern = r"\b" + re.escape(abbr) + r"\b"
        cleaned = re.sub(pattern, full, cleaned, flags=re.IGNORECASE)
    return cleaned


# --------------------------------------------------------------------------- #
# Regex patterns
# --------------------------------------------------------------------------- #

# Size / weight / volume, e.g. "16 oz", "1.5 L", "750 ml", "2.2 lb"
SIZE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*"
    r"(fl\s*oz|fluid\s*ounce|ounces?|oz|lbs?|pounds?|gallons?|gal|"
    r"kilograms?|kg|grams?|g|milliliters?|ml|liters?|l)\b",
    re.IGNORECASE,
)

# Pack count indicators, e.g. "12 pack", "pack of 24", "24 ct", "case of 6"
PACK_PATTERNS = [
    re.compile(r"pack\s*of\s*(\d+)", re.IGNORECASE),
    re.compile(r"case\s*of\s*(\d+)", re.IGNORECASE),
    re.compile(r"(\d+)\s*[- ]?\s*pack\b", re.IGNORECASE),
    re.compile(r"(\d+)\s*[- ]?\s*(?:ct|count|pcs|pieces)\b", re.IGNORECASE),
    re.compile(r"(\d+)\s*ea\b", re.IGNORECASE),
    re.compile(r"\bqty\s*[:\-]?\s*(\d+)", re.IGNORECASE),
    re.compile(r"\bx\s*(\d+)\b", re.IGNORECASE),
]

VARIANT_KEYWORDS = {
    "scent": [
        "lavender", "vanilla", "citrus", "lemon", "ocean", "mint", "pine",
        "floral", "fresh", "unscented", "original", "cherry", "apple",
        "berry", "coconut",
    ],
    "color": [
        "white", "black", "red", "blue", "green", "yellow", "purple",
        "pink", "brown", "clear", "grey", "gray", "orange",
    ],
    "flavor": [
        "chocolate", "vanilla", "strawberry", "mint", "cherry", "apple",
        "orange", "grape", "lemon", "lime", "peach", "banana", "berry",
    ],
    "form": [
        "liquid", "powder", "capsule", "tablet", "gel", "spray", "wipe",
        "stick", "bar", "cream", "lotion", "foam",
    ],
}

# Rough product-type vocabulary. Falls back to the first 2 words of the title
# if nothing here matches. The goal is a stable token, not a taxonomy.
PRODUCT_TYPES = [
    "shampoo", "conditioner", "soap", "detergent", "cleaner", "sanitizer",
    "towel", "tissue", "wipes", "wipe", "toothpaste", "deodorant",
    "lotion", "cream", "spray", "disinfectant", "bleach", "dish soap",
    "laundry detergent", "paper towel", "toilet paper", "napkin",
    "mouthwash", "bandage", "gauze", "mask", "glove", "gloves",
    "trash bag", "food wrap", "foil", "water", "coffee", "tea", "snack",
    "bar", "chips", "cookies", "cereal", "vitamin", "supplement",
]


# --------------------------------------------------------------------------- #
# Rule-based extraction
# --------------------------------------------------------------------------- #

def _first(iterable):
    """Return the first truthy item from ``iterable`` or None."""
    for item in iterable:
        if item:
            return item
    return None


def _extract_size(text: str) -> dict | None:
    match = SIZE_RE.search(text)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower().replace(" ", "")
    # Normalise a few spellings.
    unit = (unit
            .replace("ounces", "oz").replace("ounce", "oz")
            .replace("pounds", "lb").replace("pound", "lb").replace("lbs", "lb")
            .replace("grams", "g").replace("gram", "g")
            .replace("kilograms", "kg").replace("kilogram", "kg")
            .replace("milliliters", "ml").replace("milliliter", "ml")
            .replace("liters", "l").replace("liter", "l")
            .replace("gallons", "gal").replace("gallon", "gal")
            .replace("fluidoz", "floz"))
    return {"value": value, "unit": unit, "raw": match.group(0)}


def _extract_pack(text: str) -> int | None:
    for pattern in PACK_PATTERNS:
        match = pattern.search(text)
        if match:
            try:
                return int(match.group(1))
            except (TypeError, ValueError):
                continue
    return None


def _extract_variant(text: str) -> dict:
    lower = text.lower()
    found: dict[str, str] = {}
    for category, keywords in VARIANT_KEYWORDS.items():
        for kw in keywords:
            if re.search(r"\b" + re.escape(kw) + r"\b", lower):
                found[category] = kw
                break
    return found


def _extract_product_type(text: str) -> str | None:
    lower = text.lower()
    for ptype in sorted(PRODUCT_TYPES, key=len, reverse=True):
        if re.search(r"\b" + re.escape(ptype) + r"\b", lower):
            return ptype
    # No fallback — a missing product type means no penalty, not a wrong one.
    return None


def rule_extract(title: str, abbreviations: Iterable[dict]) -> dict:
    """Run the full rule-based extractor against a product title.

    Returns a dict with keys: product_type, size, pack_count, variant, form,
    and normalised_title (abbreviations expanded, lowercased).
    """
    if not title:
        return {
            "product_type": None,
            "size": None,
            "pack_count": None,
            "variant": {},
            "form": None,
            "normalised_title": "",
        }
    expanded = apply_abbreviations(title, abbreviations).lower()
    variant = _extract_variant(expanded)
    return {
        "product_type": _extract_product_type(expanded),
        "size": _extract_size(expanded),
        "pack_count": _extract_pack(expanded),
        "variant": variant,
        "form": variant.get("form"),
        "normalised_title": expanded,
    }


# --------------------------------------------------------------------------- #
# Amazon full-row extraction
# --------------------------------------------------------------------------- #

# All text-bearing fields across Keepa and standard Amazon exports.
_AMZ_TEXT_FIELDS = [
    # Keepa
    "Title", "Size", "Variation Attributes",
    "Description & Features: Feature 1", "Description & Features: Feature 2",
    "Description & Features: Feature 3", "Description & Features: Feature 4",
    "Description & Features: Feature 5", "Description & Features: Feature 6",
    "Description & Features: Feature 7", "Description & Features: Feature 8",
    "Description & Features: Feature 9", "Description & Features: Feature 10",
    "Description & Features: Description",
    # Standard export
    "item_name", "size", "item_form", "product_benefit",
    "bullet_points", "directions", "variation_theme",
]

_AMZ_COLOR_FIELDS  = ("Color",  "color")
_AMZ_SCENT_FIELDS  = ("Scent",  "scent")
_AMZ_FLAVOR_FIELDS = ("Flavor", "flavor")
_AMZ_FORM_FIELDS   = ("item_form",)
_AMZ_SIZE_FIELDS   = ("Size", "size", "liquid_volume")

# "Unit Details: Unit Value" is the actual head/piece count in Keepa exports.
# "Package: Quantity" is almost always 1 (means "sold as 1 package") so it
# is intentionally excluded — the piece count comes from text or Unit Value.
_AMZ_PACK_FIELDS = (
    "Unit Details: Unit Value",
    "Number of Items", "number_of_items",
    "item_package_quantity", "unit_count",
)


def amz_extract(amz_row: dict, abbreviations: Iterable[dict]) -> dict:
    """Extract attributes from a full Amazon row dict.

    Combines all text fields (title, features, bullets, description) into one
    blob for fuzzy matching, then overrides extracted attributes with the
    dedicated column values (Color, Scent, Size, etc.) that Amazon provides
    directly — so we never have to guess from text when the value is explicit.
    """
    parts: list[str] = []
    for field in _AMZ_TEXT_FIELDS:
        v = amz_row.get(field)
        if v not in (None, ""):
            parts.append(str(v).strip())
    combined = " ".join(parts)

    attrs = rule_extract(combined, abbreviations)

    def _pick(*fields: str) -> str | None:
        for f in fields:
            v = amz_row.get(f)
            if v not in (None, ""):
                return str(v).strip().lower()
        return None

    color  = _pick(*_AMZ_COLOR_FIELDS)
    scent  = _pick(*_AMZ_SCENT_FIELDS)
    flavor = _pick(*_AMZ_FLAVOR_FIELDS)
    form   = _pick(*_AMZ_FORM_FIELDS)

    if color:
        attrs["variant"]["color"] = color
    if scent:
        attrs["variant"]["scent"] = scent
    if flavor:
        attrs["variant"]["flavor"] = flavor
    if form:
        attrs["form"] = form
        attrs["variant"].setdefault("form", form)

    # Size: prefer dedicated column, fall back to text extraction.
    for sf in _AMZ_SIZE_FIELDS:
        raw = amz_row.get(sf)
        if raw not in (None, ""):
            parsed = _extract_size(str(raw))
            if parsed:
                attrs["size"] = parsed
                break

    # Pack count: prefer dedicated column when value > 1.
    # A value of 1 is ambiguous ("1 package") — trust the text-extracted count
    # (e.g. "2 Count" parsed from Size/title) in that case.
    for pf in _AMZ_PACK_FIELDS:
        raw = amz_row.get(pf)
        if raw not in (None, ""):
            try:
                v = int(float(raw))
                if v > 1:
                    attrs["pack_count"] = v
                    break
            except (TypeError, ValueError):
                continue

    return attrs


# --------------------------------------------------------------------------- #
# AI extraction (GPT-4o)
# --------------------------------------------------------------------------- #

AI_SYSTEM_PROMPT = (
    "You are an expert CPG / medical-supply catalog interpreter. Your PRIMARY job "
    "is to unabbreviate terse vendor titles token-by-token, then extract attributes.\n\n"
    "IMPORTANT RULES ABOUT ABBREVIATIONS:\n"
    "• Abbreviations expand at the TOKEN level, not the phrase level. "
    "For example, 'ADHSV' on its own means 'Adhesive', 'SPG' on its own means 'Sponge'. "
    "The phrase 'ADHSV SPG' becomes 'Adhesive Sponge' because each token expands independently — "
    "compound meaning falls out from adjacent tokens, not from multi-word lookups.\n"
    "• You will be given a list of KNOWN abbreviations. Treat those as ground truth — "
    "use them verbatim when the same tokens appear in the title.\n"
    "• For tokens that are NOT in the known list but clearly look like an abbreviation "
    "(short all-caps or tight letter runs, medical / CPG convention), propose an expansion "
    "ONLY if you are confident — and propose the expansion of that single token, not of a phrase.\n"
    "• Do not invent expansions for tokens that are already normal English words.\n\n"
    "Return a strict JSON object with these fields:\n"
    "  product_type: string or null\n"
    "  size: object with numeric 'value' and 'unit' or null\n"
    "  pack_count: integer or null\n"
    "  variant: object with optional scent/color/flavor\n"
    "  form: string or null\n"
    "  expanded_title: the title rewritten with ALL abbreviation tokens (known + newly proposed) "
    "expanded, preserving original word order and non-abbreviation tokens verbatim\n"
    "  new_abbreviations: array of {abbr, full} for tokens you expanded that were NOT in the known list "
    "(empty array if none). Each 'full' must be a single-word expansion, not a phrase.\n\n"
    "Respond with JSON only — no prose."
)


def ai_extract(
    title: str,
    abbreviations: Iterable[dict] | None = None,
    extra_context: str = "",
) -> dict:
    """Call GPT-4o to unabbreviate + extract attributes.

    Returns the same shape as ``rule_extract`` plus ``expanded_title`` (the
    title with every abbreviation resolved) and ``new_abbreviations`` (list of
    {abbr, full} dicts the caller should log back to the library).

    Falls back to an empty dict with an error key if the API key is missing or
    the call fails. Callers should treat this as best-effort.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return {"error": "OPENAI_API_KEY not set"}

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        known = list(abbreviations or [])
        # Compact known-abbrev list for the prompt; cap to keep the payload reasonable.
        known_lines = "\n".join(
            f"  {a['abbr']} -> {a.get('full','')}"
            for a in known[:400] if a.get("abbr")
        )
        payload = (
            f"Known abbreviations (token -> expansion):\n"
            f"{known_lines or '  (none yet — propose carefully)'}\n\n"
            f"Title: {title}\n"
            f"Context: {extra_context}"
        )
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": AI_SYSTEM_PROMPT},
                {"role": "user", "content": payload},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        raw = resp.choices[0].message.content or "{}"
        parsed: dict[str, Any] = json.loads(raw)
        # Guarantee the two unabbreviation fields exist so the scan loop can
        # trust their shape without re-checking each call.
        if not isinstance(parsed.get("new_abbreviations"), list):
            parsed["new_abbreviations"] = []
        if not isinstance(parsed.get("expanded_title"), str) or not parsed["expanded_title"].strip():
            parsed["expanded_title"] = title
        parsed["normalised_title"] = parsed["expanded_title"].lower()
        return parsed
    except Exception as exc:  # noqa: BLE001 — surface any extractor failure
        return {"error": f"AI extraction failed: {exc}"}
