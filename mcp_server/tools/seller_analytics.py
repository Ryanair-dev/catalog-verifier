from __future__ import annotations

from typing import Any

from mcp_server.app import mcp
from mcp_server.mcp_client import AppClientError, app_client


@mcp.tool()
async def seller_analysis(
    seller_id: str,
    target_roi: float = 30.0,
    confirm: bool = False,
) -> dict[str, Any]:
    """Analyze a seller's Amazon catalog and prepare an Excel export. The first call looks up the seller and asks for confirmation; confirm=true starts the long-running job."""
    seller_id = seller_id.strip()
    if not seller_id or len(seller_id) > 100:
        raise AppClientError("seller_id is required and must be 100 characters or fewer.")
    if isinstance(target_roi, bool) or not isinstance(target_roi, (int, float)):
        raise AppClientError("target_roi must be a number.")
    if not 0 <= target_roi <= 500:
        raise AppClientError("target_roi must be between 0 and 500 percent.")
    if not confirm:
        lookup = await app_client.post(
            "/api/seller-analytics/lookup", {"seller_id": seller_id}, timeout=90,
        )
        if lookup.get("error"):
            raise AppClientError(f"Seller lookup failed: {str(lookup['error'])[:250]}")
        return {
            "confirmation_required": True,
            "seller": lookup,
            "target_roi": target_roi,
            "cost_note": (
                "The seller lookup uses about 10 Keepa tokens. The full export's time and "
                "Keepa/SP-API usage vary with the seller's catalog size."
            ),
            "message": "No export job was started. Call seller_analysis again with confirm=true to start it.",
        }

    started = await app_client.post(
        "/api/seller-analytics/start",
        {"seller_id": seller_id, "roi_pct": float(target_roi), "live": True},
        timeout=90,
    )
    job_id = started.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise AppClientError("Seller Analytics did not return a job ID.")
    return {
        "confirmation_required": False,
        "started": True,
        "seller_id": seller_id,
        "target_roi": target_roi,
        "job_id": job_id,
        "next_step": f"Poll job_status(job_id='{job_id}', kind='seller_analytics') for progress and the completed workbook.",
    }
