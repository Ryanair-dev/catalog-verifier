"""
MPN Check -- paste MPNs (+ manufacturer/brand), look up fields (qty/case for
now) against a local mirror of Ford Medical's internal "fbidb" Azure database.
See services/mpn_check.py (the check) and services/mpn_check_sync.py (the
local mirror + hourly sync).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Body, HTTPException
from fastapi.concurrency import run_in_threadpool

from services import ai_recheck
from services import mpn_check as mc
from services import mpn_check_sync as sync

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/mpn-check")
async def mpn_check(body: dict = Body(...)) -> dict:
    """Body: {pairs: [{mpn, manufacturer}], fields?: ["qty_case"]}."""
    pairs = [((r.get("mpn") or ""), (r.get("manufacturer") or "")) for r in (body.get("pairs") or [])]
    fields = set(body.get("fields") or ["qty_case"])
    results = await run_in_threadpool(mc.check_mpns, pairs, fields)
    return {"results": results, "matched": sum(1 for r in results if r["matched"]), "total": len(results)}


@router.get("/mpn-check/sync-status")
async def mpn_check_sync_status() -> dict:
    return await run_in_threadpool(sync.scheduler_status)


@router.post("/mpn-check/sync")
async def mpn_check_sync_run(body: dict = Body(default=None)) -> dict:
    """Manual sync now. Body optional: {full: false} (full=true forces a
    from-scratch pull instead of the usual incremental-by-updated_at)."""
    full = bool((body or {}).get("full"))
    try:
        result = await run_in_threadpool(sync.sync_all, full)
        return {"ok": True, **result}
    except Exception as exc:  # noqa: BLE001
        log.exception("[mpn_check] manual sync failed")
        raise HTTPException(status_code=500, detail=f"Sync failed: {str(exc)[:200]}")


@router.post("/mpn-check/ai-fallback")
async def mpn_check_ai_fallback(body: dict = Body(...)) -> dict:
    """Separate, opt-in AI web-search lookup for rows the local mirror
    couldn't answer. Body: {pairs: [{mpn, manufacturer}]}. Runs a few requests
    concurrently since each is a real web-search-backed LLM call, not a fast
    DB read."""
    import asyncio

    client = ai_recheck.make_client()
    if client is None:
        raise HTTPException(status_code=400, detail="ANTHROPIC_API_KEY not configured.")

    pairs = [((r.get("mpn") or "").strip(), (r.get("manufacturer") or "").strip())
             for r in (body.get("pairs") or [])]
    pairs = [(m, b) for m, b in pairs if m and b]
    if not pairs:
        return {"results": []}

    sem = asyncio.Semaphore(4)

    async def one(mpn: str, mfr: str) -> dict:
        async with sem:
            r = await run_in_threadpool(mc.ai_lookup_qty_case, mpn, mfr, client)
        return {"mpn": mpn, "manufacturer": mfr, **r}

    results = await asyncio.gather(*(one(m, b) for m, b in pairs))
    return {"results": results}
