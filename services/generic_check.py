"""
Generic-ASIN / DOG classifier via SP-API — NEVER creates a listing.

For each ASIN:
  - getCatalogItem returns 404          -> "DOG"          (delisted / dead ASIN)
  - else putListingsItem with
    mode=VALIDATION_PREVIEW (offer-only) validates WITHOUT persisting. Amazon returns
    issue code 5885 (Generics Policy / "SB85") for a generic ASIN you may not
    contribute to  -> "GENERIC"; otherwise                -> "NOT_GENERIC" (a real,
    listable branded product — verified: even brand-gated ASINs come back clean).

VALIDATION_PREVIEW is non-persisting, so nothing is ever added to the catalog.
Requires SP-API credentials WITH the Listings role + AMZ_SELLER_ID.
"""
from __future__ import annotations

import json
import logging
import time
from functools import lru_cache

import requests

from services.spapi.auth import LWATokenManager
from services.spapi.client import get_catalog_api
from services.spapi.config import load_sp_api_credentials
from services.spapi.rate_limiter import LIMITERS

log = logging.getLogger(__name__)

ENDPOINT = "https://sellingpartnerapi-na.amazon.com"
GENERIC_ISSUE_CODE = "5885"   # Amazon Generics Policy (a.k.a. SB85) contribution block


@lru_cache(maxsize=1)
def _ctx() -> tuple:
    creds = load_sp_api_credentials()
    if not creds.seller_id:
        raise RuntimeError("AMZ_SELLER_ID not set")
    tm = LWATokenManager(
        client_id=creds.client_id,
        client_secret=creds.client_secret,
        refresh_token=creds.refresh_token,
    )
    return creds, tm


def _product_type(asin: str) -> tuple[str | None, bool]:
    """(productType, is_dog). is_dog=True when the ASIN is not in the catalog (404)."""
    api = get_catalog_api()
    try:
        raw = api.get_by_asin(asin, included_data="productTypes,summaries")
    except requests.HTTPError as exc:
        if getattr(exc.response, "status_code", None) == 404:
            return None, True
        raise
    pts = raw.get("productTypes") or []
    pt = pts[0].get("productType") if pts else None
    if not pt:
        pt = (raw.get("summaries") or [{}])[0].get("productType")
    return pt, False


def _validation_preview(asin: str, product_type: str | None) -> dict:
    """Dry-run offer-only submission (mode=VALIDATION_PREVIEW). Nothing is persisted."""
    creds, tm = _ctx()
    mp, seller = creds.marketplace_id, creds.seller_id
    body = {
        "productType": product_type or "PRODUCT",
        "requirements": "LISTING_OFFER_ONLY",
        "attributes": {
            "merchant_suggested_asin": [{"value": asin, "marketplace_id": mp}],
            "condition_type": [{"value": "new_new", "marketplace_id": mp}],
            "purchasable_offer": [{
                "currency": "USD", "marketplace_id": mp,
                "our_price": [{"schedule": [{"value_with_tax": 19.99}]}],
            }],
            "fulfillment_availability": [{"fulfillment_channel_code": "DEFAULT", "quantity": 1}],
        },
    }
    limiter = LIMITERS.get("putListingsItem")
    for attempt in range(6):
        if limiter:
            limiter.acquire()
        try:
            resp = requests.put(
                f"{ENDPOINT}/listings/2021-08-01/items/{seller}/VPGEN-{asin}",
                headers={"x-amz-access-token": tm.get_token(), "Content-Type": "application/json"},
                params={"marketplaceIds": mp, "mode": "VALIDATION_PREVIEW", "issueLocale": "en_US"},
                data=json.dumps(body), timeout=30,
            )
        except requests.RequestException as exc:
            # transient network error — retry rather than making the ASIN an ERROR
            time.sleep(min(30, 2 ** attempt))
            log.warning("[net] putListingsItem %s: %s", asin, str(exc)[:80])
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            time.sleep(min(30, 2 ** attempt))
            continue
        if resp.status_code == 403:
            raise PermissionError(
                "403 on putListingsItem — the SP-API app needs the 'Listings' role."
            )
        resp.raise_for_status()
        return resp.json()
    raise RuntimeError("putListingsItem throttled after retries")


def classify_generic(asin: str) -> dict:
    """Classify one ASIN. Returns {asin, status, detail} where status is one of
    DOG | GENERIC | NOT_GENERIC | ERROR. Never creates a listing."""
    asin = (asin or "").strip().upper()
    try:
        pt, is_dog = _product_type(asin)
        if is_dog:
            return {"asin": asin, "status": "DOG",
                    "detail": "Not in Amazon catalog (delisted / dead ASIN)"}
        j = _validation_preview(asin, pt)
        codes = [str(i.get("code")) for i in (j.get("issues") or [])]
        if GENERIC_ISSUE_CODE in codes:
            return {"asin": asin, "status": "GENERIC",
                    "detail": "Generics Policy block (SB85 / issue 5885)"}
        return {"asin": asin, "status": "NOT_GENERIC",
                "detail": f"listable ({j.get('status')})"}
    except PermissionError as exc:
        return {"asin": asin, "status": "ERROR", "detail": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"asin": asin, "status": "ERROR", "detail": str(exc)[:150]}
