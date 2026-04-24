"""
SP-API Catalog Items v2022-04-01 wrapper.

Ported from asin-scraper/scripts/catalog.py with two differences:

  1. ENDPOINT + MARKETPLACE_ID are no longer module constants. They are
     defaults on the class, and each instance can override them. That
     keeps the NA-first behaviour intact while letting future callers
     swap in EU / FE / UK without editing this file.

  2. The relative import `from scripts.rate_limiter import LIMITERS`
     is now a package-relative import so the module works when the
     catalog-verifier service imports it.

Throttling, 429 backoff, and pagination logic are unchanged.
"""
from __future__ import annotations

import logging
import time
from typing import Iterable

import requests

from .rate_limiter import LIMITERS

log = logging.getLogger(__name__)

# North America defaults — the only region asin-scraper ever used.
ENDPOINT_NA       = "https://sellingpartnerapi-na.amazon.com"
MARKETPLACE_NA    = "ATVPDKIKX0DER"

# Convenience: other marketplaces we may add later.
MARKETPLACE_IDS = {
    "US": "ATVPDKIKX0DER",
    "CA": "A2EUQ1WTGCTBG2",
    "MX": "A1AM78C64UM0Y8",
    "BR": "A2Q3Y263D00KWC",
}


class CatalogAPI:
    """
    Wraps the SP-API Catalog Items v2022-04-01 endpoint.

    - Auth via LWATokenManager (token header is injected per request).
    - Throttling via the shared LIMITERS token buckets.
    - 429 handling with exponential backoff.
    - Pagination via nextToken.
    """

    def __init__(
        self,
        token_manager,
        marketplace_id: str = MARKETPLACE_NA,
        endpoint: str = ENDPOINT_NA,
    ):
        self.tokens = token_manager
        self.marketplace_id = marketplace_id
        self.endpoint = endpoint

    # ── Internal helpers ─────────────────────────────────────────────────

    def _headers(self) -> dict:
        return {
            "x-amz-access-token": self.tokens.get_token(),
            "Content-Type":       "application/json",
        }

    def _get(
        self,
        path: str,
        params: dict,
        operation: str,
        max_retries: int = 5,
    ) -> requests.Response:
        limiter = LIMITERS.get(operation)

        for attempt in range(max_retries):
            if limiter:
                limiter.acquire()

            resp = requests.get(
                f"{self.endpoint}{path}",
                headers=self._headers(),
                params=params,
                timeout=15,
            )

            if resp.status_code == 200:
                return resp

            elif resp.status_code == 429:
                wait = 2 ** attempt
                log.warning(
                    "[429] Throttled on %s (attempt %d). Backing off %ss",
                    operation, attempt + 1, wait,
                )
                time.sleep(wait)

            elif resp.status_code == 403:
                raise PermissionError(
                    f"403 on {operation} — check SP-API app roles/scopes."
                )

            elif resp.status_code == 400:
                raise ValueError(
                    f"400 Bad Request on {operation}: {resp.text[:200]}"
                )

            else:
                resp.raise_for_status()

        raise RuntimeError(f"Max retries ({max_retries}) exceeded for {operation}")

    # ── Public search methods ────────────────────────────────────────────

    def search_by_identifiers(
        self,
        identifiers: Iterable[str],
        id_type: str = "UPC",
        included_data: str = "summaries,identifiers,attributes,salesRanks",
    ) -> dict:
        """
        Search by UPC, EAN, JAN, ISBN, or GTIN. Up to 20 identifiers per call.
        """
        ids = list(identifiers)
        if len(ids) > 20:
            raise ValueError("Max 20 identifiers per call. Batch your inputs.")

        params = {
            "marketplaceIds":  self.marketplace_id,
            "identifiers":     ",".join(ids),
            "identifiersType": id_type,
            "includedData":    included_data,
        }
        resp = self._get("/catalog/2022-04-01/items", params, "searchCatalogItems")
        return resp.json()

    def search_by_keywords(
        self,
        keywords: str,
        page_token: str | None = None,
        page_size: int = 20,
        included_data: str = "summaries,identifiers,attributes,salesRanks",
    ) -> dict:
        """
        Keyword search — brand, title, MPN, or any free text.
        Pass `page_token` from a previous response's `pagination.nextToken`
        to walk through pages.
        """
        params = {
            "marketplaceIds": self.marketplace_id,
            "keywords":       keywords,
            "includedData":   included_data,
            "pageSize":       page_size,
        }
        if page_token:
            params["pageToken"] = page_token
        resp = self._get("/catalog/2022-04-01/items", params, "searchCatalogItems")
        return resp.json()

    def search_all_pages(
        self,
        keywords: str,
        max_pages: int = 10,
    ) -> list[dict]:
        """
        Convenience wrapper that auto-paginates search_by_keywords.
        max_pages is a safety cap against runaway keyword searches.
        """
        all_items: list[dict] = []
        page_token: str | None = None

        for _ in range(max_pages):
            data = self.search_by_keywords(keywords, page_token=page_token)
            items = data.get("items", [])
            all_items.extend(items)

            page_token = data.get("pagination", {}).get("nextToken")
            if not page_token:
                break

        return all_items

    def get_by_asin(
        self,
        asin: str,
        included_data: str = "summaries,identifiers,attributes,salesRanks,images",
    ) -> dict:
        """Fetch full details for a single known ASIN."""
        params = {
            "marketplaceIds": self.marketplace_id,
            "includedData":   included_data,
        }
        resp = self._get(
            f"/catalog/2022-04-01/items/{asin}",
            params,
            "getCatalogItem",
        )
        return resp.json()
