"""
How a vendor states its units, for the two AI steps (2026-11-25). The vendor's title is never changed.

Vendor titles often put the SHIPPING CASE before the product's own size or count, and the keyword AI read the
case number as the count ("BIOFREEZE Large Patch 24 (8x3)/5 ct." -> "24 Count"; the product is a box of 5).
The system's own counters (attr_extract, pack_multiplier) already read such titles right; only the AI did not.

  case_shorthand(title)  which part of the title is the case: "24 (8x3)/5 ct." -> {"case_pack": 24, "unit": "5 ct"}
                         ("16/32 oz." -> 16 + "32 oz"; "1000/1" -> 1000; "8 oz, 12/cs" -> 12). Fractions ("3/4 oz")
                         and product counts ("100/Box", "36 Count") are not cases. Patterns: patterns.py; units:
                         the existing SIZE_RE / WEIGHT_RE / attr_extract.UNIT_COUNT_RE.
  unit_hints(raw)        what the vendor file's own columns say (size, count, case pack, unit of measure), found
                         by attr_extract.column_roles, the one reader of column names.
  hint_text(...)         one line for the AI: "vendor file: size 5 ct.; case pack 24 (the shipping case, NOT ...)".
  fix_case_count(...)    safety check on the AI's answer: a count that is really the case ("24 Count") becomes the
                         product's unit ("5 Count"), whatever the AI did.

Used by brand_extractor.extract_brands (keyword AI) and runner._agent_search_row (AI search); pack_multiplier
uses unit_hints for the case pack and unit of measure.
"""
from __future__ import annotations

import re
from typing import Any

from services.analytics.matching_core.matching import patterns as P
from services.analytics.matching_core.matching.attr_extract import UNIT_COUNT_RE, column_roles


def _unit_at(text: str) -> tuple[float, str] | None:
    """The size or count with its unit at the start of `text` ("5 ct.", "32 oz.", "1.5 oz"): (number, "5 ct")."""
    t = text.lstrip().lower()
    for rx in (P.SIZE_RE, P.WEIGHT_RE, UNIT_COUNT_RE):
        m = rx.match(t)
        if m:
            return float(m.group(1)), m.group(0).strip()
    return None


def case_shorthand(title: str) -> dict[str, Any]:
    """{"case_pack": N, "unit": "5 ct"} when the title states a shipping case; {} otherwise."""
    text = str(title or "")
    m = P.CASE_LAYOUT_RE.search(text)
    if m and int(m.group(1)) > 1:
        unit = _unit_at(text[m.end():])
        return {"case_pack": int(m.group(1)), **({"unit": unit[1]} if unit else {})}
    for m in P.CASE_SLASH_RE.finditer(text):
        n, unit = int(m.group(1)), _unit_at(text[m.end():])
        if n < 2 or not unit:
            continue
        if n < unit[0] and unit[0] in P.SIZE_FRACTION_DENOMINATORS:       # "3/4 oz", "1/2 gal": a size
            continue
        return {"case_pack": n, "unit": unit[1]}
    for rx in (P.PER_CASE_RE, P.CASE_OF_ONES_RE):
        m = rx.search(text)
        if m and int(m.group(1)) > 1:
            return {"case_pack": int(m.group(1))}
    return {}


def _pretty_unit(unit: str) -> str:
    n, _, u = unit.partition(" ")
    return f"{n} Count" if u.lower().rstrip(".") in ("ct", "count", "pcs", "pc", "pieces", "piece", "ea", "each") else unit


def fix_case_count(text: str, title: str) -> str:
    """The AI's keyword text, with a count that is really the shipping case replaced by the product's own unit.
    "Biofreeze Large Patch 24 Count" + title "... Large Patch 24 (8x3)/5 ct." -> "Biofreeze Large Patch 5 Count".
    Anything else is returned unchanged."""
    info = case_shorthand(title)
    case, unit = info.get("case_pack"), info.get("unit")
    if not text or not case or not unit:
        return text
    pattern = re.compile(rf"(?<![\d.]){case}\s*-?\s*(?:count|ct|pack|pk|pcs|pieces|ea|each)\b\.?", re.I)
    return pattern.sub(_pretty_unit(unit), str(text))


def _number(value: Any) -> int | None:
    m = re.search(r"\d+(?:\.\d+)?", str(value or "").replace(",", ""))
    if not m:
        return None
    n = float(m.group(0))
    return int(n) if n == int(n) and n > 0 else None


def unit_hints(raw: dict[str, Any] | None) -> dict[str, Any]:
    """One row's unit facts from its own columns: {"unit_size": "5 ct.", "unit_count": 5, "case_pack": 24,
    "uom": "CS"}. Empty when the file has none of these columns."""
    raw = raw or {}
    out: dict[str, Any] = {}
    for role, header in column_roles(raw.keys()).items():
        value = str(raw.get(header) or "").strip()
        if not value:
            continue
        if role == "size_text":
            out["unit_size"] = value
        elif role == "count" and _number(value):
            out["unit_count"] = _number(value)
        elif role == "case_pack" and (_number(value) or 0) > 1:
            out["case_pack"] = _number(value)
        elif role == "uom" and re.sub(r"[^A-Z]", "", value.upper()):
            out["uom"] = re.sub(r"[^A-Z]", "", value.upper())
    return out


def hint_text(hints: dict[str, Any] | None, title: str = "") -> str:
    """One line for the AI steps from the row's columns and its title's case shorthand, e.g.
    "vendor file: size 5 ct.; case pack 24 (the shipping case, NOT the product's count)". Empty when nothing is known."""
    h = dict(hints or {})
    title_units = case_shorthand(title)
    h.setdefault("case_pack", title_units.get("case_pack"))
    h.setdefault("unit_size", title_units.get("unit"))
    parts = []
    if h.get("unit_size"):
        parts.append(f"size {h['unit_size']}")
    if h.get("unit_count"):
        parts.append(f"count {h['unit_count']}")
    if h.get("uom"):
        parts.append(f"sold by {h['uom']}")
    if h.get("case_pack"):
        parts.append(f"case pack {h['case_pack']} (the shipping case, NOT the product's count)")
    return ("vendor file: " + "; ".join(parts)) if parts else ""
