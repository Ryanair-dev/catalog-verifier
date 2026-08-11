"""
Create PO — wizard backend.

  POST /api/create-po/preview   (multipart catalog_file)         -> headers + sample rows
  POST /api/create-po/brands    (json: token, brand config)      -> brand roster (status/mfr/prefix)
  POST /api/create-po/generate  (json: token, full config)       -> review board (groups/counts)
  POST /api/create-po/export    (json: token, edits)             -> .xlsx (bulk/kit/amz), streamed
  GET  /api/create-po/status
"""
from __future__ import annotations

import base64
import datetime
import io
import logging
import os
import uuid
from functools import lru_cache

from fastapi import APIRouter, Body, File, Form, HTTPException, UploadFile
from fastapi.responses import StreamingResponse

from services import azure_sql
from services import brand_map
from services import create_po as cp
from services import database
from services import sku_exports
from services.file_parser import get_sheet_names, parse_raw_rows
from services.sellercloud import get_sellercloud_client, sellercloud_configured
from services.sellercloud import catalog_index as ci

log = logging.getLogger(__name__)
router = APIRouter()


def _catalog_source(company: str = "Ford Medical") -> tuple:
    """(CatalogIndex, brand_map, source_label) for reference lookups, restricted to
    `company`'s own SKUs (a brand only under another company is treated as new).
    Prefer the LIVE Azure SellerCloud view; fall back to the local SC-data snapshot +
    the brand_mappings table when Azure is unconfigured or unreachable."""
    if azure_sql.is_configured():
        try:
            index, mapping = azure_sql.catalog_source(company)
            return index, mapping, "azure"
        except Exception as exc:   # network/token/query — degrade gracefully
            log.warning("Azure catalog unavailable (%s) — using local snapshot", exc)
    return ci.local_index(), brand_map.get_map(), "snapshot"   # NOTE: snapshot isn't company-filtered

# token -> accumulating wizard session (rows, config, derived state). In-memory is
# fine for this local single-user app.
_SESS: dict[str, dict] = {}
_AI_CACHE: dict[str, str] = {}


@router.get("/create-po/status")
def status() -> dict:
    return {"sellercloud_configured": sellercloud_configured()}


@lru_cache(maxsize=1)
def _brand_name_to_id() -> dict[str, int]:
    client = get_sellercloud_client()
    out = {}
    for b in client.get_brands():
        name = str(b.get("Value", "")).strip()
        if name:
            out[name.lower()] = b.get("Key")
    return out


def _catalog_brand_names() -> set[str]:
    return {n.upper() for n in _brand_name_to_id().keys()}


def _ai_manufacturer(brand: str) -> str:
    """Best-effort: ask Claude for a new brand's manufacturer. Returns '' on failure."""
    key = brand.strip().lower()
    if key in _AI_CACHE:
        return _AI_CACHE[key]
    result = ""
    try:
        import anthropic
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if api_key:
            client = anthropic.Anthropic(api_key=api_key)
            model = os.getenv("BRAND_EXTRACTION_MODEL") or "claude-haiku-4-5-20251001"
            msg = client.messages.create(
                model=model, max_tokens=30,
                messages=[{"role": "user", "content":
                    f"What company manufactures or owns the consumer/medical brand "
                    f"\"{brand}\"? Reply with ONLY the manufacturer or parent-company "
                    f"name, nothing else. If unsure, reply UNKNOWN."}],
            )
            text = "".join(getattr(b, "text", "") for b in msg.content).strip()
            if text and text.upper() != "UNKNOWN":
                result = text
    except Exception:
        result = ""
    _AI_CACHE[key] = result
    return result


def _require(token: str) -> dict:
    sess = _SESS.get(token)
    if not sess:
        raise HTTPException(404, "Session expired — re-upload the file.")
    return sess


def _brand_ids_for(brand_names: set[str]) -> list[int]:
    lookup = _brand_name_to_id()
    return [lookup[n.lower()] for n in brand_names if n.lower() in lookup]


# ── Step 1: preview (with sheet + header-row picking) ────────────────────────
def _parse_with_header(filename: str, data: bytes, sheet_name: str, header_row: int):
    """(headers, dict_rows) reading `sheet_name` and treating 1-based `header_row`
    as the header line — everything below it is data."""
    raw = parse_raw_rows(filename, data, sheet_name)
    hi = max(0, int(header_row or 1) - 1)
    if hi >= len(raw):
        return [], []
    hdr = raw[hi]
    headers = [str(h).strip() if h not in (None, "") else f"Column {i + 1}"
               for i, h in enumerate(hdr)]
    body = [dict(zip(headers, list(r))) for r in raw[hi + 1:]
            if r is not None and not all(v in (None, "") for v in r)]
    return headers, body


def _preview_payload(sess: dict) -> dict:
    headers, rows = sess["headers"], sess["rows"]
    return {
        "headers": headers,
        "rows": [{h: _v(r.get(h)) for h in headers} for r in rows[:20]],
        "row_count": len(rows), "col_count": len(headers),
        "sheets": sess.get("sheets", []), "active_sheet": sess.get("sheet", ""),
        "header_row": sess.get("header_row", 1),
    }


@router.post("/create-po/preview")
async def preview(catalog_file: UploadFile = File(...)) -> dict:
    # No SellerCloud needed — the catalog index is the live Azure view (snapshot
    # fallback). We keep the raw bytes so the sheet / header row can be re-picked.
    data = await catalog_file.read()
    fname = catalog_file.filename or "items.xlsx"
    sheets = get_sheet_names(fname, data)
    active = sheets[0] if sheets else ""
    headers, dict_rows = _parse_with_header(fname, data, active, 1)
    if not headers:
        raise HTTPException(400, "Could not read any columns from the uploaded file.")
    token = uuid.uuid4().hex
    _SESS[token] = {"filename": fname, "data": data, "sheets": sheets,
                    "sheet": active, "header_row": 1, "headers": headers, "rows": dict_rows}
    return {"token": token, "filename": fname, **_preview_payload(_SESS[token])}


@router.post("/create-po/reparse")
def reparse(body: dict = Body(...)) -> dict:
    """Re-read the uploaded file with a different sheet and/or header row."""
    sess = _require(body.get("token", ""))
    if "data" not in sess:
        raise HTTPException(400, "Re-upload the file (raw data not in session).")
    sheet = body.get("sheet_name", sess.get("sheet", ""))
    header_row = int(body.get("header_row") or sess.get("header_row") or 1)
    headers, dict_rows = _parse_with_header(sess["filename"], sess["data"], sheet, header_row)
    if not headers:
        raise HTTPException(400, "No columns found for that sheet / header row.")
    sess.update({"sheet": sheet, "header_row": header_row, "headers": headers, "rows": dict_rows})
    return _preview_payload(sess)


@router.get("/create-po/history")
def history(limit: int = 20) -> dict:
    return {"exports": sku_exports.list_recent(limit)}


@router.post("/create-po/check-sku")
def check_sku(body: dict = Body(...)) -> dict:
    """Look up a single (possibly hand-edited) SKU string against the live catalog,
    so the review board can re-flag 'on SellerCloud' when the user changes a SKU by
    hand. Checks the SKU STRING itself (not the row's UPC/MPN)."""
    sess = _require(body.get("token", ""))
    sku = _v(body.get("sku"))
    field = _v(body.get("field")) or "main"
    index = sess.get("index")
    if index is None:
        index, mapping, _src = _catalog_source(sess.get("config", {}).get("company", "Ford Medical"))
        sess["index"], sess["mapping"] = index, mapping
    on_sc = bool(sku) and index.sku_exists(sku)
    return {
        "sku": sku, "field": field, "on_sc": on_sc,
        "note": "Already on SellerCloud — will not recreate" if on_sc else "",
    }


# ── Step 2: brand roster ──────────────────────────────────────────────────
@router.post("/create-po/brands")
def brands(body: dict = Body(...)) -> dict:
    sess = _require(body.get("token", ""))
    brand_mode = body.get("brand_mode", "single")
    rows = sess["rows"]

    if brand_mode == "single":
        name = str(body.get("single_brand", "")).strip()
        items = [{"brand": name} for _ in rows] if name else []
    else:
        col = body.get("brand_col") or cp.brand_column(body.get("col_map", {})) or "Brand"
        items = [{"brand": cp._s(r.get(col))} for r in rows]
        items = [it for it in items if it["brand"]]

    index, mapping, _src = _catalog_source(body.get("company", "Ford Medical"))
    sess["index"] = index
    sess["mapping"] = mapping

    resolved = cp.resolve_brands(
        items, index=index, catalog_brand_names=index.brand_names,
        overrides=body.get("brand_overrides"), ai_suggest=_ai_manufacturer,
        mapping=mapping, company=body.get("company", "Ford Medical"),
    )
    return {
        "brand_col": (body.get("brand_col") or cp.brand_column(body.get("col_map", {}))),
        "count": len(resolved),
        "brands": [{
            "brand": bi.brand, "brand_name": bi.brand_name, "status": bi.status,
            "manufacturer": bi.manufacturer, "prefix": bi.prefix, "ai_mfr": bi.ai_mfr,
            "row_count": bi.row_count, "purchaser": bi.purchaser, "sourcer": bi.sourcer,
        } for bi in resolved.values()],
    }


# ── Step 3: generate ────────────────────────────────────────────────────────
@router.post("/create-po/generate")
def generate(body: dict = Body(...)) -> dict:
    sess = _require(body.get("token", ""))
    config = _config(body)
    items = cp.normalize_rows(sess["rows"], config["col_map"])
    if config["brand_mode"] == "single":
        for it in items:
            it["brand"] = config.get("single_brand", "")

    index = sess.get("index")
    mapping = sess.get("mapping")
    if index is None or mapping is None:
        index, mapping, _src = _catalog_source(config.get("company", "Ford Medical"))
    sess["index"] = index
    sess["mapping"] = mapping
    brands_info = cp.resolve_brands(
        items, index=index, catalog_brand_names=index.brand_names,
        overrides=config.get("brand_overrides"), ai_suggest=_ai_manufacturer,
        mapping=mapping, company=config.get("company", "Ford Medical"),
    )
    results = cp.derive_rows(items, config=config, index=index, brands=brands_info)

    sess.update({"config": config, "items": items, "brands": brands_info})
    suf_fba, suf_fbm = cp.suffixes(config["company"])
    return {
        "token": body["token"], "counts": cp.counts(results),
        "groups": cp.build_groups(results),
        "suffixes": {"fba": suf_fba, "fbm": suf_fbm},
    }


# ── AI ProductName cleanup ───────────────────────────────────────────────────
_TITLE_CACHE: dict[str, str] = {}


def _apply_ai_titles(items: list[dict]) -> None:
    """Rewrite each vendor title into a clean, readable description and store it on
    the item as `clean_name` (brand + UOM are added later in build_files).

    Connected to the shared Abbreviation Library: known abbr→full mappings are fed in
    as context so expansions stay consistent, and any NEW abbreviation/count/pack
    shorthand the AI expands is saved back (tagged `added_by='ai'`) so the table grows.
    Batched Haiku calls, cached by raw title, best-effort (leaves the raw title on
    failure)."""
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return
    todo: list[tuple[dict, str, str]] = []   # (item, raw_title, brand)
    for it in items:
        raw = _v(it.get("name"))
        if not raw:
            continue
        if raw in _TITLE_CACHE:
            it["clean_name"] = _TITLE_CACHE[raw]
        else:
            todo.append((it, raw, _v(it.get("brand"))))
    if not todo:
        return

    # Known abbreviations from the shared Library — passed as context + used to dedupe
    # what the AI reports back as "new".
    try:
        known = database.flat_library()
    except Exception:
        known = []
    known_lower = {_v(e.get("abbr")).lower() for e in known if e.get("abbr")}
    ref = "; ".join(f"{e['abbr']}={e['full']}" for e in known
                    if e.get("abbr") and e.get("full"))[:4000]

    try:
        import json as _json

        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        model = os.getenv("BRAND_EXTRACTION_MODEL") or "claude-haiku-4-5-20251001"
        instr = (
            "You clean up messy vendor product titles for a catalog. For each numbered "
            "line, rewrite it into a short, readable product description.\n"
            "RULES:\n"
            "- EXPAND abbreviations, unit shorthands, and pack/count shorthands into full "
            "readable words (e.g. UNSC->Unscented, SOL->Solution, CT->Count, PK->Pack). "
            "Use the KNOWN list below for consistency.\n"
            "- Remove the brand name and any sub-brand/series/marketing words, model or "
            "SKU codes (e.g. TK-075, WH-GR, Z212), trademark symbols (®/™), and filler.\n"
            "- KEEP the real product type, material, colour, size/dimension and key "
            "features, in natural order.\n"
            "- Do NOT add a unit or pack suffix like '(Each)'.\n"
            "- The bracketed [brand] is context only — do not include it in the output.\n"
            'Return ONLY a JSON array, one object per input line IN ORDER:\n'
            '  {"name": "<clean description>", "abbr": [{"a":"<abbrev>","f":"<expansion>"}]}\n'
            'where "abbr" lists ONLY abbreviations you expanded that are NOT already in '
            'the KNOWN list (empty array if none).\n'
            f"KNOWN abbreviations: {ref or '(none yet)'}"
        )
        learned = 0
        for i in range(0, len(todo), 25):
            chunk = todo[i:i + 25]
            listing = "\n".join(
                f"{j + 1}. [{b or '?'}] {raw}" for j, (_it, raw, b) in enumerate(chunk)
            )
            try:
                msg = client.messages.create(
                    model=model, max_tokens=3000,
                    messages=[{"role": "user", "content": f"{instr}\n\n{listing}"}],
                )
                text = "".join(getattr(p, "text", "") for p in msg.content)
                arr = _json.loads(text[text.find("["):text.rfind("]") + 1])
                for (it, raw, _b), obj in zip(chunk, arr):
                    if isinstance(obj, str):            # tolerate old string format
                        name, new_abbr = obj.strip(), []
                    elif isinstance(obj, dict):
                        name = _v(obj.get("name"))
                        new_abbr = obj.get("abbr") or []
                    else:
                        continue
                    if name:
                        it["clean_name"] = name
                        _TITLE_CACHE[raw] = name
                    for na in new_abbr:               # grow the Library with AI expansions
                        if not isinstance(na, dict):
                            continue
                        a, f = _v(na.get("a")), _v(na.get("f"))
                        if not a or not f or a.lower() == f.lower() or a.lower() in known_lower:
                            continue
                        try:
                            created, _ = database.add_library_entry(
                                "Product Attributes", a, f, added_by="ai")
                        except Exception:
                            created = False
                        known_lower.add(a.lower())
                        if created:
                            learned += 1
            except Exception:
                continue   # best-effort per chunk
        if learned:
            log.info("Create SKUs: learned %d new abbreviation(s) from titles", learned)
    except Exception:
        return


# ── Step 4: export ──────────────────────────────────────────────────────────
@router.post("/create-po/export")
def export(body: dict = Body(...)) -> dict:
    sess = _require(body.get("token", ""))
    if "items" not in sess:
        raise HTTPException(400, "Run Generate before exporting.")
    if body.get("clean_titles", True):
        _apply_ai_titles(sess["items"])   # sets item['clean_name']; read by build_files
    edits = body.get("edits") or {}
    results = cp.derive_rows(sess["items"], config=sess["config"],
                             index=sess.get("index"), brands=sess["brands"], edits=edits)

    # Tag each batch with a unique ID + today's date → filenames "Bulk_<ID>_<date>.xlsx"
    export_id = uuid.uuid4().hex[:6].upper()
    export_date = datetime.date.today().isoformat()
    files = cp.build_files(results, sess["config"], tag=f"{export_id}_{export_date}")

    cfg = sess["config"]
    # Remember NEW brands we just exported (prefix + manufacturer) so they resolve
    # instantly next time instead of coming back blank / re-hitting the AI.
    _company = cfg.get("company", "Ford Medical")
    for bi in (sess.get("brands") or {}).values():
        if getattr(bi, "status", "") == "new" and getattr(bi, "prefix", ""):
            cp.save_brand_reference(bi.brand, bi.prefix, _company, bi.manufacturer)
    try:
        sku_exports.record(
            export_id=export_id, export_date=export_date,
            company=cfg.get("company", ""), batch_type=cfg.get("batch_type", ""),
            total_mains=sum(1 for r in results if r.main and not r.main_on_sc),
            total_fba=sum(1 for r in results if r.fba and not r.fba_on_sc),
            total_fbm=sum(1 for r in results if r.fbm and not r.fbm_on_sc),
            total_kits=sum(1 for r in results if r.kit_value),
            files=[name for name, _ in files],
        )
    except Exception as exc:
        log.warning("failed to log sku export: %s", exc)

    return {"export_id": export_id, "export_date": export_date,
            "files": [{"name": name, "b64": base64.b64encode(data).decode("ascii")}
                      for name, data in files]}


def _config(body: dict) -> dict:
    return {
        "batch_type": body.get("batch_type", "CPG"),
        "create_by": body.get("create_by", "UPC"),
        "company": body.get("company", "Ford Medical"),
        "create": body.get("create", {"main": True, "shadow": True, "kit": True}),
        "col_map": body.get("col_map", {}),
        "brand_mode": body.get("brand_mode", "single"),
        "single_brand": body.get("single_brand", ""),
        "single_mfr": body.get("single_mfr", ""),
        "single_prefix": body.get("single_prefix", ""),
        "single_purchaser": body.get("single_purchaser", ""),
        "single_sourcer": body.get("single_sourcer", ""),
        "brand_overrides": body.get("brand_overrides", {}),
        "purchaser": body.get("purchaser", {"mode": "value", "value": ""}),
        "sourcer": body.get("sourcer", {"mode": "value", "value": ""}),
    }


def _v(c) -> str:
    return str(c if c is not None else "").strip()
