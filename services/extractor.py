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
    for ptype in PRODUCT_TYPES:
        if re.search(r"\b" + re.escape(ptype) + r"\b", lower):
            return ptype
    # Fallback: use the last meaningful word of the title.
    tokens = [t for t in re.split(r"\s+", lower) if t and not t.isdigit()]
    return tokens[-1] if tokens else None


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
# AI extraction (GPT-4o)
# --------------------------------------------------------------------------- #

AI_SYSTEM_PROMPT = (
    "You are an expert CPG product data extractor. Given a product title and "
    "optional attributes, return a strict JSON object with the fields: "
    "product_type (string), size (object with numeric 'value' and 'unit' or null), "
    "pack_count (integer or null), variant (object with optional scent/color/flavor), "
    "form (string or null). Respond with JSON only — no prose."
)


def ai_extract(title: str, extra_context: str = "") -> dict:
    """Call GPT-4o to extract the same shape as ``rule_extract``.

    Falls back to an empty dict with an error key if the API key is missing or
    the call fails. Callers should treat this as best-effort.
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return {"error": "OPENAI_API_KEY not set"}

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key)
        payload = f"Title: {title}\nContext: {extra_context}"
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
        parsed["normalised_title"] = title.lower()
        return parsed
    except Exception as exc:  # noqa: BLE001 — surface any extractor failure
        return {"error": f"AI extraction failed: {exc}"}
