from __future__ import annotations

import logging
import sys
import time
from typing import Any

from mcp_server.app import mcp
from mcp_server.client import AppClientError, app_client
from mcp_server.format_results import format_quick_search_result

logger = logging.getLogger("catalog_verifier.mcp")
logging.basicConfig(level=logging.INFO, stream=sys.stderr)


@mcp.tool()
async def find_asins(
    upc: str | None = None,
    item_id: str | None = None,
    title: str | None = None,
    brand: str | None = None,
    scoring_method: str = "weighted",
    vetting_mode: str = "cpg",
) -> dict[str, Any]:
    """Use when someone has a UPC, item ID/MPN, or product description and wants matching Amazon listings with confidence and verdicts."""
    started = time.perf_counter()
    ok = False
    fields = [
        name
        for name, value in (("upc", upc), ("item_id", item_id), ("title", title), ("brand", brand))
        if value and value.strip()
    ]
    try:
        if not any((upc and upc.strip(), item_id and item_id.strip(), title and title.strip())):
            raise AppClientError("Provide at least one of UPC, item_id, or title.")

        payload = await app_client.post(
            "/api/analytics/quick-search",
            {
                "upc": upc or "",
                "itemid": item_id or "",
                "title": title or "",
                "brand": brand or "",
                "scoring_method": scoring_method,
                "vetting_mode": vetting_mode,
            },
        )
        searched_for = next(
            (
                f"{label} {value.strip()}"
                for label, value in (("UPC", upc), ("item ID", item_id), ("title", title))
                if value and value.strip()
            ),
            "the supplied product",
        )
        result = format_quick_search_result(payload, searched_for)
        ok = True
        return result
    finally:
        duration_ms = round((time.perf_counter() - started) * 1000)
        logger.info(
            "tool=find_asins duration_ms=%d ok=%s input_fields=%s",
            duration_ms,
            ok,
            ",".join(fields) or "none",
        )
