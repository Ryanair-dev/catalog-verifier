"""
Reports tab -- the first dashboard is PO P&L (services/reports_po_pnl.py).
Future dashboards register their own GET endpoints under this same
`/api/reports/...` prefix as they're built.

GET /api/reports/po-pnl         -- PO-level bought/sold/left, units + dollars
                                    (PO totals only; items via /po-pnl/items)
GET /api/reports/po-pnl/items   -- one PO's item lines, loaded when its row is expanded
GET /api/reports/po-pnl/filters -- distinct vendors/brands to populate the
                                    dashboard's filter dropdowns
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.concurrency import run_in_threadpool

from services import reports_po_pnl as pnl

router = APIRouter()


@router.get("/reports/po-pnl")
async def reports_po_pnl(
    purchaser: str = Query(pnl.DEFAULT_PURCHASERS),
    vendor: str = Query(""),
    po_number: str = Query(""),
    brand: str = Query(""),
    item: str = Query(""),
    bucket: str = Query("active"),
    refresh: bool = Query(False),
    include_items: bool = Query(False),
) -> dict:
    return await run_in_threadpool(
        pnl.get_po_pnl, purchaser, vendor, po_number, brand, item, bucket, refresh, include_items)


@router.get("/reports/po-pnl/items")
async def reports_po_pnl_items(
    po_number: str = Query(...),
    purchaser: str = Query(pnl.DEFAULT_PURCHASERS),
    brand: str = Query(""),
    item: str = Query(""),
) -> dict:
    items = await run_in_threadpool(pnl.get_po_items, purchaser, po_number, brand, item)
    return {"po_number": po_number, "items": items}


@router.get("/reports/po-pnl/filters")
async def reports_po_pnl_filters(purchaser: str = Query(pnl.DEFAULT_PURCHASERS)) -> dict:
    return await run_in_threadpool(pnl.get_filter_options, purchaser)
