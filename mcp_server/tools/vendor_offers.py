from __future__ import annotations

import asyncio
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any
from zipfile import BadZipFile

from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from mcp_server.app import mcp
from mcp_server.mcp_client import AppClientError, app_client


@mcp.tool()
async def cheapest_offer(upc: str | None = None, asin: str | None = None) -> dict[str, Any]:
    """Return the Price Desk's cheapest-offer row for one UPC or ASIN using its existing lookup export."""
    upc_value = (upc or "").strip()
    asin_value = (asin or "").strip()
    if bool(upc_value) == bool(asin_value):
        raise AppClientError("Provide exactly one of upc or asin.")
    identifier = asin_value or upc_value
    content = await app_client.post_download(
        "/api/vendor-offers/lookup-export",
        {"identifiers": [identifier]},
        timeout=90,
    )
    try:
        workbook = load_workbook(BytesIO(content), read_only=True, data_only=True)
        try:
            sheet = workbook["Price Check"]
            rows = sheet.iter_rows(values_only=True)
            headers = next(rows, None)
            result = next(rows, None)
            if (
                not headers
                or not result
                or any(not isinstance(header, str) or not header for header in headers)
                or len(headers) != len(result)
            ):
                raise AppClientError("Price Desk returned an invalid lookup workbook.")
            return {"price_check": dict(zip(headers, result))}
        finally:
            workbook.close()
    except (BadZipFile, InvalidFileException, OSError, ValueError, KeyError) as exc:
        raise AppClientError("Price Desk returned an invalid lookup workbook.") from exc


@mcp.tool()
async def export_offers(
    upcs: list[str] | None = None,
    focus_vendor: str = "",
    confirm: bool = False,
    output_dir: str = "",
) -> dict[str, Any]:
    """Export the Price Desk comparison workbook to the local computer. Omit upcs for the full book; confirm=true is required before export."""
    clean_upcs: list[str] | None = None
    if upcs is not None:
        clean_upcs = list(dict.fromkeys(value.strip() for value in upcs if value.strip()))
        if not clean_upcs:
            raise AppClientError("upcs was provided but contains no non-empty UPCs.")
        if len(clean_upcs) > 5000:
            raise AppClientError("Export at most 5,000 UPCs per request.")
        if any(len(value) > 32 for value in clean_upcs):
            raise AppClientError("Each UPC must be 32 characters or fewer.")
    if len(focus_vendor) > 200:
        raise AppClientError("focus_vendor must be 200 characters or fewer.")
    directory = (
        Path(output_dir).expanduser()
        if output_dir.strip()
        else Path.home() / "Downloads" / "Catalog Verifier" / "Price Desk"
    )
    if not directory.is_absolute():
        raise AppClientError("output_dir must be an absolute path.")
    filename = f"price_desk_{uuid.uuid4().hex[:8]}.xlsx"
    output_file = (directory / filename).resolve()
    if not confirm:
        return {
            "confirmation_required": True,
            "scope": f"{len(clean_upcs)} UPCs" if clean_upcs else "the full Price Desk",
            "focus_vendor": focus_vendor.strip() or None,
            "output_file": str(output_file),
            "message": "No export was generated. Confirm to build and save the workbook.",
        }

    try:
        directory.mkdir(parents=True, exist_ok=True)
        content = await app_client.post_download(
            "/api/vendor-offers/export",
            {"upcs": clean_upcs, "focus_vendor": focus_vendor.strip()},
            timeout=300,
        )
        if not content:
            raise AppClientError("Price Desk returned an empty export.")
        if not content.startswith(b"PK"):
            raise AppClientError("Price Desk returned an invalid Excel workbook.")
        await asyncio.to_thread(output_file.write_bytes, content)
    except OSError as exc:
        raise AppClientError(f"Could not save the export file to {directory}.") from exc
    return {
        "confirmation_required": False,
        "file_path": str(output_file),
        "upc_count": len(clean_upcs) if clean_upcs else None,
        "focus_vendor": focus_vendor.strip() or None,
    }
