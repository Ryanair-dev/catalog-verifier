"""
Walmart Catalog -- item search + a first-cut "Offer Analysis" ROI
calculator (the Walmart equivalent of the Amazon Offer Analysis tab).

Status (2026-09-28): scaffolding only, not yet live-tested -- this
session doesn't have the actual Walmart Client ID/Secret values, only
the user's confirmation that credentials exist and are (probably)
already scoped for catalog search, tested against PRODUCTION per their
explicit choice. Every endpoint here degrades to a clear 400 if
WALMART_CLIENT_ID/WALMART_CLIENT_SECRET aren't in .env yet.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException
from fastapi.concurrency import run_in_threadpool

from services.walmart import config as wm_config
from services.walmart.auth import WalmartTokenManager
from services.walmart.catalog import WalmartCatalogAPI
from services.walmart.offer_analysis import compute_offer_analysis

log = logging.getLogger(__name__)
router = APIRouter()

_client_cache: dict = {}


def _get_client() -> WalmartCatalogAPI:
    creds = wm_config.load_walmart_credentials()  # raises RuntimeError if unset
    key = (creds.client_id, creds.production)
    if key not in _client_cache:
        tokens = WalmartTokenManager(creds.client_id, creds.client_secret, creds.production)
        _client_cache.clear()  # only ever keep the current credential set
        _client_cache[key] = WalmartCatalogAPI(tokens, creds.production)
    return _client_cache[key]


def _extract_price(item: dict) -> float | None:
    """Walmart's item-search price field has shown up as either a flat
    number or a {"amount": ..., "currency": ...} dict in different API
    docs/examples -- handle both defensively until we've seen a real
    live response."""
    price = item.get("price")
    if isinstance(price, dict):
        amount = price.get("amount")
    else:
        amount = price
    try:
        return float(amount) if amount is not None else None
    except (TypeError, ValueError):
        return None


def _extract_category(item: dict) -> str:
    for key in ("category", "primaryCategory", "categoryPath", "productType"):
        v = item.get(key)
        if isinstance(v, str) and v:
            return v
        if isinstance(v, list) and v:
            return str(v[-1])
    return ""


@router.get("/walmart/status")
async def walmart_status() -> dict:
    configured = wm_config.walmart_configured()
    production = None
    if configured:
        try:
            production = wm_config.load_walmart_credentials().production
        except RuntimeError:
            pass
    return {"configured": configured, "production": production}


@router.post("/walmart/search")
async def walmart_search(body: dict = Body(...)) -> dict:
    """Body: {query?, upc?, gtin?, ean?, isbn?} -- one identifier."""
    try:
        client = _get_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    kwargs = {k: (body.get(k) or "").strip() for k in ("query", "upc", "gtin", "ean", "isbn")}
    kwargs = {k: v for k, v in kwargs.items() if v}
    if not kwargs:
        raise HTTPException(status_code=400, detail="Provide one of query/upc/gtin/ean/isbn.")

    try:
        data = await run_in_threadpool(client.search, **kwargs)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return data


@router.post("/walmart/offer-analysis")
async def walmart_offer_analysis(body: dict = Body(...)) -> dict:
    """Body: {items: [{upc?, query?, vendor_cost, category?, other_fees?}]}.

    For each row: search Walmart for the item (an exact-identifier UPC
    lookup when given, else a keyword/title query with a fuzzy relevance
    check -- see services/walmart/catalog.py search_best()/best_match(),
    added 2026-09-29 after a live title search for "L'Oreal Paris Eye
    Makeup Remover" returned an unrelated NYX eyeliner as the top result),
    pull its price, then compute
    net_profit = walmart_price - vendor_cost - referral_fee - other_fees
    using the static referral-rate table in services/walmart/offer_analysis.py
    (Walmart has no live per-item fee-estimate API -- confirmed via research,
    2026-09-28 -- so this mirrors how this app already hardcodes Amazon's
    referral-fee formula rather than calling a nonexistent endpoint)."""
    try:
        client = _get_client()
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    items = body.get("items") or []
    if not items:
        raise HTTPException(status_code=400, detail="Provide at least one item.")

    async def one(row: dict) -> dict:
        upc = (row.get("upc") or "").strip()
        query = (row.get("query") or "").strip()
        vendor_cost = float(row.get("vendor_cost") or 0)
        category_override = (row.get("category") or "").strip()
        other_fees = float(row.get("other_fees") or 0)

        if not upc and not query:
            return {"upc": upc, "query": query, "error": "No UPC or query supplied."}

        match_score = None
        try:
            if upc:
                # A barcode hit is an exact-identifier match, same trust
                # level as Amazon's UPC search -- no relevance filter needed.
                found = await run_in_threadpool(client.search_one, upc=upc)
            else:
                # A keyword/title search is NOT reliably ranked -- verified
                # live 2026-09-29 that item[0] can be a completely unrelated
                # product. search_best() only returns a hit that clears a
                # fuzzy title-similarity floor; otherwise treat as not found.
                found, match_score = await run_in_threadpool(client.search_best, query=query)
        except RuntimeError as exc:
            return {"upc": upc, "query": query, "error": str(exc)[:300]}

        if not found:
            result = compute_offer_analysis(
                walmart_price=None, vendor_cost=vendor_cost,
                category=category_override, other_fees=other_fees,
            )
            note = result.note
            if query and match_score is not None:
                note = (
                    f"No Walmart result matched \"{query}\" closely enough "
                    f"(best title similarity: {match_score:.0f}%)."
                )
            return {
                "upc": upc, "query": query, "title": None, "item_id": None,
                "match_score": match_score,
                **{**result.__dict__, "note": note},
            }

        price = _extract_price(found)
        category = category_override or _extract_category(found)
        result = compute_offer_analysis(
            walmart_price=price, vendor_cost=vendor_cost,
            category=category, other_fees=other_fees,
        )
        return {
            "upc": upc,
            "query": query,
            "title": found.get("title"),
            "item_id": found.get("itemId"),
            "match_score": match_score,
            **result.__dict__,
        }

    import asyncio
    results = await asyncio.gather(*(one(r) for r in items))
    return {"results": results, "total": len(results)}
