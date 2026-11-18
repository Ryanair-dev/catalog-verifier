from __future__ import annotations

from typing import Any, Literal

from mcp_server.app import mcp
from mcp_server.mcp_client import AppClientError, app_client


def _positive_id(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AppClientError(f"{name} must be a positive integer.")
    return value


@mcp.tool()
async def brand_runs(limit: int = 10) -> dict[str, Any]:
    """List recent Brand Analytics runs and their progress without loading item details."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise AppClientError("limit must be an integer from 1 to 50.")
    response = await app_client.get("/api/brand-analytics/runs")
    if not isinstance(response, dict) or not isinstance(response.get("runs"), list):
        raise AppClientError("Brand Analytics returned an invalid runs list.")
    runs = response["runs"]
    summaries = []
    for run in runs[:limit]:
        if not isinstance(run, dict):
            raise AppClientError("Brand Analytics returned an invalid run row.")
        summaries.append({
            key: run.get(key)
            for key in (
                "id", "name", "search_type", "search_terms", "status",
                "progress_phase", "progress_done", "progress_total",
                "item_count", "created_at", "updated_at",
            )
        })
    return {"runs": summaries, "count": len(summaries)}


@mcp.tool()
async def brand_run(run_id: int, limit: int = 50, offset: int = 0) -> dict[str, Any]:
    """Read one Brand Analytics run and a bounded page of its ASIN results."""
    run_id = _positive_id(run_id, "run_id")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise AppClientError("limit must be an integer from 1 to 100.")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        raise AppClientError("offset must be a non-negative integer.")
    response = await app_client.get(
        f"/api/brand-analytics/runs/{run_id}",
        params={"limit": limit, "offset": offset},
    )
    if not isinstance(response, dict) or not isinstance(response.get("run"), dict):
        raise AppClientError("Brand Analytics returned an invalid run detail.")
    return response


@mcp.tool()
async def start_brand_search(
    brand: str,
    confirm: bool = False,
    entity_type: Literal["brand", "manufacturer"] = "brand",
    search_terms: list[str] | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Use the existing discovery endpoint, then start a run with search terms the user selected. The app has no pre-run cost-estimate endpoint, so discovery requires explicit confirmation with an unknown variable cost."""
    brand = brand.strip()
    if not brand:
        raise AppClientError("brand is required.")
    if len(brand) > 200:
        raise AppClientError("brand must be 200 characters or fewer.")
    if not isinstance(force_refresh, bool):
        raise AppClientError("force_refresh must be true or false.")
    if search_terms is not None and (
        not isinstance(search_terms, list)
        or not search_terms
        or any(not isinstance(term, str) or not term.strip() for term in search_terms)
    ):
        raise AppClientError("search_terms must be a non-empty list of names.")
    if not confirm:
        return {
            "confirmation_required": True,
            "brand": brand,
            "search_terms": search_terms,
            "message": (
                "No discovery or run was started. The existing app API cannot estimate "
                "Keepa/AI cost before discovery. Set confirm=true only if the user accepts "
                "the unknown variable cost."
            ),
        }
    if search_terms is None:
        discovery = await app_client.post(
            "/api/brand-analytics/discover",
            {"name": brand, "entity_type": entity_type},
            timeout=180,
        )
        return {
            "confirmation_required": True,
            "discovery": discovery,
            "message": (
                "Discovery finished. Review its results and provide the search_terms "
                "the user wants to include, with confirm=true, to start the ASIN run."
            ),
        }

    started = await app_client.post(
        "/api/brand-analytics/runs",
        {
            "name": brand,
            "search_type": entity_type,
            "search_terms": search_terms,
            "force_refresh": force_refresh,
        },
        timeout=180,
    )
    return {"confirmation_required": False, "started": started}
