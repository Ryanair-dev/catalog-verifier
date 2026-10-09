"""Switches for the normalization fixes. 

Read once at import, so set them in the environment BEFORE starting the app or
the quality eval. Both default to ON.

To reproduce the OLD behaviour (baseline run for before/after comparison):

    MATCH_CLEAN_TITLES=0 MATCH_EXTENDED_COUNTS=0
"""
from __future__ import annotations

import os


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
EXTENDED_COUNTS = _on("MATCH_EXTENDED_COUNTS")