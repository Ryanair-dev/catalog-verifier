"""
Pair Manager endpoints.

Blacklisted UPC/ASIN pairs accumulate over time. The frontend never loads the
full list on open — everything is search-first. ``POST /pairs/unlock`` removes
a single blacklist pair.

Pair Library
------------
Manually confirmed identifier → ASIN mappings, imported from a fixed-template
spreadsheet.  Each identifier (UPC / EAN / MPN) points to exactly ONE ASIN
(newest import wins — overwrite is intentional, imports are ground truth);
an ASIN may carry many identifiers (primary + aliases).  UPC/EAN pairs are
mirrored into verified_items so analytics/verify runs auto-approve them.

Endpoints:
  GET  /pairs/library/stats          — totals + per-brand counts
  GET  /pairs/library/template       — ?kind=import|lookup → xlsx template
  POST /pairs/library/import         — upload fixed-template pair file
  GET  /pairs/library/export         — ?brand= → xlsx of the library (brand or all)
  POST /pairs/library/lookup-export  — upload identifiers → xlsx Matched/Not Found;
                                       complete new pairs in the file are saved
  POST /pairs/library/search         — search library by identifier/ASIN/brand
"""
from __future__ import annotations

import asyncio
import io
import re

from fastapi import APIRouter, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from pydantic import BaseModel

from services import database
from services.file_parser import parse_raw_rows
from services.safety import read_upload_limited, safe_spreadsheet_row

router = APIRouter()


class PairQuery(BaseModel):
    query: str


class PairKey(BaseModel):
    upc: str
    asin: str


@router.post("/pairs/search")
async def search_pairs(body: PairQuery) -> dict:
    """Search blacklisted pairs by UPC or ASIN (partial, case-insensitive)."""
    return {"results": database.search_blacklist(body.query)}


@router.post("/pairs/unlock")
async def unlock_pair(body: PairKey) -> dict:
    removed = database.remove_from_blacklist(body.upc, body.asin)
    return {"unlocked": removed > 0}


# --------------------------------------------------------------------------- #
# Pair Library — helpers
# --------------------------------------------------------------------------- #

_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")

# Fixed-template header → field mapping (case/space-insensitive).
_HEADER_MAP = {
    "asin": "asin",
    "upc": "upc", "upc/ean": "upc",
    "ean": "ean",
    "mpn": "mpn", "mpn/item id": "mpn", "mpn/itemid": "mpn",
    "item id": "mpn", "itemid": "mpn", "item_id": "mpn",
    "brand": "brand", "brand name": "brand",
    "manufacturer": "manufacturer", "mfr": "manufacturer",
    "amz pack": "amz_pack", "amzpack": "amz_pack", "amz_pack": "amz_pack",
    "pack": "amz_pack", "pack qty": "amz_pack", "amazon pack": "amz_pack",
}

_IMPORT_HEADERS = ["ASIN", "UPC", "EAN", "MPN/Item ID", "Brand", "Manufacturer", "amz pack"]
_LOOKUP_HEADERS = ["UPC", "EAN", "MPN/Item ID", "ASIN", "Brand"]


_SCI_NOTATION_RE = re.compile(r"^\d+(\.\d+)?[eE]\+?\d+$")


def _cell(v) -> str:
    """Coerce a raw spreadsheet cell to text. Floats that are whole numbers
    become integer strings so Excel-mangled UPCs ('5.1131e+10') round-trip —
    including when a CSV carries the scientific notation as a TEXT cell."""
    if v is None:
        return ""
    if isinstance(v, float):
        if v != v:  # NaN
            return ""
        if v.is_integer():
            return str(int(v))
    s = str(v).strip()
    if _SCI_NOTATION_RE.match(s):
        try:
            f = float(s)
            if f == int(f):
                return str(int(f))
        except (ValueError, OverflowError):
            pass
    return s


def _norm_barcode(v: str) -> str:
    """Digits-only; zero-pad 11-digit UPC-A to 12 (vendor exports drop the 0).
    Returns '' for values whose digit length isn't a real barcode (8/12/13/14)
    so typos and Excel-mangled junk never enter the library or the
    auto-approve mirror."""
    d = re.sub(r"\D", "", v or "")
    if len(d) == 11:
        d = "0" + d
    return d if len(d) in (8, 12, 13, 14) else ""


def _split_multi(cell: str) -> list[str]:
    """Split a multi-value cell on commas/semicolons ('upc1, upc2')."""
    return [p.strip() for p in re.split(r"[,;]", cell or "") if p.strip()]


def _row_ids(upc_cell: str, ean_cell: str, mpn_cell: str) -> dict[str, list[str]]:
    """Normalize one row's identifier cells into {'upc': [...], ...}."""
    ids: dict[str, list[str]] = {}
    upcs = [n for n in (_norm_barcode(v) for v in _split_multi(upc_cell)) if n]
    eans = [n for n in (_norm_barcode(v) for v in _split_multi(ean_cell)) if n]
    mpns = [m.upper() for m in _split_multi(mpn_cell)]
    if upcs:
        ids["upc"] = list(dict.fromkeys(upcs))
    if eans:
        ids["ean"] = list(dict.fromkeys(eans))
    if mpns:
        ids["mpn"] = list(dict.fromkeys(mpns))
    return ids


def _map_headers(header_row: list) -> dict[str, int]:
    """Resolve a template header row → {field: column_index}."""
    out: dict[str, int] = {}
    for i, cell in enumerate(header_row):
        key = re.sub(r"\s+", " ", _cell(cell).lower()).strip()
        field = _HEADER_MAP.get(key)
        if field and field not in out:
            out[field] = i
    return out


def _parsed_rows(filename: str, data: bytes) -> tuple[dict[str, int], list[tuple[int, list]]]:
    """Parse upload → (header map, [(1-based row number, cells), ...]).
    The first non-empty row is the header."""
    try:
        rows = parse_raw_rows(filename, data)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Could not parse file: {exc}")
    header_idx = None
    for i, r in enumerate(rows):
        if any(_cell(c) for c in r):
            header_idx = i
            break
    if header_idx is None:
        raise HTTPException(status_code=400, detail="File appears to be empty")
    cols = _map_headers(rows[header_idx])
    body = [(i + 1, list(r)) for i, r in enumerate(rows) if i > header_idx]
    return cols, body


def _get(cells: list, cols: dict[str, int], field: str) -> str:
    idx = cols.get(field)
    if idx is None or idx >= len(cells):
        return ""
    return _cell(cells[idx])


def _xlsx_response(wb: Workbook, filename: str) -> StreamingResponse:
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    safe = "".join(c if (c.isascii() and (c.isalnum() or c in " -_")) else "_" for c in filename)
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{safe}.xlsx"'},
    )


def _barcode_forms(v: str) -> list[str]:
    """Equivalent barcode forms for matching (12↔13↔14-digit)."""
    out = [v]
    if v.isdigit():
        if len(v) == 12:
            out.append("0" + v)
        elif len(v) == 13:
            if v.startswith("0"):
                out.append(v[1:])
            out.append("0" + v)
        elif len(v) == 14 and v.startswith("00"):
            out.append(v[2:])
    return list(dict.fromkeys(out))


# --------------------------------------------------------------------------- #
# Pair Library — endpoints
# --------------------------------------------------------------------------- #


@router.get("/pairs/library/stats")
async def library_stats() -> dict:
    return database.pair_library_stats()


@router.post("/pairs/library/search")
async def library_search(body: PairQuery) -> dict:
    return {"results": database.search_pair_library(body.query)}


class LibraryDelete(BaseModel):
    asin: str
    id_type: str = ""      # optional: drop just one identifier ('upc'|'ean'|'mpn')
    identifier: str = ""


@router.post("/pairs/library/delete")
async def library_delete(body: LibraryDelete) -> dict:
    """Remove an incorrect Pair Library entry — a whole ASIN, or one identifier of
    it. Also clears the verified_items mirror so runs stop auto-approving the pair."""
    if not body.asin.strip():
        raise HTTPException(400, "asin is required.")
    res = database.delete_library_pair(body.asin, body.id_type, body.identifier)
    if not (res["deleted_ids"] or res["deleted_asin"]):
        raise HTTPException(404, "No matching Pair Library entry to remove.")
    return {"removed": True, **res}


@router.get("/pairs/library/template")
async def library_template(kind: str = "import") -> StreamingResponse:
    """Download the fixed import/lookup template with a Notes sheet."""
    wb = Workbook()
    ws = wb.active
    if kind == "lookup":
        ws.title = "Lookup"
        ws.append(_LOOKUP_HEADERS)
        notes = [
            "Fill ONE row per product you want to find. At least one identifier per row.",
            "UPC / EAN / MPN-Item ID — the identifier(s) to look up in the Pair Library.",
            "ASIN (optional) — if filled, the identifier+ASIN pair is SAVED to the library as confirmed.",
            "Brand (optional) — saved with new pairs.",
        ]
        fname = "pair_lookup_template"
    else:
        ws.title = "Pairs"
        ws.append(_IMPORT_HEADERS)
        notes = [
            "One row per ASIN. ASIN is required; at least one identifier (UPC/EAN/MPN) is required.",
            "The SAME UPC may appear on many rows with different ASINs — that's fine (one product, many Amazon listings); all are kept.",
            "Multiple UPCs for one ASIN: separate with commas in the UPC cell — first becomes primary, the rest aliases.",
            "For any ASIN you import, only the UPC(s) you give it count for it in runs (this overrides earlier slipped-through pairs).",
            "Brand/Manufacturer are optional but enable the per-brand export.",
            "amz pack (optional) — the Amazon pack quantity for that ASIN's listing.",
        ]
        fname = "pair_import_template"
    ns = wb.create_sheet("Notes")
    for n in notes:
        ns.append([n])
    return _xlsx_response(wb, fname)


@router.post("/pairs/library/import")
async def library_import(pairs_file: UploadFile = File(...)) -> dict:
    """Import a fixed-template pair spreadsheet into the Pair Library."""
    data = await read_upload_limited(pairs_file)
    cols, body = _parsed_rows(pairs_file.filename or "", data)

    if "asin" not in cols:
        raise HTTPException(
            status_code=400,
            detail="No ASIN column found. Use the import template: "
                   + " | ".join(_IMPORT_HEADERS),
        )
    if not any(f in cols for f in ("upc", "ean", "mpn")):
        raise HTTPException(
            status_code=400,
            detail="No identifier column (UPC/EAN/MPN) found. Use the import template: "
                   + " | ".join(_IMPORT_HEADERS),
        )

    def _process() -> dict:
        totals = {"rows": 0, "pairs_created": 0, "pairs_updated": 0,
                  "ids_added": 0, "blacklist_cleared": 0}
        skipped: list[dict] = []

        for row_no, cells in body:
            if not any(_cell(c) for c in cells):
                continue
            totals["rows"] += 1
            asin = _get(cells, cols, "asin").upper()
            if not _ASIN_RE.fullmatch(asin):
                skipped.append({"row": row_no, "reason": f"Invalid ASIN {asin!r}"})
                continue
            raw_upc = _get(cells, cols, "upc")
            raw_ean = _get(cells, cols, "ean")
            raw_mpn = _get(cells, cols, "mpn")
            ids = _row_ids(raw_upc, raw_ean, raw_mpn)
            if not ids:
                reason = (
                    "Invalid barcode — UPC/EAN must be 8/12/13/14 digits"
                    if (raw_upc or raw_ean) else "No identifier (UPC/EAN/MPN)"
                )
                skipped.append({"row": row_no, "reason": reason})
                continue
            r = database.upsert_library_pair(
                asin, ids,
                brand=_get(cells, cols, "brand"),
                manufacturer=_get(cells, cols, "manufacturer"),
                amz_pack=_get(cells, cols, "amz_pack"),
            )
            totals["pairs_created"]     += r["created"]
            totals["pairs_updated"]     += r["updated"]
            totals["ids_added"]         += r["ids_added"]
            totals["blacklist_cleared"] += r["blacklist_cleared"]

        return {**totals, "skipped": skipped[:30], "skipped_total": len(skipped)}

    # Per-row SQLite writes on a big file would otherwise block the event loop.
    return await asyncio.to_thread(_process)


_EXPORT_HEADERS = ["ASIN", "UPC", "UPC Aliases", "EAN", "EAN Aliases",
                   "MPN/Item ID", "MPN Aliases", "Brand", "Manufacturer", "amz pack", "Updated"]


def _library_row_cells(r: dict) -> list:
    return safe_spreadsheet_row([
        r["asin"], r["upc"], r["upc_aliases"], r["ean"], r["ean_aliases"],
        r["mpn"], r["mpn_aliases"], r["brand"], r["manufacturer"],
        r.get("amz_pack", ""), r["updated_at"],
    ])


@router.get("/pairs/library/export")
async def library_export(brand: str = "") -> StreamingResponse:
    """Export the Pair Library (one brand, or everything) to Excel."""
    rows = database.pair_library_rows(brand=brand.strip())
    if not rows:
        raise HTTPException(
            status_code=404,
            detail=f"No library pairs found for brand {brand!r}" if brand.strip()
                   else "The Pair Library is empty — import pairs first.",
        )
    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Pair Library")
    ws.append(_EXPORT_HEADERS)
    for r in rows:
        ws.append(_library_row_cells(r))
    return _xlsx_response(wb, f"pair_library_{brand.strip() or 'all'}")


@router.post("/pairs/library/lookup-export")
async def library_lookup_export(lookup_file: UploadFile = File(...)) -> StreamingResponse:
    """
    Match an uploaded identifier list (UPC/EAN/MPN per row) against the Pair
    Library and return an Excel with Matched + Not Found sheets.

    Read-only lookup: any row whose identifier isn't in the library goes to the
    Not Found sheet — including rows that supply an ASIN (nothing is auto-saved;
    use the Import feature to add pairs). Counts are in the X-Plib-* headers.
    """
    data = await read_upload_limited(lookup_file)
    cols, body = _parsed_rows(lookup_file.filename or "", data)

    if not any(f in cols for f in ("upc", "ean", "mpn")):
        raise HTTPException(
            status_code=400,
            detail="No identifier column (UPC/EAN/MPN) found. Use the lookup template: "
                   + " | ".join(_LOOKUP_HEADERS),
        )

    def _process() -> tuple[Workbook, int, int, int]:
        id_map = database.load_pair_library_map()          # (type, ident) → {asins}
        lib_by_asin = {r["asin"]: r for r in database.pair_library_rows()}

        def _find(ids: dict[str, list[str]]) -> set[str]:
            # A UPC can map to MANY ASINs — collect every match across the row's
            # identifiers and barcode forms (UPC↔EAN cross-checked).
            hits: set[str] = set()
            for t in ("upc", "ean", "mpn"):
                for val in ids.get(t, []):
                    forms = _barcode_forms(val) if t in ("upc", "ean") else [val]
                    for f in forms:
                        for lookup_t in (("upc", "ean") if t in ("upc", "ean") else (t,)):
                            hits |= id_map.get((lookup_t, f), set())
            return hits

        matched: list[list] = []
        not_found: list[list] = []
        n_added = 0
        n_match_lines = 0

        for row_no, cells in body:
            if not any(_cell(c) for c in cells):
                continue
            in_upc = _get(cells, cols, "upc")
            in_ean = _get(cells, cols, "ean")
            in_mpn = _get(cells, cols, "mpn")
            in_asin = _get(cells, cols, "asin").upper()
            ids = _row_ids(in_upc, in_ean, in_mpn)
            if not ids:
                reason = (
                    "Invalid barcode — UPC/EAN must be 8/12/13/14 digits"
                    if (in_upc or in_ean or in_mpn) else "No identifier in row"
                )
                not_found.append([row_no, in_upc, in_ean, in_mpn, in_asin, reason])
                continue

            asins = _find(ids)
            # Read-only lookup: not in the library → Not Found (even if the row
            # supplies an ASIN — we don't auto-save; use Import to add pairs).
            if not asins:
                reason = ("Not in Pair Library — ASIN " + in_asin + " supplied (not saved)"
                          if _ASIN_RE.fullmatch(in_asin) else "Not in Pair Library")
                not_found.append([row_no, in_upc, in_ean, in_mpn, in_asin, reason])
                continue

            # One output row per matching ASIN — so a UPC lists EVERY ASIN.
            for a in sorted(asins):
                n_match_lines += 1
                lib = lib_by_asin.get(a, {})
                matched.append([
                    row_no, in_upc, in_ean, in_mpn, in_asin, "Matched", a,
                    lib.get("brand", ""), lib.get("manufacturer", ""),
                    lib.get("upc", ""), lib.get("upc_aliases", ""),
                    lib.get("ean", ""), lib.get("mpn", ""),
                ])

        wb = Workbook(write_only=True)
        ws = wb.create_sheet("Matched")
        ws.append(["Row", "Input UPC", "Input EAN", "Input MPN", "Input ASIN",
                   "Status", "ASIN", "Brand", "Manufacturer",
                   "UPC", "UPC Aliases", "EAN", "MPN/Item ID"])
        for m in matched:
            ws.append(safe_spreadsheet_row(m))
        ws2 = wb.create_sheet("Not Found")
        ws2.append(["Row", "Input UPC", "Input EAN", "Input MPN", "Input ASIN", "Reason"])
        for n in not_found:
            ws2.append(safe_spreadsheet_row(n))
        return wb, n_match_lines, n_added, len(not_found)

    # Matching + per-row writes can be heavy — keep them off the event loop.
    wb, n_matched, n_added, n_notfound = await asyncio.to_thread(_process)

    resp = _xlsx_response(wb, "pair_lookup_results")
    resp.headers["X-Plib-Matched"] = str(n_matched)
    resp.headers["X-Plib-Added"] = str(n_added)
    resp.headers["X-Plib-Notfound"] = str(n_notfound)
    return resp
