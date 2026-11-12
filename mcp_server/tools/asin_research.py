from __future__ import annotations

import asyncio
import logging
import sys
import time
from typing import Any, Literal

from mcp_server.app import mcp
from mcp_server.mcp_client import AppClientError, app_client
from mcp_server.format_results import format_quick_search_result
from mcp_server.history_db import search_saved_matches

logger = logging.getLogger("catalog_verifier.mcp")
logging.basicConfig(level=logging.INFO, stream=sys.stderr)

@mcp.tool()
async def find_asins(
    upc: str | None = None,
    item_id: str | None = None,
    title: str | None = None,
    brand: str | None = None,
    scoring_method: Literal["weighted", "cascade", "classifier"] = "weighted",
    vetting_mode: Literal["cpg", "medical"] = "cpg",
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


@mcp.tool()
async def lookup_product(
    upc: str | None = None,
    asin: str | None = None,
    item_id: str | None = None,
) -> dict[str, Any]:
    """Search the Pair Library, rejected-pair list, and saved Offer Analysis runs by UPC, ASIN, or item ID."""
    started = time.perf_counter()
    ok = False
    try:
        provided = [
            ("upc", upc),
            ("asin", asin),
            ("item_id", item_id),
        ]
        supplied = [(kind, value.strip()) for kind, value in provided if value and value.strip()]
        if len(supplied) != 1:
            raise AppClientError("Provide exactly one of upc, asin, or item_id.")
        identifier_type, query = supplied[0]
        pair_library, blacklist = await asyncio.gather(
            app_client.post("/api/pairs/library/search", {"query": query}),
            app_client.post("/api/pairs/search", {"query": query}),
        )
        try:
            saved_matches = await asyncio.to_thread(
                search_saved_matches, query, identifier_type,
            )
        except (FileNotFoundError, ValueError, RuntimeError) as exc:
            raise AppClientError(str(exc)) from exc
        pair_rows = pair_library.get("results")
        blacklist_rows = blacklist.get("results")
        if not isinstance(pair_rows, list) or not isinstance(blacklist_rows, list):
            raise AppClientError("Pair search returned an invalid response.")
        if any(not isinstance(row, dict) for row in pair_rows + blacklist_rows):
            raise AppClientError("Pair search returned an invalid result row.")
        ok = True
        return {
            "identifier": query,
            "identifier_type": identifier_type,
            "pair_library": pair_rows,
            "blacklist": blacklist_rows,
            "saved_offer_analysis_matches": saved_matches,
        }
    finally:
        logger.info(
            "tool=lookup_product duration_ms=%d ok=%s identifier_type=%s",
            round((time.perf_counter() - started) * 1000),
            ok,
            "asin" if asin else "item_id" if item_id else "upc",
        )


@mcp.tool()
async def mpn_lookup(
    mpn: str,
    manufacturer: str,
    use_ai_fallback: bool = False,
    confirm: bool = False,
) -> dict[str, Any]:
    """Use when someone needs Qty/Case or EA/Case for a manufacturer part number. Looks up the existing local mirror and FDA AccessGUDID data first. Optional Claude web search may incur variable API charges; set use_ai_fallback=true to preview it, then confirm=true only after the user agrees."""
    started = time.perf_counter()
    ok = False
    mpn = mpn.strip()
    manufacturer = manufacturer.strip()
    try:
        if not mpn or not manufacturer:
            raise AppClientError("Both mpn and manufacturer are required.")

        payload = await app_client.post(
            "/api/mpn-check",
            {"pairs": [{"mpn": mpn, "manufacturer": manufacturer}], "fields": ["qty_case"]},
        )
        results = payload.get("results")
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            raise AppClientError("MPN Check returned an unexpected response.")
        result = {
            key: results[0].get(key)
            for key in (
                "mpn", "manufacturer", "matched", "product_id", "qty_case",
                "qty_case_uom", "ea_case", "note",
            )
            if key in results[0]
        }
        unresolved = result.get("ea_case") is None

        if use_ai_fallback and unresolved and not confirm:
            result["ai_fallback_preview"] = {
                "will_run": False,
                "reason": "The standard lookup did not return an EA/Case value.",
                "estimated_cost": (
                    "Not available from the app. Claude web-search and token charges vary; "
                    "no AI search was made."
                ),
                "next_step": "Ask the user before calling again with confirm=true.",
            }
        elif use_ai_fallback and unresolved and confirm:
            fallback_payload = await app_client.post(
                "/api/mpn-check/ai-fallback",
                {"pairs": [{"mpn": mpn, "manufacturer": manufacturer}]},
                timeout=90,
            )
            fallback_results = fallback_payload.get("results")
            if not isinstance(fallback_results, list):
                raise AppClientError("MPN AI fallback returned an unexpected response.")
            if fallback_results:
                fallback = fallback_results[0]
                if not isinstance(fallback, dict):
                    raise AppClientError("MPN AI fallback returned an invalid result row.")
                result["ai_ea_case"] = fallback.get("ea_case")
                result["ai_confidence"] = fallback.get("confidence")
                result["ai_note"] = fallback.get("note")
            result["ai_fallback_confirmed"] = True
        elif use_ai_fallback:
            result["ai_fallback_preview"] = {
                "will_run": False,
                "reason": "The standard lookup already returned an EA/Case value.",
            }

        ok = True
        return {
            "summary": (
                f"MPN {mpn} for {manufacturer}: "
                f"{'found' if result.get('matched') else 'not found'} in FDA AccessGUDID."
            ),
            "items": [result],
        }
    finally:
        logger.info(
            "tool=mpn_lookup duration_ms=%d ok=%s ai_fallback=%s confirmed=%s",
            round((time.perf_counter() - started) * 1000),
            ok,
            use_ai_fallback,
            confirm,
        )
