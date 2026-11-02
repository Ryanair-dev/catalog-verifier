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
from rapidfuzz import fuzz

from .auth import WalmartTokenManager, SVC_NAME
from services.analytics.matcher import _size_mismatch  # reused, see best_match()

log = logging.getLogger(__name__)

# Below this token_set_ratio (0-100) against the query, a title/keyword
# search result is treated as "no match" rather than trusted -- Walmart's
# keyword search does NOT guarantee item[0] is actually the right product
# (verified live 2026-09-29: querying "L'Oreal Paris Eye Makeup Remover"
# returned an unrelated NYX eyeliner pencil as the first result). A UPC/
# GTIN/EAN/ISBN search is a real barcode lookup, not a keyword match, so
# it does NOT go through this relevance filter -- only query-based search
# needs it.
MIN_TITLE_MATCH_RATIO = 55.0

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
        """Convenience: return the first result item, or None if nothing found.
        Only safe for barcode-identified searches (upc/gtin/ean/isbn), where
        Amazon-style "exact identifier = same product" logic applies. For a
        plain keyword `query`, use `search_best()` instead -- see its
        docstring for why item[0] can't be trusted there."""
        data = self.search(**kwargs)
        items = data.get("items") or []
        return items[0] if items else None

    def search_best(
        self, *, query: str, min_ratio: float = MIN_TITLE_MATCH_RATIO, max_retries: int = 4,
    ) -> tuple[dict | None, float, str]:
        """Title/keyword search WITH a relevance check. Returns
        (best_item_or_None, best_score, exclude_reason) -- best_item is None
        when no result clears `min_ratio` (fuzzy token_set_ratio, 0-100, of
        the query against each candidate's title), has a matching size, and
        actually carries a Walmart `price` -- see `best_match()`'s docstring
        for what `exclude_reason` means."""
        data = self.search(query=query, max_retries=max_retries)
        items = data.get("items") or []
        return best_match(items, query, min_ratio)


def best_match(
    items: list[dict], query: str, min_ratio: float = MIN_TITLE_MATCH_RATIO,
) -> tuple[dict | None, float, str]:
    """Pick the item whose title best fuzzy-matches `query`; only returns it
    (non-None) when the score clears `min_ratio`, its size doesn't
    contradict the query's, AND it actually carries a `price`.

    Three distinct failure modes, each verified live against the real
    Walmart search API, each needing its own guard:
    (1) 2026-09-29 -- a query for "L'Oreal Paris Eye Makeup Remover"
        returned an unrelated NYX eyeliner as item[0] -- pure text
        mismatch, caught by `min_ratio`.
    (2) 2026-09-29 -- a query for "L'Oreal Paris Eye Makeup Remover 0.4
        fluid ounces" matched a REAL L'Oreal eye-makeup-remover listing
        that was actually "4 fl oz" -- a 10x size difference the text-
        similarity score barely penalises, since every other word
        overlaps. `_size_mismatch` (reused verbatim from the Amazon
        matcher, `services/analytics/matcher.py`) catches this.
    (3) 2026-09-30 -- Walmart's keyword search frequently returns items
        with NO `price` field at all: of 39 real results for "tide pods",
        only 16 carried a price, and the single HIGHEST-scoring title
        match was one of the 23 that didn't (a UPC-identifier search
        always includes `price` -- this is specific to keyword search).
        A price-less item is useless for Offer Analysis (net_profit/roi
        both come out blank), so it can't be returned as `best_item`.

    `exclude_reason` is `""` when `best_item` is returned; otherwise one of
    `"size"` or `"no_price"`, naming which guard excluded the single best
    TEXT match overall (so the caller can report "found the right product,
    wrong size" vs. "found it, but Walmart didn't give a price for it" --
    two different, differently-actionable outcomes) -- or `""` again when
    nothing cleared `min_ratio` at all (a genuine no-match, not an
    exclusion)."""
    if not items:
        return None, 0.0, ""
    q = (query or "").strip().lower()
    best_item, best_score = None, 0.0
    best_overall_item, best_overall_score = None, 0.0
    for item in items:
        raw_title = item.get("title") or ""
        title = raw_title.strip().lower()
        if not title:
            continue
        score = fuzz.token_set_ratio(q, title)
        if score > best_overall_score:
            best_overall_score, best_overall_item = score, item
        if "price" not in item:
            continue
        if _size_mismatch(query, raw_title):
            continue
        if score > best_score:
            best_item, best_score = item, score
    if best_item is not None and best_score >= min_ratio:
        return best_item, best_score, ""
    if best_overall_score < min_ratio:
        return None, best_overall_score, ""
    reason = "no_price" if "price" not in (best_overall_item or {}) else "size"
    return None, best_overall_score, reason
