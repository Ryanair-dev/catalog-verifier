from __future__ import annotations

import asyncio
from typing import Any

from mcp_server.app import mcp
from mcp_server.mcp_client import AppClientError, app_client

async def _library_search(query: str) -> list[dict[str, Any]]:
    response = await app_client.post("/api/pairs/library/search", {"query": query})
    rows = response.get("results")
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise AppClientError("Pair Library returned an invalid search result.")
    return rows


@mcp.tool()
async def search_pairs(query: str) -> dict[str, Any]:
    """Search the Pair Library and rejected-pair blacklist for an identifier, ASIN, or brand."""
    query = query.strip()
    if not query:
        raise AppClientError("Provide a search query.")
    if len(query) > 200:
        raise AppClientError("query must be 200 characters or fewer.")
    library, blacklist = await asyncio.gather(
        _library_search(query),
        app_client.post("/api/pairs/search", {"query": query}),
    )
    rejected = blacklist.get("results")
    if not isinstance(rejected, list) or any(not isinstance(row, dict) for row in rejected):
        raise AppClientError("Blacklist returned an invalid search result.")
    return {
        "pair_library": library[:50],
        "pair_library_total": len(library),
        "blacklist": rejected[:50],
        "blacklist_total": len(rejected),
    }
