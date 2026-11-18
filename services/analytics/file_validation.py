"""
Pre-flight checks for vendor catalog files.
"""
from __future__ import annotations

import csv
import os
import re
import zipfile
from dataclasses import dataclass, field, replace
from typing import Any

# Hard cap on data rows per run. Override with the MAX_CATALOG_ROWS env var.
MAX_CATALOG_ROWS = int(os.getenv("MAX_CATALOG_ROWS", "200000"))
MAX_TITLE_CHARS = 1000        # a longer "title" is almost always a mis-mapped column
EXAMPLES_PER_ISSUE = 5

# Matches services/file_parser.py: .csv/.tsv are read as text, everything else goes to
# openpyxl, which reads .xlsx and .xlsm. Legacy .xls is not supported.
SUPPORTED_EXTENSIONS = (".xlsx", ".xlsm", ".csv", ".tsv")

_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # old .xls AND password-protected .xlsx
_ZIP_MAGIC = b"PK"
_EMPTY_TOKENS = {
    "", "n/a", "na", "n.a.", "none", "null", "nan", "tbd", "unknown", "-", "--", "#n/a",
}
_SCIENTIFIC = re.compile(r"^\d+(?:\.\d+)?[eE][+-]?\d+$")
_TRAILING_ZERO = re.compile(r"^(\d+)\.0+$")
_FORMULA_ERR = re.compile(r"^#(?:N/A|REF!|VALUE!|NAME\?|DIV/0!|NULL!|NUM!)$", re.I)


class CatalogRejected(ValueError):
    """The file can't be processed. The message is written for whoever uploaded it."""


# --------------------------------------------------------------------------- #
# File level
# --------------------------------------------------------------------------- #

def sniff_file(filename: str, data: bytes) -> None:
    """Cheap checks on the raw bytes, before any parser touches the file."""
    ext = os.path.splitext((filename or "").strip().lower())[1]
    if not data:
        raise CatalogRejected(
            "The file is empty (0 bytes). Re-export it from the vendor and upload it again."
        )
    if ext and ext not in SUPPORTED_EXTENSIONS:
        hint = (" Open it in Excel and save it as .xlsx."
                if ext in (".xls", ".xlsb", ".ods") else "")
        raise CatalogRejected(
            f"{ext} files are not supported. Upload .xlsx, .csv or .tsv.{hint}"
        )

    head = data[:512].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
    if head.startswith((b"<html", b"<!doctype html", b"<table", b"<?xml")):
        raise CatalogRejected(
            "This looks like an HTML/XML export saved with a spreadsheet extension, not a "
            "real spreadsheet. Open it in Excel, save it as .xlsx, and upload that."
        )
    if ext in (".xlsx", ".xlsm"):
        if data.startswith(_OLE2_MAGIC):
            raise CatalogRejected(
                "This .xlsx is password-protected, or it is an old .xls file that was renamed. "
                "Remove the password or re-save it as a normal .xlsx and try again."
            )
        if not data.startswith(_ZIP_MAGIC):
            raise CatalogRejected(
                "This file has a .xlsx extension but is not a valid Excel workbook (it may be "
                "corrupted or cut off). Download it from the vendor again."
            )
    if ext in (".csv", ".tsv") and (data.startswith(_ZIP_MAGIC) or data.startswith(_OLE2_MAGIC)):
        raise CatalogRejected(
            f"This file has a {ext} extension but is actually an Excel workbook. "
            "Rename it to .xlsx and upload it again."
        )


def describe_parse_error(exc: BaseException, filename: str = "") -> str:
    """Turn a parser exception into something a person can act on."""
    if isinstance(exc, CatalogRejected):
        return str(exc)
    name = type(exc).__name__
    if isinstance(exc, zipfile.BadZipFile) or name in {"InvalidFileException", "BadZipFile"}:
        return ("The workbook could not be opened. It may be corrupted, password-protected, "
                "or not a real .xlsx. Open it in Excel and save a fresh copy as .xlsx.")
    if isinstance(exc, UnicodeDecodeError):
        return ("The text encoding of this file could not be read. Open it in Excel and save "
                "it as .xlsx, or save the CSV as UTF-8.")
    if isinstance(exc, MemoryError):
        return (f"This file is too large to read in one go. Split it into parts of up to "
                f"{MAX_CATALOG_ROWS:,} rows.")
    if name == "EmptyDataError":
        return "The file has no data rows."
    if isinstance(exc, csv.Error) or name == "ParserError":
        return ("The file could not be read as a table. Check that it is a normal CSV/TSV, or "
                "save it as .xlsx.")
    if isinstance(exc, (ValueError, KeyError, IndexError)):
        return f"Could not read this file: {str(exc)[:200]}"
    return f"Could not read this file ({name}). Re-save it as .xlsx or .csv and try again."


def check_row_limit(n_rows: int, limit: int = MAX_CATALOG_ROWS, slack: int = 0) -> None:
    """Reject files over the limit. `slack` lets the early /preview check ignore the
    junk rows above the header; the authoritative check runs on parsed data rows."""
    if n_rows > limit + slack:
        raise CatalogRejected(
            f"This file has {n_rows:,} rows; the limit is {limit:,} per run. "
            f"Split it into parts of up to {limit:,} rows and run them separately."
        )


# --------------------------------------------------------------------------- #
# Barcode cleanup
# --------------------------------------------------------------------------- #

def inspect_barcode(raw: Any) -> tuple[str, str]:
    """-> (kind, cleaned). kind is one of:
         ok          usable; `cleaned` is digits only (11-digit UPCs padded to 12)
         empty       blank, 'N/A', '#N/A' ...; `cleaned` is ''
         scientific  Excel turned it into 1.23E+11: the digits are gone for good
         non_numeric contains letters or symbols
         bad_length  digits, but not 8 / 12 / 13 / 14 long
    Check digits are deliberately NOT validated: vendors often drop the final digit
    of an EAN-13, and the runner already recovers those."""
    s = "" if raw is None else str(raw).strip()
    if s.lower() in _EMPTY_TOKENS or _FORMULA_ERR.match(s):
        return "empty", ""
    if _SCIENTIFIC.match(s):
        return "scientific", ""
    m = _TRAILING_ZERO.match(s)           # '012345678905.0' from a numeric cell
    if m:
        s = m.group(1)
    s = re.sub(r"[\s\-]", "", s)          # '0 12345-67890 5'
    if not s.isdigit():
        return "non_numeric", ""
    if len(s) == 11:
        s = "0" + s                       # same padding the runner applies
    if len(s) in (8, 12, 13, 14):
        return "ok", s
    return "bad_length", ""


# --------------------------------------------------------------------------- #
# Row level
# --------------------------------------------------------------------------- #

@dataclass
class CatalogReport:
    total_rows: int = 0
    searchable_rows: int = 0
    issues: list[dict[str, Any]] = field(default_factory=list)

    def add(self, code: str, severity: str, short: str, message: str, rows: list[int]) -> None:
        if not rows:
            return
        self.issues.append({
            "code": code, "severity": severity, "count": len(rows),
            "short": short, "message": message,
            # row_idx is 0-based; people count rows from 1
            "examples": [r + 1 for r in rows[:EXAMPLES_PER_ISSUE]],
        })

    def summary(self) -> str:
        return "; ".join(f"{i['count']:,} {i['short']}" for i in self.issues)

    def as_dict(self) -> dict[str, Any]:
        return {"total_rows": self.total_rows, "searchable_rows": self.searchable_rows,
                "issues": self.issues}


def validate_catalog(rows: list) -> tuple[list, CatalogReport]:
    """Clean the UPC column, count problems, and reject files that can't work.

    Returns (rows with usable UPCs only, report). Raises CatalogRejected when the
    file is over the row limit or nothing in it can be searched."""
    report = CatalogReport(total_rows=len(rows))
    check_row_limit(len(rows))

    bad_upc: dict[str, list[int]] = {"scientific": [], "non_numeric": [], "bad_length": []}
    no_ident: list[int] = []
    long_title: list[int] = []
    n_upc_ok = n_titles = n_numeric_titles = n_with_id = 0
    id_values: set[str] = set()
    cleaned: list = []

    for r in rows:
        kind, upc = inspect_barcode(getattr(r, "upc", ""))
        if kind in bad_upc:
            bad_upc[kind].append(r.row_idx)
        elif kind == "ok":
            n_upc_ok += 1
        if (getattr(r, "upc", "") or "") != upc:
            r = replace(r, upc=upc)
        cleaned.append(r)

        itemid = (getattr(r, "itemid", "") or "").strip()
        title = (getattr(r, "search_term", None) or getattr(r, "title", "") or "").strip()
        if itemid:
            n_with_id += 1
            id_values.add(itemid.lower())
        if title:
            n_titles += 1
            if title.replace(".", "").replace(",", "").isdigit():
                n_numeric_titles += 1
            if len(title) > MAX_TITLE_CHARS:
                long_title.append(r.row_idx)
        if not (upc or itemid or title):
            no_ident.append(r.row_idx)

    report.searchable_rows = len(rows) - len(no_ident)
    if rows and report.searchable_rows == 0:
        raise CatalogRejected(
            f"None of the {len(rows):,} rows has a UPC, Item ID or title, so there is "
            "nothing to search. Check the column mapping in step 3."
        )

    report.add(
        "upc_scientific", "warning", "UPCs lost to scientific notation",
        "These UPCs were saved as numbers like 1.23E+11, so the digits are lost. Those rows "
        "are searched by Item ID / title only. Fix: format the UPC column as Text in Excel "
        "and export again.", bad_upc["scientific"])
    report.add(
        "upc_not_numeric", "warning", "UPCs with letters or symbols (ignored)",
        "The UPC column holds text that is not a barcode. Those values were ignored; the rows "
        "are searched by Item ID / title only.", bad_upc["non_numeric"])
    report.add(
        "upc_bad_length", "warning", "UPCs with an unusual length (ignored)",
        "Barcodes should be 8, 12, 13 or 14 digits (11 is padded). These were ignored; the "
        "rows are searched by Item ID / title only.", bad_upc["bad_length"])
    report.add(
        "no_identifier", "warning", "rows with no UPC, Item ID or title (will not be searched)",
        "Nothing to search on. They will appear under Not Found.", no_ident)
    report.add(
        "title_too_long", "info", f"titles over {MAX_TITLE_CHARS} characters",
        "That long is usually a description column mapped as the title.", long_title)

    # Mapping sanity: warn when a column doesn't look like what it was mapped as.
    n_upc_bad = sum(len(v) for v in bad_upc.values())
    if n_upc_ok + n_upc_bad >= 20 and n_upc_ok / (n_upc_ok + n_upc_bad) < 0.5:
        report.add("upc_column_suspect", "warning", "UPC column mostly not barcodes",
                   "Fewer than half the UPC values look like barcodes. Is the right column "
                   "mapped?", [0])
    if n_titles >= 20 and n_numeric_titles / n_titles > 0.8:
        report.add("title_column_numeric", "warning", "title column mostly numbers",
                   "Most 'titles' are numbers. Is the right column mapped?", [0])
    if n_with_id >= 50 and len(id_values) / n_with_id < 0.02:
        report.add("itemid_low_variety", "warning", "Item ID column has almost no variety",
                   "Nearly every row has the same Item ID, so one search would stand in for "
                   "the whole file. Is the right column mapped?", [0])

    return cleaned, report