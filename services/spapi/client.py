"""
One-line factory for a fully-wired CatalogAPI.

Usage:

    from services.spapi.client import get_catalog_api
    api = get_catalog_api()                 # NA marketplace (default)
    data = api.search_by_identifiers(["012345678905"], id_type="UPC")

The factory is cached per-marketplace so that the LWA token manager (and
its cached access token) is shared across requests within the same
process. Every token refresh hits Amazon and counts against your rate
limits, so we do not want to spin up a fresh manager per call.
"""
from __future__ import annotations

from functools import lru_cache

from .auth import LWATokenManager
from .catalog import CatalogAPI, ENDPOINT_NA, MARKETPLACE_IDS
from .config import load_sp_api_credentials


@lru_cache(maxsize=8)
def get_catalog_api(marketplace: str = "US") -> CatalogAPI:
    """
    Build (or return a cached) CatalogAPI for the given marketplace code.

    Raises RuntimeError if SP-API credentials are not configured — the
    caller (usually a FastAPI route) should convert that into a 400/503
    response with a helpful message.
    """
    creds = load_sp_api_credentials()

    mp_id = MARKETPLACE_IDS.get(marketplace.upper(), creds.marketplace_id)

    token_mgr = LWATokenManager(
        client_id=creds.client_id,
        client_secret=creds.client_secret,
        refresh_token=creds.refresh_token,
    )
    return CatalogAPI(
        token_manager=token_mgr,
        marketplace_id=mp_id,
        endpoint=ENDPOINT_NA,
    )
