"""
SP-API Product Pricing v0 `getItemOffersBatch` -- one specific seller's live
offer per ASIN (2026-10-30, Seller Analytics "what is THAT seller charging").

Amazon returns up to 20 New offers per ASIN (lowest-priced first), each with
SellerId, ListingPrice, Shipping, IsFulfilledByAmazon and IsBuyBoxWinner. 20
ASINs per request; the operation allows 0.1 request/s (burst 1), so 10 s per
batch of 20 -> a 2,000-ASIN storefront takes ~17 min. Costs no Keepa
tokens. Verified live before building: for seller A2LYX7FKX6AP16, 19 of 20
sampled ASINs returned that seller's offer; the 20th had 7 offers in total but
only 6 were returned, so a seller can be missing from the list -- reported as
found=False, never guessed.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Callable

from services.cancellation import JobCancelled, cancellable_sleep
from .auth import LWATokenManager
from .config import load_sp_api_credentials
from .http import thread_session
from .rate_limiter import LIMITERS

log = logging.getLogger(__name__)

ENDPOINT = "https://sellingpartnerapi-na.amazon.com"
BATCH = 20


@lru_cache(maxsize=1)
def _ctx():
    c = load_sp_api_credentials()
    return c, LWATokenManager(client_id=c.client_id, client_secret=c.client_secret,
                              refresh_token=c.refresh_token)


def _amount(m) -> float | None:
    try:
        return float((m or {}).get("Amount"))
    except (TypeError, ValueError):
        return None


def _post_batch(asins: list[str], should_cancel: Callable[[], bool] | None) -> dict:
    creds, tm = _ctx()
    body = {"requests": [{"uri": f"/products/pricing/v0/items/{a}/offers", "method": "GET",
                          "MarketplaceId": creds.marketplace_id, "ItemCondition": "New",
                          "CustomerType": "Consumer"} for a in asins]}
    limiter = LIMITERS["getItemOffersBatch"]
    for attempt in range(6):
        if should_cancel and should_cancel():
            raise JobCancelled()
        limiter.acquire()
        try:
            r = thread_session().post(f"{ENDPOINT}/batches/products/pricing/v0/itemOffers",
                                      headers={"x-amz-access-token": tm.get_token(),
                                               "Content-Type": "application/json"},
                                      data=json.dumps(body), timeout=60)
        except Exception as exc:  # noqa: BLE001 -- network blip, retry
            log.warning("[pricing] network error (attempt %d): %s", attempt + 1, str(exc)[:100])
            cancellable_sleep(min(30, 2 ** attempt), should_cancel)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code in (429, 500, 502, 503, 504):
            cancellable_sleep(min(60, 5 * 2 ** attempt), should_cancel)
            continue
        raise RuntimeError(f"getItemOffersBatch HTTP {r.status_code}: {r.text[:200]}")
    raise RuntimeError("getItemOffersBatch kept failing after retries")


def seller_offers(asins, seller_id: str, on_progress=None, should_cancel=None) -> dict:
    """{asin: {found, price, shipping, fba, buybox, offers_total}} for `seller_id`'s
    New offer on each ASIN. An ASIN whose batch item errored is left out."""
    uniq = list(dict.fromkeys(a.strip().upper() for a in asins if a and a.strip()))
    out: dict = {}
    for i in range(0, len(uniq), BATCH):
        chunk = uniq[i:i + BATCH]
        try:
            data = _post_batch(chunk, should_cancel)
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 -- lose this batch, not the whole run
            log.warning("[pricing] batch %d failed: %s", i // BATCH, str(exc)[:200])
            data = {}
        for resp in data.get("responses") or []:
            p = (resp.get("body") or {}).get("payload") or {}
            asin = (p.get("ASIN") or "").upper()
            if not asin or (resp.get("status") or {}).get("statusCode") != 200:
                continue
            offers = p.get("Offers") or []
            total = (p.get("Summary") or {}).get("TotalOfferCount")
            mine = next((o for o in offers if o.get("SellerId") == seller_id), None)
            if mine:
                out[asin] = {"found": True, "price": _amount(mine.get("ListingPrice")),
                             "shipping": _amount(mine.get("Shipping")) or 0.0,
                             "fba": bool(mine.get("IsFulfilledByAmazon")),
                             "buybox": bool(mine.get("IsBuyBoxWinner")), "offers_total": total}
            else:
                out[asin] = {"found": False, "offers_total": total}
        if on_progress:
            on_progress(min(i + BATCH, len(uniq)), len(uniq))
    return out
