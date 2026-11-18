"""Switches for the normalization fixes. 
# OFF until the classifier is retrained and the cascade's 72 threshold is re-picked
# on cleaned titles. Switching it on alone made the eval worse (classifier 6 -> 39 wrong approvals).
"""
from __future__ import annotations

import os

def category_check_enabled() -> bool:
    """Optional check: send a pair to review when its two product categories are far apart. Default OFF."""
    return _on("MATCH_CATEGORY_CHECK", default=False)

def _on(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in ("0", "false", "no", "off", "")


# Same cleaning on both titles before comparing: punctuation and hyphens become
# spaces, numbers and units are split ("4pcs" -> "4 pcs").
CLEAN_TITLES = _on("MATCH_CLEAN_TITLES", default=False)

# Read "4PCS", "12 ct", "4 Count" as pack counts on BOTH sides (not just "Pack of 4"),
# and use Amazon's own pack-quantity field when the Amazon title has no count.
EXTENDED_COUNTS = _on("MATCH_EXTENDED_COUNTS", default=False)

def unified_attrs_enabled() -> bool:
    """Shared variant check (color, clothing size, units, flavor, scent) applied to the result of
    ALL THREE scorers. Default OFF. Switch on with  MATCH_UNIFIED_ATTRS=1  in .env."""
    return _on("MATCH_UNIFIED_ATTRS", default=True)