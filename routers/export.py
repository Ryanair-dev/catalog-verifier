"""
Excel export endpoint.

Accepts the final (post-review) results list from the client and returns a
styled .xlsx with colour-coded rows and a second sheet containing the
current abbreviation library.
"""
from __future__ import annotations

import io
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from pydantic import BaseModel

router = APIRouter()


OUTPUT_COLUMNS = [
    "UPC/EAN", "Item ID", "Vendor Title", "Brand", "ASIN", "Amazon Title",
    "Confidence Score", "Verdict", "Review Status",
    "UPC Signal", "Item ID Signal", "Brand Signal", "Title Signal",
    "Pack Signal", "Amz Pack", "Barcode DB Match", "Duplicate Flag",
    "Original Verdict", "Notes", "Reason",
]

# Fill colours. Tuned for readability on a white sheet.
FILL_VERIFIED = PatternFill("solid", fgColor="D9F2E3")   # soft green
FILL_REVIEW = PatternFill("solid", fgColor="FFF4CC")     # soft yellow
FILL_NOT_VERIFIED = PatternFill("solid", fgColor="FBDADA")  # soft red
FILL_OVERRIDDEN = PatternFill("solid", fgColor="FFE0B8")    # light orange
FILL_DUPLICATE = PatternFill("solid", fgColor="FFF2A8")     # duplicate yellow
FILL_HEADER = PatternFill("solid", fgColor="0E2A47")        # dark navy


class ExportRow(BaseModel):
    data: dict[str, Any]


class ExportPayload(BaseModel):
    results: list[dict[str, Any]]
    abbreviations: list[dict[str, Any]] = []


def _build_reason(row: dict) -> str:
    signals = row.get("signals") or {}
    upc_sig = signals.get("upc") or {}
    title_sig = signals.get("title") or {}
    brand_sig = signals.get("brand") or {}
    parts: list[str] = []

    if not row.get("ASIN"):
        return "No ASIN in catalog"
    if upc_sig.get("detail") == "No Amazon row found":
        return "ASIN not in Amazon file"
    if upc_sig.get("no_data"):
        parts.append("No UPC in Amazon export — scored on title")
    elif not upc_sig.get("matched"):
        parts.append("UPC mismatch")
    title_score = title_sig.get("score") or 0
    if title_score < 60:
        parts.append(f"Low title similarity ({round(title_score)}%)")
    if not brand_sig.get("matched"):
        parts.append("Brand mismatch")
    if row.get("duplicate"):
        parts.append("Duplicate Item ID")
    notes = row.get("notes") or ""
    if notes:
        parts.append(notes)
    if not parts:
        verdict = row.get("verdict") or ""
        return "" if verdict == "Approved" else "Low confidence score"
    return " · ".join(parts)


def _signal_text(signal: dict | None) -> str:
    if not signal:
        return ""
    score = signal.get("score", "")
    detail = signal.get("detail", "")
    return f"{score}% — {detail}" if detail else f"{score}%"


def _row_fill(row: dict) -> PatternFill | None:
    """Override colour wins over verdict colour.

    A row is 'overridden' whenever the final verdict differs from the engine's
    original verdict, or when a review-status tag marks a manual action.
    """
    review_status = row.get("review_status") or ""
    is_overridden = bool(review_status) and review_status in (
        "Reviewed", "Manually Approved", "Manually Rejected",
    )
    if is_overridden and review_status in ("Manually Approved", "Manually Rejected"):
        return FILL_OVERRIDDEN
    verdict = row.get("verdict")
    if verdict == "Approved":
        return FILL_VERIFIED
    if verdict == "Review":
        return FILL_REVIEW
    if verdict in ("Not Approved", "Not Verified"):
        return FILL_NOT_VERIFIED
    return None


@router.post("/export")
async def export(payload: ExportPayload) -> StreamingResponse:
    wb = Workbook()
    ws = wb.active
    ws.title = "Verification Results"

    # Header row.
    header_font = Font(name="Inter", bold=True, color="FFFFFF", size=11)
    for col_idx, name in enumerate(OUTPUT_COLUMNS, start=1):
        cell = ws.cell(row=1, column=col_idx, value=name)
        cell.fill = FILL_HEADER
        cell.font = header_font
        cell.alignment = Alignment(horizontal="left", vertical="center")

    ws.row_dimensions[1].height = 26
    ws.freeze_panes = "A2"

    # Column widths (20 columns: added Amazon Title at col 6, Reason at col 20).
    widths = [16, 14, 48, 18, 14, 48, 14, 14, 18, 28, 28, 28, 36, 28, 10, 36, 14, 16, 36, 52]
    for idx, width in enumerate(widths, start=1):
        ws.column_dimensions[ws.cell(row=1, column=idx).column_letter].width = width

    # Data rows.
    body_font = Font(name="Inter", size=10)
    for r_idx, row in enumerate(payload.results, start=2):
        signals = row.get("signals", {}) or {}
        barcode = row.get("barcode_db") or {}
        barcode_text = (
            f"{barcode.get('source', '')}: {barcode.get('name', '')}"
            if barcode.get("found") else (barcode.get("reason", "Not Found") if barcode else "")
        )

        values = [
            row.get("UPC/EAN"),
            row.get("Item ID"),
            row.get("Vendor Title"),
            row.get("Brand"),
            row.get("ASIN"),
            row.get("AmzTitle") or "",
            row.get("confidence"),
            row.get("verdict"),
            row.get("review_status") or "",
            _signal_text(signals.get("upc")),
            _signal_text(signals.get("item_id")),
            _signal_text(signals.get("brand")),
            _signal_text(signals.get("title")),
            _signal_text(signals.get("pack")),
            row.get("amz_pack"),
            barcode_text,
            "Duplicate" if row.get("duplicate") else "",
            row.get("original_verdict"),
            row.get("notes") or "",
            _build_reason(row),
        ]

        fill = _row_fill(row)
        for c_idx, val in enumerate(values, start=1):
            cell = ws.cell(row=r_idx, column=c_idx, value=val)
            cell.font = body_font
            cell.alignment = Alignment(vertical="center", wrap_text=True)
            # Duplicate flag cell highlighted in yellow regardless of verdict fill.
            # (Column 17 = "Duplicate Flag" in the 20-column layout.)
            if c_idx == 17 and row.get("duplicate"):
                cell.fill = FILL_DUPLICATE
            elif fill is not None:
                cell.fill = fill

    # Second sheet — abbreviation library.
    ws2 = wb.create_sheet(title="Abbreviation Library")
    ws2.cell(row=1, column=1, value="Abbreviation").font = header_font
    ws2.cell(row=1, column=2, value="Full Form").font = header_font
    ws2.cell(row=1, column=1).fill = FILL_HEADER
    ws2.cell(row=1, column=2).fill = FILL_HEADER
    ws2.column_dimensions["A"].width = 22
    ws2.column_dimensions["B"].width = 40
    ws2.freeze_panes = "A2"
    for idx, entry in enumerate(payload.abbreviations, start=2):
        ws2.cell(row=idx, column=1, value=entry.get("abbr"))
        ws2.cell(row=idx, column=2, value=entry.get("full"))

    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    headers = {
        "Content-Disposition": "attachment; filename=catalog_verification_results.xlsx"
    }
    return StreamingResponse(
        buffer,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers=headers,
    )
