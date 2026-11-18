"""
Variation detection on the attribute boxes from attr_extract (vendor rows and Amazon candidates).
"""
from __future__ import annotations

from typing import Any

from services.analytics.matching_core.matching.attr_extract import _agree, size_relation

VARIATION_BOXES = ("color", "flavor", "scent", "size", "gender", "shade_code", "tone", "volume_ml",
                   "weight_g", "count", "pack", "bundle", "material", "finish", "type", "model")
COUNT_BOXES = ("count", "pack", "bundle")



# --------------------------------------------------------------------------- #
# 1. Families
# --------------------------------------------------------------------------- #

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


# --------------------------------------------------------------------------- #
# 2. Comparing a vendor row with a candidate
# --------------------------------------------------------------------------- #

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
    open_boxes = boxes that vary inside the candidate's family.

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


FILE_VERSION = "2026-10-22 v8 (size partial)"