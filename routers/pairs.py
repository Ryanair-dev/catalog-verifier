"""
Pair Manager endpoints.

Blacklisted UPC/ASIN pairs accumulate over time. The frontend never loads the
full list on open — everything is search-first. The only write action from
this surface is ``POST /pairs/unlock`` which removes a single pair.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from services import database

router = APIRouter()


class PairQuery(BaseModel):
    query: str


class PairKey(BaseModel):
    upc: str
    asin: str


@router.post("/pairs/search")
async def search_pairs(body: PairQuery) -> dict:
    """Search blacklisted pairs by UPC or ASIN (partial, case-insensitive)."""
    return {"results": database.search_blacklist(body.query)}


@router.post("/pairs/unlock")
async def unlock_pair(body: PairKey) -> dict:
    removed = database.remove_from_blacklist(body.upc, body.asin)
    return {"unlocked": removed > 0}
