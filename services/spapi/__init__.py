"""
SP-API client package.

Ported from the standalone `asin-scraper` project with two changes to
make it safe to embed inside the catalog-verifier FastAPI app:

  1. Marketplace endpoint + id are parameters on CatalogAPI rather than
     module-level constants, so we can support EU / FE / UK later
     without forking the file.
  2. Relative imports (`from scripts.rate_limiter import LIMITERS`) are
     rewritten as package-relative imports so the code works no matter
     how the catalog-verifier is launched.

Do NOT rewrite the rate-limiter numbers or the SP-API endpoints here —
they come from Amazon's own published throttle docs.
"""
from .auth import LWATokenManager
from .catalog import CatalogAPI, MARKETPLACE_NA, ENDPOINT_NA, MARKETPLACE_IDS
from .rate_limiter import LIMITERS, TokenBucketLimiter
from .config import load_sp_api_credentials, sp_api_configured, SPAPICredentials
from .client import get_catalog_api

__all__ = [
    "LWATokenManager",
    "CatalogAPI",
    "MARKETPLACE_NA",
    "ENDPOINT_NA",
    "MARKETPLACE_IDS",
    "LIMITERS",
    "TokenBucketLimiter",
    "load_sp_api_credentials",
    "sp_api_configured",
    "SPAPICredentials",
    "get_catalog_api",
]
