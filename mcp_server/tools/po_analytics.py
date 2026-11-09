from __future__ import annotations

import asyncio
from typing import Any, Literal

from mcp_server.app import mcp
from mcp_server.mcp_client import AppClientError, app_client


@mcp.tool()
async def po_watchlist() -> dict[str, Any]:
    """Show POs on the automated PO-analysis watchlist and their tracked status."""
    result = await app_client.get("/api/po-analytics/automation/watchlist")
    if not isinstance(result, dict) or not isinstance(result.get("watchlist"), list):
        raise AppClientError("PO Analytics returned an invalid watchlist.")
    return result


@mcp.tool()
async def analyze_po(po_numbers: str, confirm: bool = False) -> dict[str, Any]:
    """Analyze one or more SellerCloud PO numbers and create an Excel report. Requires confirm=true because it starts a background job that reads live SellerCloud/Keepa/SP-API data."""
    po_numbers = po_numbers.strip()
    if not po_numbers:
        raise AppClientError("Provide at least one PO number.")
    if not confirm:
        return {
            "confirmation_required": True,
            "po_numbers": po_numbers,
            "message": "No analysis started. Confirm to let the app parse these PO numbers and run the report.",
        }
    result = await app_client.post(
        "/api/po-analytics/start",
        {"po_numbers": po_numbers},
        timeout=90,
    )
    job_id = result.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise AppClientError("PO Analytics did not return a job ID.")
    return {
        "confirmation_required": False,
        "started": True,
        "po_numbers": result.get("po_numbers"),
        "job_id": job_id,
        "next_step": f"Poll job_status(job_id='{job_id}', kind='po_analytics') for progress and the completed workbook.",
    }


@mcp.tool()
async def po_pnl(
    purchaser: str = "",
    vendor: str = "",
    po_number: str = "",
    brand: str = "",
    item: str = "",
    bucket: Literal["active", "sold_out", "all"] = "active",
    include_items: bool = False,
) -> dict[str, Any]:
    """Read PO P&L from the Reports API. The app owns all report calculations."""
    params = {
        "purchaser": purchaser,
        "vendor": vendor,
        "po_number": po_number,
        "brand": brand,
        "item": item,
        "include_items": str(include_items).lower(),
    }

    async def get_report(app_bucket: str) -> dict[str, Any]:
        result = await app_client.get(
            "/api/reports/po-pnl",
            params={**params, "bucket": app_bucket},
            timeout=180,
        )
        if not isinstance(result, dict):
            raise AppClientError("PO P&L report returned an invalid response.")
        return result

    if bucket == "all":
        active, sold_out = await asyncio.gather(
            get_report("active"),
            get_report("archive"),
        )
        return {"active": active, "sold_out": sold_out}
    return await get_report("archive" if bucket == "sold_out" else "active")
