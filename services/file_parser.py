"""
Shared file parsing helpers used by scans, verify, and analytics routers.
"""
from __future__ import annotations

import csv
import io
from typing import Any

from openpyxl import load_workbook

_CSV_DELIMITERS = ",;\t|"


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
        rows = _csv_rows(data, tab=name.endswith(".tsv"))
    else:
        rows = _sheet_rows(data, sheet_name)
    return _trim_trailing_blank(rows)


# --------------------------------------------------------------------------- #
# CSV helpers
# --------------------------------------------------------------------------- #

def _decode_text(data: bytes) -> str:
    """UTF-16 when a BOM says so, else UTF-8 (BOM ok), else Windows-1252, which is
    what Excel on Windows writes for 'CSV (Comma delimited)' with accented text."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return data.decode("utf-16")
        except UnicodeDecodeError:
            pass
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _guess_delimiter(text: str) -> str:
    """Pick the delimiter that splits the first lines into the most, and most
    consistent, columns. Falls back to a comma (also right for one-column files)."""
    lines = [ln for ln in text[:20000].splitlines() if ln.strip()][:20]
    best, best_score = ",", 0.0
    for d in _CSV_DELIMITERS:
        counts = [len(r) for r in csv.reader(lines, delimiter=d)]
        if not counts:
            continue
        width = max(set(counts), key=counts.count)           # most common column count
        agree = counts.count(width) / len(counts)
        score = agree * width if width > 1 else 0.0
        if score > best_score:
            best, best_score = d, score
    return best


def _csv_rows(data: bytes, tab: bool) -> list[list[str]]:
    text = _decode_text(data)
    delim = "\t" if tab else _guess_delimiter(text)
    return [list(r) for r in csv.reader(io.StringIO(text), dialect="excel", delimiter=delim)]


def _parse_csv(data: bytes, dialect: str = "excel") -> tuple[list[str], list[list[Any]]]:
    rows = _csv_rows(data, tab=(dialect == "excel-tab"))
    if not rows:
        return [], []
    headers = [h.strip() for h in rows[0]]
    body = [r for r in rows[1:] if any((c or "").strip() for c in r)]
    return headers, body


# --------------------------------------------------------------------------- #
# Excel helpers
# --------------------------------------------------------------------------- #

def _sheet_rows(data: bytes, sheet_name: str = "") -> list[list[Any]]:
    """Stream every row of one sheet. read_only mode keeps memory flat on big files;
    reset_dimensions() matters because some generators write a wrong <dimension> tag
    and read-only mode would otherwise stop after the first row or column."""
    wb = load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    try:
        ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active
        ws.reset_dimensions()
        return [list(r) for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()


def _trim_trailing_blank(rows: list[list[Any]]) -> list[list[Any]]:
    """Drop all-blank rows at the end. Workbooks with formatting applied to whole
    columns can report a million 'rows' that hold nothing."""
    while rows and all(c in (None, "") for c in rows[-1]):
        rows.pop()
    return rows


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