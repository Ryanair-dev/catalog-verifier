"""
One-line factory for a fully-wired CatalogAPI.

Usage:

    from services.spapi.client import get_catalog_api
    api = get_catalog_api()                 # NA marketplace (default)
    data = api.search_by_identifiers(["012345678905"], id_type="UPC")

The factory is cached per-marketplace (and per-store) so that the LWA token
manager (and its cached access token) is shared across requests within the
same process. Every token refresh hits Amazon and counts against your rate
limits, so we do not want to spin up a fresh manager per call.

Multi-store search (2026-10-02): `get_multi_store_catalog_api()` returns a
`MultiStoreCatalogAPI` that round-robins catalog SEARCH calls (not
eligibility — that's genuinely seller-scoped, see services/restrictions.py)
across every configured store. Catalog search results are Amazon's PUBLIC
catalog data, identical regardless of which seller account's credentials
make the call — so alternating stores doesn't change what's found, it just
draws from two independent per-store rate-limit buckets instead of one,
roughly doubling sustained throughput for a long batch search.
"""
from __future__ import annotations

import itertools
import threading
from functools import lru_cache

from .auth import LWATokenManager
from .catalog import CatalogAPI, ENDPOINT_NA, MARKETPLACE_IDS
from .config import load_sp_api_credentials
from .rate_limiter import new_limiters


@lru_cache(maxsize=16)
def get_catalog_api(marketplace: str = "US", store: str = "default") -> CatalogAPI:
    """
    Build (or return a cached) CatalogAPI for the given marketplace code
    and store (seller account — see services/spapi/config._STORES).

    Raises RuntimeError if that store's SP-API credentials are not
    configured — the caller (usually a FastAPI route) should convert that
    into a 400/503 response with a helpful message.
    """
    creds = load_sp_api_credentials(store)

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
        # "default" keeps using the shared module-level LIMITERS (no change
        # for any existing single-store caller); any OTHER store gets its own
        # independent set so its rate budget never shares with "default"'s.
        limiters=None if store == "default" else new_limiters(),
    )


class MultiStoreCatalogAPI:
    """Round-robins `search_by_identifiers` / `search_by_keywords` /
    `get_by_asin` across however many CatalogAPI clients were successfully
    built (one per configured store). Duck-types CatalogAPI's three search
    methods, so it drops straight into any existing call site that takes an
    `api: CatalogAPI` and calls one of those methods — no caller-side change
    needed beyond how the client is constructed.

    Thread-safe: a lock guards the round-robin counter so concurrent callers
    (if a future caller parallelises across workers) don't race on `next()`."""

    def __init__(self, clients: list[CatalogAPI]):
        if not clients:
            raise RuntimeError("MultiStoreCatalogAPI needs at least one CatalogAPI client")
        self._clients = clients
        self._lock = threading.Lock()
        self._cycle = itertools.cycle(range(len(clients)))

    def _next_client(self) -> CatalogAPI:
        with self._lock:
            idx = next(self._cycle)
        return self._clients[idx]

    def search_by_identifiers(self, *args, **kwargs):
        return self._next_client().search_by_identifiers(*args, **kwargs)

    def search_by_keywords(self, *args, **kwargs):
        return self._next_client().search_by_keywords(*args, **kwargs)

    def search_by_brand_names(self, *args, **kwargs):
        return self._next_client().search_by_brand_names(*args, **kwargs)

    def get_by_asin(self, *args, **kwargs):
        return self._next_client().get_by_asin(*args, **kwargs)


def get_multi_store_catalog_api(
    marketplace: str = "US", stores: tuple[str, ...] = ("default", "turba"),
) -> MultiStoreCatalogAPI:
    """Build (or reuse cached per-store clients for) a round-robin catalog API
    spanning every store in `stores` that's actually configured. Silently
    skips any store whose credentials aren't set (e.g. Turba not yet added)
    so this degrades gracefully to single-store behaviour — raises only if
    NONE of the requested stores are configured."""
    clients: list[CatalogAPI] = []
    for store in stores:
        try:
            clients.append(get_catalog_api(marketplace, store))
        except RuntimeError:
            continue
    if not clients:
        # Surface the default store's real error message (the common case:
        # no SP-API credentials configured at all).
        get_catalog_api(marketplace, "default")
    return MultiStoreCatalogAPI(clients)
