"""
Analytics pipeline — port of AmazonAsinResearch1 search + vetting logic
into the catalog-verifier.

Modules:
  matcher  — confidence scoring (UPC/Brand/Title/MPN weights)
  parser   — vendor file -> source rows (with user-picked header & mapping)
  runner   — 3-tier SP-API search + per-candidate scoring (background worker)
"""
from .matcher import calculate_confidence, score_amazon_item
from .parser import parse_source_rows, SourceRow
from .runner import start_analytics_run, normalize_amazon_item

__all__ = [
    "calculate_confidence",
    "score_amazon_item",
    "parse_source_rows",
    "SourceRow",
    "start_analytics_run",
    "normalize_amazon_item",
]
