"""
Walmart Marketplace item search — the equivalent of SP-API's Catalog
Items search for Amazon.

`GET /v3/items/walmart/search` searches the ENTIRE Walmart.com catalog
(any seller, not just ours) and returns only published items — up to 40
per query, one of query/upc/gtin/ean/isbn required. This is deliberately
the ONLY endpoint wrapped here for now: `/v3/items/catalog/search` is a
different, seller-scoped endpoint (your own listed items) and isn't
useful for "does this UPC already sell on Walmart, at what price"
research.

Docs: https://developer.walmart.com/us-marketplace/reference/getsearchresult
      https://developer.walmart.com/us-marketplace/docs/item-search-for-the-walmart-catalog
"""
from __future__ import annotations

import logging
import uuid

import requests

from .auth import WalmartTokenManager, SVC_NAME

log = logging.getLogger(__name__)

PROD_ENDPOINT = "https://marketplace.walmartapis.com"
SANDBOX_ENDPOINT = "https://sandbox.walmartapis.com"

SEARCH_PATH = "/v3/items/walmart/search"


class WalmartCatalogAPI:
    def __init__(self, token_manager: WalmartTokenManager, production: bool = True):
        self.tokens = token_manager
        self.endpoint = PROD_ENDPOINT if production else SANDBOX_ENDPOINT

    def _headers(self) -> dict:
        return {
            "WM_SVC.NAME": SVC_NAME,
            "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
            "WM_SEC.ACCESS_TOKEN": self.tokens.get_token(),
            "Accept": "application/json",
        }

    def search(
        self,
        *,
        query: str = "",
        upc: str = "",
        gtin: str = "",
        ean: str = "",
        isbn: str = "",
        max_retries: int = 4,
    ) -> dict:
        """One search call. Exactly one of query/upc/gtin/ean/isbn should be set
        (Walmart's own precedence if more than one is passed is undocumented,
        so callers should pick one identifier per call)."""
        params = {}
        if query:
            params["query"] = query
        if upc:
            params["upc"] = upc
        if gtin:
            params["gtin"] = gtin
        if ean:
            params["ean"] = ean
        if isbn:
            params["isbn"] = isbn
        if not params:
            raise ValueError("search() needs one of query/upc/gtin/ean/isbn")

        backoff = 2
        for attempt in range(max_retries):
            resp = requests.get(
                f"{self.endpoint}{SEARCH_PATH}",
                headers=self._headers(),
                params=params,
                timeout=20,
            )
            if resp.status_code == 429:
                log.warning("[walmart] 429, backing off %ss (attempt %s)", backoff, attempt + 1)
                import time
                time.sleep(backoff)
                backoff = min(30, backoff * 2)
                continue
            try:
                resp.raise_for_status()
            except requests.HTTPError as e:
                raise RuntimeError(
                    f"Walmart item search failed [{resp.status_code}]: {resp.text[:300]}"
                ) from e
            return resp.json()

        raise RuntimeError(f"Walmart item search: exhausted {max_retries} retries on 429")

    def search_one(self, **kwargs) -> dict | None:
        """Convenience: return the first result item, or None if nothing found."""
        data = self.search(**kwargs)
        items = data.get("items") or []
        return items[0] if items else None
