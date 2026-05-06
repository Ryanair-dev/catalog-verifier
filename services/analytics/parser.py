"""
Vendor-file -> source-row parser for the Analytics pipeline.

The Analytics wizard lets the user pick which row holds the headers and
which columns map to UPC / Item ID / Title. This module consumes that
decision and yields a clean list of source rows ready for SP-API search
and confidence scoring.

Supported formats:
  * .xlsx / .xls / .xlsm  (openpyxl)
  * .csv / .tsv           (stdlib csv)

The parser deliberately does *not* try to auto-detect the header row --
that's the wizard's job, and is already in state.awiz.headerRowIdx by
the time we get the upload.
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from typing import Any

from openpyxl import load_workbook


# --------------------------------------------------------------------------- #
# Public shape
# --------------------------------------------------------------------------- #


@dataclass
class SourceRow:
    """One catalog line after the wizard's column mapping has been applied."""

    row_idx: int                     # 0-based index within the data rows
    upc: str = ""                    # raw UPC/EAN/GTIN value
    itemid: str = ""                 # raw Item ID / MPN / SKU
    title: str = ""                  # full vendor title — used for scoring
    search_title: str = ""           # simplified title for SP-API keyword search
                                     # (falls back to title if not mapped)
    brand: str = ""                  # always the wizard-supplied brand
    raw: dict[str, Any] = field(default_factory=dict)  # original header->cell map

    @property
    def search_term(self) -> str:
        """The term to send to Amazon keyword search — search_title if set, else title."""
        return self.search_title or self.title

    def as_dict(self) -> dict:
        """Serialisable form stored in analytics_catalog_rows.data_json."""
        return {
            "row_idx": self.row_idx,
            "upc": self.upc,
            "itemid": self.itemid,
            "title": self.title,
            "search_title": self.search_title,
            "brand": self.brand,
            "raw": self.raw,
        }

    # Shape expected by matcher.calculate_confidence — always uses full title
    def as_source_dict(self) -> dict:
        return {
            "upc": self.upc,
            "mpn": self.itemid,
            "itemid": self.itemid,
            "title": self.title,
            "brand": self.brand,
            "manufacturer": self.brand,
        }


# --------------------------------------------------------------------------- #
# Raw rows (xlsx / csv)
# --------------------------------------------------------------------------- #


def _raw_rows(filename: str, data: bytes) -> list[list[Any]]:
    """Pull rows-as-arrays. No header assumption."""
    name = (filename or "").lower()
    if name.endswith(".csv") or name.endswith(".tsv"):
        text = data.decode("utf-8-sig", errors="replace")
        dialect = "excel-tab" if name.endswith(".tsv") else "excel"
        reader = csv.reader(io.StringIO(text), dialect=dialect)
        return [list(r) for r in reader]

    # Default: Excel via openpyxl (handles .xlsx / .xlsm / newer .xls
    # technically not but the wizard's preview endpoint also uses openpyxl).
    wb = load_workbook(io.BytesIO(data), data_only=True)
    ws = wb.active
    return [list(r) for r in ws.iter_rows(values_only=True)]


# --------------------------------------------------------------------------- #
# Cell cleanup
# --------------------------------------------------------------------------- #


def _cell_str(value: Any) -> str:
    """
    Safe text conversion for a cell value.

    * None -> ""
    * floats that are whole numbers -> integer string (avoids "1.0" for UPCs)
    * everything else -> str(value).strip()
    """
    if value is None:
        return ""
    if isinstance(value, float):
        if value != value:  # NaN
            return ""
        if value.is_integer():
            return str(int(value))
    return str(value).strip()


def _col_int(mapping: dict, key: str) -> int | None:
    """Pull a column index out of the wizard's mapping dict. Empty -> None."""
    raw = mapping.get(key)
    if raw in (None, "", "null"):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Main entrypoint
# --------------------------------------------------------------------------- #


def parse_source_rows(
    filename: str,
    data: bytes,
    header_row_idx: int,
    mapping: dict,
    brand: str,
) -> list[SourceRow]:
    """
    Parse a vendor file into SourceRow objects.

    Args:
      filename:        original upload filename (drives extension sniffing).
      data:            raw bytes of the file.
      header_row_idx:  0-based index of the row the user clicked in step 2.
      mapping:         {"upc": "3", "itemid": "5", "title": "4"} — wizard
                       step 3 output. Values are 0-based column indices
                       as strings.
      brand:           free-text brand typed in step 3; applied to every row.
                       Overridden per-row by mapping["brand"] column when set.

    Returns:
      A list of SourceRow dicts, skipping any row where UPC, Item ID, and
      Title are all blank (those are almost always spacer rows).
    """
    all_rows = _raw_rows(filename, data)
    if not all_rows:
        return []

    if header_row_idx < 0 or header_row_idx >= len(all_rows):
        header_row_idx = 0

    header_row = all_rows[header_row_idx]
    headers = [_cell_str(c) or f"col_{i}" for i, c in enumerate(header_row)]
    data_rows = all_rows[header_row_idx + 1:]

    upc_col          = _col_int(mapping, "upc")
    itemid_col       = _col_int(mapping, "itemid")
    title_col        = _col_int(mapping, "title")
    search_title_col = _col_int(mapping, "search_title")
    brand_col        = _col_int(mapping, "brand")

    brand_clean = (brand or "").strip()

    parsed: list[SourceRow] = []
    for i, row in enumerate(data_rows):
        # Pad short rows so the col lookups don't IndexError.
        cells = list(row) + [None] * max(0, len(headers) - len(row))

        upc_raw      = _cell_str(cells[upc_col])          if upc_col          is not None and upc_col          < len(cells) else ""
        # Zero-pad 11-digit UPCs → 12-digit UPC-A (vendor exports often strip leading zero)
        upc          = ("0" + upc_raw) if (len(upc_raw) == 11 and upc_raw.isdigit()) else upc_raw
        itemid       = _cell_str(cells[itemid_col])       if itemid_col       is not None and itemid_col       < len(cells) else ""
        title        = _cell_str(cells[title_col])        if title_col        is not None and title_col        < len(cells) else ""
        search_title = _cell_str(cells[search_title_col]) if search_title_col is not None and search_title_col < len(cells) else ""

        if not upc and not itemid and not title:
            continue  # spacer row — skip

        # Preserve the full raw row keyed by header so the UI can show
        # "View original row" later without re-parsing the file.
        raw = {headers[j]: _cell_str(cells[j]) for j in range(min(len(headers), len(cells)))}

        row_brand = (
            _cell_str(cells[brand_col])
            if brand_col is not None and brand_col < len(cells)
            else None
        ) or brand_clean

        parsed.append(SourceRow(
            row_idx=i,
            upc=upc,
            itemid=itemid,
            title=title,
            search_title=search_title,
            brand=row_brand,
            raw=raw,
        ))

    return parsed
