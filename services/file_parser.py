"""
Shared file parsing helpers used by scans, verify, and analytics routers.

Single source of truth for Excel / CSV ingestion so a bug fix or encoding
tweak propagates everywhere automatically.
"""
from __future__ import annotations

import csv
import io
from typing import Any

from openpyxl import load_workbook


def parse_file(filename: str, data: bytes, sheet_name: str = "") -> tuple[list[str], list[list[Any]]]:
    """Return (headers, rows) from an Excel or CSV/TSV file.

    Headers come from the first row. Empty trailing rows are dropped.
    Pass ``sheet_name`` to read a specific Excel sheet; omit to use the active sheet.
    """
    name = (filename or "").lower()
    if name.endswith(".csv") or name.endswith(".tsv"):
        return _parse_csv(data, dialect="excel-tab" if name.endswith(".tsv") else "excel")
    return _parse_workbook(data, sheet_name=sheet_name)


def get_sheet_names(filename: str, data: bytes) -> list[str]:
    """Return the list of sheet names for an Excel workbook.

    Returns an empty list for CSV/TSV files (they have no concept of sheets).
    """
    name = (filename or "").lower()
    if name.endswith(".csv") or name.endswith(".tsv"):
        return []
    try:
        wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
        names = list(wb.sheetnames)
        wb.close()
        return names
    except Exception:
        return []


def parse_raw_rows(filename: str, data: bytes, sheet_name: str = "") -> list[list[Any]]:
    """Return every row as a raw list with NO header extraction.

    Used by the analytics wizard preview where the caller chooses which row
    is the header.  Pass ``sheet_name`` to read a specific Excel sheet;
    omit (or pass empty string) to use the active sheet.
    """
    name = (filename or "").lower()
    if name.endswith(".csv") or name.endswith(".tsv"):
        text = data.decode("utf-8-sig", errors="replace")
        dialect = "excel-tab" if name.endswith(".tsv") else "excel"
        reader = csv.reader(io.StringIO(text), dialect=dialect)
        return [list(r) for r in reader]
    wb = load_workbook(io.BytesIO(data), data_only=True)
    if sheet_name and sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
    else:
        ws = wb.active
    return [list(r) for r in ws.iter_rows(values_only=True)]


def _parse_workbook(data: bytes, sheet_name: str = "") -> tuple[list[str], list[list[Any]]]:
    wb = load_workbook(io.BytesIO(data), data_only=True)
    if sheet_name and sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
    else:
        ws = wb.active
    rows_iter = ws.iter_rows(values_only=True)
    try:
        first = next(rows_iter)
    except StopIteration:
        return [], []
    headers = [
        str(h).strip() if h is not None else f"Column {i + 1}"
        for i, h in enumerate(first)
    ]
    body: list[list[Any]] = []
    for row in rows_iter:
        if row is None or all(v in (None, "") for v in row):
            continue
        body.append(list(row))
    return headers, body


def _parse_csv(data: bytes, dialect: str = "excel") -> tuple[list[str], list[list[Any]]]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.reader(io.StringIO(text), dialect=dialect)
    try:
        headers = [h.strip() for h in next(reader)]
    except StopIteration:
        return [], []
    body = [list(r) for r in reader if any((c or "").strip() for c in r)]
    return headers, body
