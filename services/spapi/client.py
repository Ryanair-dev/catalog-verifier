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
from .config import known_stores, load_sp_api_credentials
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


_lease_lock = threading.Lock()
_lease_counts: dict[str, int] = {}


class CatalogStoreLease:
    """A held claim on one store's SP-API rate budget for the lifetime of one
    batch job. Use as a context manager so the lease is always released, even
    if the job raises:

        with lease_catalog_api() as capi:
            ...  # the whole batch job's getCatalogItem/searchCatalogItems calls
    """

    def __init__(self, store: str, api: CatalogAPI):
        self.store = store
        self.api = api
        self._released = False

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        with _lease_lock:
            _lease_counts[self.store] = max(0, _lease_counts.get(self.store, 0) - 1)

    def __enter__(self) -> CatalogAPI:
        return self.api

    def __exit__(self, *exc_info) -> None:
        self.release()


def lease_catalog_api(marketplace: str = "US") -> CatalogStoreLease:
    """
    Hand out a CatalogAPI pinned to whichever CONFIGURED store currently has
    the fewest other batch jobs leasing it, so two getCatalogItem-heavy
    pipelines running at the same time (e.g. a Vendor Offers export
    overlapping with a PO Analytics report or a Brand Analytics run) land on
    two DIFFERENT seller accounts' rate-limit buckets instead of both
    fighting over the single "default" store's tight 2 req/sec getCatalogItem
    ceiling -- per user request (2026-10-20): "if there are like 2 get
    catalog requests using at the same time, then we split it... one uses
    PPP and the second uses turba."

    A lone job still gets "default" (Priority Pharmacy) only -- Turba is
    handed out solely because something else already holds the lease on
    "default" at the moment this is called, never just to go faster for a
    single job. This is a coarser, per-JOB assignment, distinct from
    `get_multi_store_catalog_api`'s per-REQUEST round-robin (used by catalog
    SEARCH to roughly double a single job's own throughput) -- here, one
    job's whole run stays pinned to one store's credentials/budget for its
    duration, so two concurrent jobs genuinely never share a bucket.

    The caller MUST release the lease when the job ends. Use as a context
    manager (`with lease_catalog_api() as capi:`) so this happens even on an
    exception; `CatalogStoreLease.release()` is also safe to call directly
    and is idempotent.
    """
    with _lease_lock:
        candidates: list[str] = []
        for store in known_stores():
            try:
                get_catalog_api(marketplace, store)  # cheap after first call (lru_cache); raises if unconfigured
            except RuntimeError:
                continue
            candidates.append(store)
        if not candidates:
            # No store configured at all -- let the real "not configured" error surface.
            get_catalog_api(marketplace, "default")
        store = min(candidates, key=lambda s: (_lease_counts.get(s, 0), s != "default"))
        _lease_counts[store] = _lease_counts.get(store, 0) + 1
    return CatalogStoreLease(store, get_catalog_api(marketplace, store))


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
