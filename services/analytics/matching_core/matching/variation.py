"""
Variation detection (works on vendor rows AND on Amazon candidates).

Two jobs, both built on the boxes from attr_extract:

1. FAMILIES.  Look-alike items are grouped into a family: same brand, and the same title once the
   variant words (colors, flavors, scents, clothing sizes, counts, units) are taken out.
       "Injinji Sport Lightweight Hidden (Small, Black)"  and  "... (Medium, Black)"  -> one family
       "Boost Glucose Control Chocolate 8 fl oz"  and  "... Vanilla 8 fl oz"          -> one family
   Inside a family, the boxes that take 2+ different values are its VARIATIONS (size, color, flavor).
   (services/analytics/vendor_families.py builds the run-level families the UI and dedup use.)

2. COMPARING one vendor row with one candidate, box by box:
       match / mismatch / unknown (one side does not say) / open (the candidate's family varies by
       that box, and the vendor row does not pick one, so it is not a conflict) /
       partial (sizes only: S vs XS/S, M vs M/L - one combined size shares a part with the other).

Nothing here changes a verdict. It only reads and reports, until the scorers are switched to use it.
"""
from __future__ import annotations

import re
from typing import Any

from services.analytics.matching_core.matching.attr_extract import (
    _agree,
    _norm,
    _tokens,
    _words,
    size_relation,
)

VARIATION_BOXES = ("color", "flavor", "scent", "size", "gender", "shade_code", "tone", "volume_ml",
                   "weight_g", "count", "pack", "bundle", "material", "finish", "type", "model")
COUNT_BOXES = ("count", "pack", "bundle")

_SIZE_WORDS = {"small", "medium", "large", "xs", "xl", "xxl", "xxxl", "xxs", "extra", "x",
               "s", "m", "l"}
_UNIT_WORDS = {"oz", "ounce", "ounces", "fl", "ml", "l", "liter", "liters", "litre", "lb", "lbs",
               "pound", "pounds", "kg", "g", "gram", "grams", "gal", "gallon", "count", "ct",
               "pack", "packs", "pk", "pcs", "pc", "piece", "pieces", "each", "ea", "of", "set",
               "box", "case", "bundle", "size", "in", "inch", "inches", "volt"}


# --------------------------------------------------------------------------- #
# 1. Families
# --------------------------------------------------------------------------- #

def base_title(title: Any) -> str:
    """The title with every variant word taken out. Look-alike items share this."""
    drop = (_words("color_words") | _words("flavor_words") | _words("scent_words")
            | _SIZE_WORDS | _UNIT_WORDS)
    keep = [t for t in _tokens(title)
            if t not in drop and not t.isdigit() and not re.fullmatch(r"\d+[a-z]{1,3}", t)]
    return " ".join(keep)


def family_key(title: Any, brand: Any = "") -> str | None:
    base = base_title(title)
    if not base:
        return None
    return f"{_norm(brand)}|{base}"


def _same_value(box: str, a: Any, b: Any) -> bool:
    return _agree(box, a, b)


def varying_boxes(members: list[dict]) -> dict[str, list[Any]]:
    """Boxes that take 2+ different values among the members: {"size": ["S", "M", "L"], ...}."""
    out: dict[str, list[Any]] = {}
    for box in VARIATION_BOXES:
        values: list[Any] = []
        for m in members:
            b = (m.get("fields") or {}).get(box)
            if b and not any(_same_value(box, b["value"], v) for v in values):
                values.append(b["value"])
        if len(values) >= 2:
            out[box] = values
    return out


def group_families(items: list[dict]) -> list[dict]:
    """items: [{"id": ..., "title": ..., "brand": ..., "fields": {...boxes...}}]
    Returns families: {"key", "ids", "varying"}. Items with no usable title form no family."""
    groups: dict[str, list[dict]] = {}
    loose: list[dict] = []
    for it in items:
        key = family_key(it.get("title"), it.get("brand"))
        if key is None:
            loose.append(it)
        else:
            groups.setdefault(key, []).append(it)
    families = [{"key": k, "ids": [m["id"] for m in v], "varying": varying_boxes(v)}
                for k, v in groups.items()]
    families += [{"key": None, "ids": [m["id"]], "varying": {}} for m in loose]
    return families


# --------------------------------------------------------------------------- #
# 2. Comparing a vendor row with a candidate
# --------------------------------------------------------------------------- #

def total_units(fields: dict[str, dict]) -> float | None:
    """How many units the item holds in all: count x pack. ("2 Each (Pack of 12)" = 24,
    "2 Count (Pack of 1)" = 2, "4 Pack" = 4, "Pack of 6" = 6.)  None when no number is known."""
    def val(name: str) -> float | None:
        box = fields.get(name)
        try:
            return float(box["value"]) if box else None
        except (TypeError, ValueError):
            return None
    count, pack, bundle = val("count"), val("pack"), val("bundle")
    if count and pack:
        return count * pack
    return count or pack or bundle


def _box_conflicted(fields: dict[str, dict], names: tuple[str, ...]) -> bool:
    return any((fields.get(n) or {}).get("conflict") for n in names)


def _count_value(fields: dict[str, dict]) -> float | None:
    """The count PRINTED on the package ('80 Count', '2 Each', '6 Rolls'), only when it is above 1."""
    box = fields.get("count")
    try:
        n = float(box["value"]) if box else None
    except (TypeError, ValueError):
        return None
    return n if n and n > 1 else None


def _within(a: float, b: float, tolerance: float) -> bool:
    return abs(a - b) <= tolerance * max(abs(a), abs(b), 1e-9)


def _same_physical_size(vendor: dict[str, dict], amazon: dict[str, dict]) -> bool:
    """Both sides state the same per-unit volume or weight (10% tolerance)."""
    for box in ("volume_ml", "weight_g"):
        v, a = vendor.get(box), amazon.get(box)
        if v and a and _within(float(v["value"]), float(a["value"]), 0.10):
            return True
    return False


def compare_boxes(vendor: dict[str, dict], amazon: dict[str, dict],
                  open_boxes: frozenset[str] | set[str] = frozenset()) -> dict[str, str]:
    """Box by box:  match | mismatch | unknown | open | partial (sizes only).
    open_boxes = boxes that vary inside the candidate's family (from group_families).

    'units' follows the old scorer's rule: only the count PRINTED on the package decides
    ("80 Count" vs "110 Count" = a different product).  A pack or bundle multiplier ("Pack of 12",
    "4 Pack") is only a pack difference and is NOT a mismatch.  Counts within 10% agree, and when the
    per-unit volume / weight is the same a count difference is not held against the pair.

    Sizes: the same set is a match (S = S), no shared part is a mismatch (S vs M), and a combined size
    that shares a part (S vs XS/S, M vs M/L) is 'partial': usually a different SKU, but not proven."""
    out: dict[str, str] = {}
    for box in ("color", "flavor", "scent", "material", "gender", "size", "shade_code", "tone",
                "volume_ml", "weight_g", "finish", "type", "model"):
        v, a = vendor.get(box), amazon.get(box)
        if v and a:
            if v.get("conflict") or a.get("conflict"):
                out[box] = "unknown"          # a side whose own sources disagree is not trusted
            elif box == "size":
                relation = size_relation(v["value"], a["value"])
                out[box] = {"same": "match", "partial": "partial"}.get(relation, "mismatch")
            else:
                out[box] = "match" if _same_value(box, v["value"], a["value"]) else "mismatch"
        elif a and not v and box in open_boxes:
            out[box] = "open"                 # the family varies here and the vendor did not pick
        elif v or a:
            out[box] = "unknown"
    vc, ac = _count_value(vendor), _count_value(amazon)
    if vc and ac:
        if (vendor.get("count") or {}).get("conflict") or (amazon.get("count") or {}).get("conflict"):
            out["units"] = "unknown"
        elif _within(vc, ac, 0.10):
            out["units"] = "match"
        else:
            out["units"] = "unknown" if _same_physical_size(vendor, amazon) else "mismatch"
    elif vc or ac:
        out["units"] = "unknown"
    return out


def describe_candidates(vendor_fields: dict[str, dict], candidates: list[dict]) -> list[dict]:
    """For ONE vendor row and its candidates [{"id","title","brand","fields"}]:
    group the candidates into families and report, per family, the varying boxes, which of them
    the vendor row leaves open, and how each candidate compares box by box."""
    result = []
    by_id = {c["id"]: c for c in candidates}
    for fam in group_families(candidates):
        varying = set(fam["varying"])
        open_boxes = {b for b in varying if b not in vendor_fields}
        fits = {i: compare_boxes(vendor_fields, by_id[i]["fields"], open_boxes) for i in fam["ids"]}
        result.append({**fam, "open": sorted(open_boxes), "compare": fits})
    return result


FILE_VERSION = "2026-10-22 v8 (size partial)"