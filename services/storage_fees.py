"""
Storage fee calculator — SP-API dimension extraction + FBA storage rates.

Rates (per cubic foot per month, Amazon US):
  Standard (Small/Large):  Jan–Sep $0.78  |  Oct–Dec (Q4/peak) $2.40
  Bulky (Small/Large):     Jan–Sep $0.56  |  Oct–Dec (Q4/peak) $1.40
  Extra Large:             N/A (not calculable)
"""
from __future__ import annotations

from typing import Any


# --------------------------------------------------------------------------- #
# Unit conversion helpers
# --------------------------------------------------------------------------- #

def _to_cm(val: float, unit: str) -> float:
    u = unit.lower().strip()
    if u in ("inches", "inch", "in"):
        return val * 2.54
    if u in ("feet", "foot", "ft"):
        return val * 30.48
    if u in ("millimeters", "millimeter", "mm"):
        return val / 10
    return val  # assume centimeters


def _to_grams(val: float, unit: str) -> float:
    u = unit.lower().strip()
    if "pound" in u or u == "lb" or u == "lbs":
        return val * 453.592
    if "kilogram" in u or u in ("kg", "kgs"):
        return val * 1000
    if "ounce" in u or u in ("oz", "ounces"):
        return val * 28.3495
    return val  # assume grams


# --------------------------------------------------------------------------- #
# SP-API attribute parsing
# --------------------------------------------------------------------------- #

def _read_measure(entry: Any) -> tuple[float | None, str]:
    """
    Read (value, unit) from an SP-API attribute entry.

    Handles both flat dicts  {"value": 10.0, "unit": "centimeters"}
    and nested-list dicts    {"value": [{"value": 10.0, "unit": "centimeters"}]}.
    """
    if not isinstance(entry, dict):
        return None, ""
    val  = entry.get("value")
    unit = str(entry.get("unit") or "")
    # Nested list form
    if isinstance(val, list):
        val = val[0] if val else None
        if isinstance(val, dict):
            unit = str(val.get("unit") or unit)
            val  = val.get("value")
    if val is None:
        return None, unit
    try:
        return float(val), unit
    except (TypeError, ValueError):
        return None, unit


def _first_entry(attrs: dict, *keys: str) -> Any | None:
    """Return attrs[first_matching_key][0], trying keys in order."""
    for key in keys:
        entries = attrs.get(key) or []
        if entries:
            e = entries[0] if isinstance(entries, list) else entries
            if e is not None:
                return e
    return None


def extract_dimensions(raw_attrs: dict) -> dict:
    """
    Parse SP-API attributes into a normalised dict:
      {
        "length_cm": float | None,
        "width_cm":  float | None,
        "height_cm": float | None,
        "weight_g":  float | None,
        # Convenience copies in inches / lbs for display
        "length_in": float | None,
        "width_in":  float | None,
        "height_in": float | None,
        "weight_lb": float | None,
      }

    Tries package dimensions first (most relevant for FBA storage),
    then falls back to item dimensions.
    """
    result: dict[str, float | None] = {
        "length_cm": None, "width_cm": None, "height_cm": None, "weight_g": None,
        "length_in": None, "width_in": None, "height_in": None, "weight_lb": None,
    }

    # ── Dimensional axes ───────────────────────────────────────────────────
    for dim_key in ("item_package_dimensions", "item_dimensions"):
        dim_list = raw_attrs.get(dim_key) or []
        if not dim_list:
            continue
        dim = dim_list[0] if isinstance(dim_list, list) else dim_list
        if not isinstance(dim, dict):
            continue

        ok = True
        for axis, res_key in (("length", "length_cm"), ("width", "width_cm"), ("height", "height_cm")):
            axis_entry = dim.get(axis)
            if axis_entry is None:
                ok = False
                break
            # axis_entry may be a bare dict or a list
            if isinstance(axis_entry, list):
                axis_entry = axis_entry[0] if axis_entry else None
            val, unit = _read_measure(axis_entry)
            if val is None:
                ok = False
                break
            result[res_key] = _to_cm(val, unit)

        # Weight inside the dimensions block
        w_entry = dim.get("weight")
        if w_entry is not None:
            if isinstance(w_entry, list):
                w_entry = w_entry[0] if w_entry else None
            w_val, w_unit = _read_measure(w_entry)
            if w_val is not None:
                result["weight_g"] = _to_grams(w_val, w_unit)

        if ok:
            break  # Found all three axes — stop trying

    # ── Weight (separate attribute, tried as fallback) ────────────────────
    if result["weight_g"] is None:
        w_entry = _first_entry(raw_attrs, "item_weight", "item_package_weight")
        if w_entry is not None:
            w_val, w_unit = _read_measure(w_entry)
            if w_val is not None:
                result["weight_g"] = _to_grams(w_val, w_unit)

    # ── Derive inch / lb copies for display ──────────────────────────────
    CM_TO_IN = 0.393701
    G_TO_LB  = 0.00220462

    if result["length_cm"] is not None:
        result["length_in"] = round(result["length_cm"] * CM_TO_IN, 3)
    if result["width_cm"] is not None:
        result["width_in"]  = round(result["width_cm"]  * CM_TO_IN, 3)
    if result["height_cm"] is not None:
        result["height_in"] = round(result["height_cm"] * CM_TO_IN, 3)
    if result["weight_g"] is not None:
        result["weight_lb"] = round(result["weight_g"]  * G_TO_LB,  4)

    return result


# --------------------------------------------------------------------------- #
# Size tier + fee calculation (ported from user-supplied Python)
# --------------------------------------------------------------------------- #

def get_size_tier(length_in: float, width_in: float, height_in: float, weight_lb: float) -> str:
    dims = sorted([length_in, width_in, height_in], reverse=True)
    l, m, s = dims
    if l <= 15  and m <= 12 and s <= 0.75 and weight_lb <= 1.0:
        return "Small Standard"
    if l <= 18  and m <= 14 and s <= 8    and weight_lb <= 20:
        return "Large Standard"
    if l <= 37  and m <= 28 and s <= 20   and weight_lb <= 50:
        return "Small Bulky"
    if l <= 59  and m <= 33 and s <= 33   and weight_lb <= 50:
        return "Large Bulky"
    return "Extra Large"


def calc_storage_fee(
    length_cm: float | None,
    width_cm:  float | None,
    height_cm: float | None,
    weight_g:  float | None,
) -> tuple[float | None, float | None]:
    """
    Returns (fee_offpeak, fee_peak_q4) in USD per unit per month.
    Returns (None, None) if any dimension is missing or tier is Extra Large.

    Rates per cubic foot/month:
      Standard:  Jan–Sep $0.78  |  Oct–Dec $2.40
      Bulky:     Jan–Sep $0.56  |  Oct–Dec $1.40
    """
    if any(v is None for v in [length_cm, width_cm, height_cm, weight_g]):
        return None, None

    CM_TO_IN = 0.393701
    G_TO_LB  = 0.00220462

    l = length_cm * CM_TO_IN
    w = width_cm  * CM_TO_IN
    h = height_cm * CM_TO_IN
    weight_lb = weight_g * G_TO_LB

    tier = get_size_tier(l, w, h, weight_lb)
    if tier == "Extra Large":
        return None, None

    cubic_feet = (l * w * h) / 1728

    if tier in ("Small Standard", "Large Standard"):
        return round(cubic_feet * 0.78, 4), round(cubic_feet * 2.40, 4)
    else:  # Small Bulky, Large Bulky
        return round(cubic_feet * 0.56, 4), round(cubic_feet * 1.40, 4)
