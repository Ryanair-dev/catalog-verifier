"""
PO Analytics — analyze received Purchase Orders for FBA profitability.

Given one or more SellerCloud PO numbers: pull each PO's line items, resolve
every Main SKU to its FBA/FBM shadow children (+ASIN, +pack qty) via the local
catalog snapshot, then enrich each distinct ASIN with the same live data
sources the Price Desk "Offer Analytics" export already uses (Keepa buy-box/
fee/rank data, SP-API eligibility, SP-API 30d Sales & Traffic) and compute the
same Net Profit/Margin/ROI/Referral-Fee formulas.

One PO line item (one Main SKU) expands into one row per distinct ASIN among
its FBA-shadow pack variants (e.g. a vendor case of 50-count wraps that we
list both individually and as a 5-pack/25-pack on Amazon → 2 rows, one per
ASIN). "PO qty" (that Main SKU's total received qty) is IDENTICAL across all
of its variant rows — the received stock is one shared pool, not pre-split by
pack size. "PO Asins" = PO qty / amz pack: "if all of this stock went into
this specific pack size, how many sellable units would result" — a planning
number, not an inventory split (confirmed against the ground-truth reference
file `49932 Blazy Susan.xlsx`'s own `=PO qty / amz pack` formula).

Two columns are deliberately left blank for manual entry (confirmed with the
user, 2026-10-06): "90d sales" (no 90-day Sales & Traffic report exists yet)
and "FBA" (the qty the purchaser decides to send to FBA and replenish the
SKU with — a human planning decision, not derivable from any API).

Filename / brand resolution (confirmed with the user): look at the distinct
first-3-char SKU prefixes across every line item in the batch. Exactly one
prefix → resolve it to its real brand name via the existing Create-SKUs
brand↔prefix memory (services.sc_reference, same tables Create SKUs already
builds/maintains). More than one distinct prefix (the PO/batch spans more
than one brand) → fall back to the PO's vendor name instead.
"""
from __future__ import annotations

import datetime as dt
import io
import logging
import re
from collections import defaultdict

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from services import database, sc_reference
from services import keepa as keepa_api
from services.sellercloud.catalog_index import local_index
from services.sellercloud.client import get_sellercloud_client
from services.spapi import reports as sp_reports
from services.vendor_offers_template_export import (
    ExportCancelled, _f, _fba_total_by_asin, enrich_asins,
)

log = logging.getLogger(__name__)


# ── Analytics sheet layout ───────────────────────────────────────────────────
# Column order per explicit user instruction (2026-10-06): "PO qty and PO Asin"
# placed right after "FBA total" — NOT where the hand-built reference file
# happens to have them (after "Estimated Monthly Units Sold").
ANALYTICS_COLS = [
    "SKU", "FBA SKU", "UPC", "ASIN", "Link", "Approved", "vendor title", "Amazon title",
    "FBA total", "PO qty", "PO Asins", "30d sales", "90d sales",
    "Estimated Monthly Units Sold", "FBA", "amz pack",
    "Product Cost", "Product cost * amz pack",
    "Buy Box Price current", "BB Price 30d", "BB Price 90d",
    "Net Profit", "Net Margin", "ROI",
    "FBA Fee", "Referral Fee", "Storage Fee", "Prep & Out fee",
    "Rank (current)", "Rank 30d", "BB seller", "BB share 30d", "BB share 90d",
    "Amazon 30d %", "Amazon 90d %", "FBM sellers", "FBA sellers",
    "Brand", "Category", "Total Ratings Count", "Variation Parent",
]
COL = {h: i + 1 for i, h in enumerate(ANALYTICS_COLS)}


def L(name: str) -> str:
    return get_column_letter(COL[name])


# Columns the user confirmed are manual-only — never system-filled.
MANUAL_ONLY_COLS = {"90d sales", "FBA"}

PO_SHEET_COLS = [
    "POItemID", "ProductID", "Vendor SKU", "Product Name", "LOC",
    "Unit Price", "Unit Discount", "Adjusted Price", "Qty Ordered", "Qty/Case",
    "Qty Received", "Local Notes", "Sub Total", "UPC", "ExtraCostPerUnit", "VendorName",
]
PCOL = {h: i + 1 for i, h in enumerate(PO_SHEET_COLS)}


def _pl(name: str) -> str:
    return get_column_letter(PCOL[name])


_HDR_FILL = PatternFill("solid", fgColor="FFE9E9E9")
_HDR_FONT = Font(bold=True)
_HDR_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)
_CUR = r'"$"#,##0.00_);\("$"#,##0.00\)'


def _parse_po_numbers(raw: str) -> list[int]:
    """'49932, 49933' / '49932 49933' / '49932' -> [49932, 49933]."""
    nums = re.findall(r"\d+", raw or "")
    seen: list[int] = []
    for n in nums:
        v = int(n)
        if v not in seen:
            seen.append(v)
    return seen


def _resolve_brand_label(client, items: list[dict], vendor_ids: set[int],
                          company: str = "Ford Medical") -> str:
    prefixes = {str(it.get("ProductID") or "")[:3].upper() for it in items if it.get("ProductID")}
    prefixes.discard("")
    if len(prefixes) == 1:
        try:
            sc_reference.ensure_tables()
            brand = sc_reference.brand_for_prefix(next(iter(prefixes)), company)
        except Exception:  # noqa: BLE001
            # sc_brand_prefix may not be built yet (first boot — the Azure
            # warm-up that populates it runs in a background thread) — fall
            # through to the vendor-name fallback rather than failing the run.
            brand = None
        if brand:
            return brand
    # multiple brands in this batch, or the single prefix isn't known yet ->
    # fall back to the vendor name (first vendor id if several POs disagree).
    for vid in vendor_ids:
        if not vid:
            continue
        try:
            v = client.get_vendor(vid)
            name = (v or {}).get("Name")
            if name:
                return name
        except Exception:  # noqa: BLE001
            continue
    return "PO Analytics"


def gather(po_numbers: list[int], company: str = "Ford Medical") -> dict:
    """Fast phase: pull the PO(s) from SellerCloud, resolve every Main SKU to
    its FBA/FBM children via the local catalog snapshot, and build the base
    (un-enriched) row list + the raw PO-sheet rows. No Keepa/SP-API calls."""
    client = get_sellercloud_client()
    pos: list[dict] = []
    all_items: list[dict] = []
    vendor_ids: set[int] = set()
    for num in po_numbers:
        po = client.get_purchase_order(num)
        pos.append(po)
        items = po.get("Items") or []
        all_items.extend(items)
        vid = (po.get("Purchase") or {}).get("VendorId")
        if vid:
            vendor_ids.add(vid)

    brand_label = _resolve_brand_label(client, all_items, vendor_ids, company)

    idx = local_index()
    base_rows: list[dict] = []
    for it in all_items:
        main_sku = str(it.get("ProductID") or "").strip()
        if not main_sku:
            continue
        po_qty = _f(it.get("QtyReceived")) or 0
        product_cost = _f(it.get("AdjustedPrice"))
        vendor_title = it.get("ProductName") or it.get("ProductNameFromProductTable") or ""
        upc = str(it.get("UPC") or "").strip()

        children = idx.children_for_main(main_sku)
        by_asin: dict[str, dict] = defaultdict(dict)
        for ch in children:
            if not ch.get("asin"):
                continue
            slot = by_asin[ch["asin"]]
            slot["pack_qty"] = ch["pack_qty"]
            slot[ch["channel"].lower()] = ch["sku"]   # fba / fbm

        if not by_asin:
            # no FBA/FBM shadow found for this Main SKU yet — still surface the
            # line item (ASIN/amz-pack blank) rather than silently dropping it.
            base_rows.append({
                "sku": main_sku, "fba_sku": None, "upc": upc, "asin": None,
                "po_qty": po_qty, "product_cost": product_cost,
                "vendor_title": vendor_title, "pack_qty": 1,
            })
            continue

        for asin, info in by_asin.items():
            base_rows.append({
                "sku": main_sku,
                "fba_sku": info.get("fba") or info.get("fbm"),
                "upc": upc, "asin": asin,
                "po_qty": po_qty, "product_cost": product_cost,
                "vendor_title": vendor_title,
                "pack_qty": info.get("pack_qty") or 1,
            })

    po_rows: list[dict] = []
    vendor_names: dict[int, str] = {}
    for po in pos:
        vid = (po.get("Purchase") or {}).get("VendorId")
        if vid and vid not in vendor_names:
            try:
                vendor_names[vid] = (client.get_vendor(vid) or {}).get("Name") or ""
            except Exception:  # noqa: BLE001
                vendor_names[vid] = ""
        vname = vendor_names.get(vid, "")
        for it in (po.get("Items") or []):
            po_rows.append({
                "po_item_id": it.get("ID"), "product_id": it.get("ProductID"),
                "vendor_sku": it.get("VendorSKU"), "product_name": it.get("ProductName"),
                "unit_price": _f(it.get("PricePerCase")),
                "unit_discount": _f(it.get("DiscountValue")) or 0,
                "qty_ordered": _f(it.get("TotalCases")),
                "qty_per_case": _f(it.get("QtyPerCase")),
                "local_notes": it.get("POItemNotesLocal") or "",
                "sub_total": _f(it.get("Total")),
                "upc": it.get("UPC"),
                "extra_cost_per_unit": _f(it.get("ExtraCostPerUnit")),
                "vendor_name": vname,
            })

    return {
        "po_numbers": po_numbers, "brand_label": brand_label,
        "rows": base_rows, "po_rows": po_rows,
    }


def enrich_and_build(gathered: dict, on_progress=None, should_cancel=None) -> bytes:
    """Slow phase: live Keepa + SP-API eligibility/storage + 30d Sales & Traffic
    for every distinct ASIN, then assemble the 2-sheet workbook."""
    rows = gathered["rows"]
    asins = sorted({r["asin"] for r in rows if r.get("asin")})

    _kp = (lambda d, t: on_progress(d, t, "Keepa live prices")) if on_progress else None
    keepa = keepa_api.fetch_products(asins, on_progress=_kp, should_cancel=should_cancel)

    _ep = (lambda d, t: on_progress(d, t, "eligibility check")) if on_progress else None
    live_elig, live_storage, _gen = enrich_asins(asins, _ep, should_cancel)

    fba_total = _fba_total_by_asin(asins)
    report30 = sp_reports.get_units_30d()

    def z(v):
        f = _f(v)
        return f if f is not None else 0

    pct = lambda v: z(v) / 100

    wb = Workbook()
    ws = wb.active
    ws.title = "Analytics"
    for c, name in enumerate(ANALYTICS_COLS, start=1):
        cell = ws.cell(row=1, column=c, value=name)
        cell.fill, cell.font, cell.alignment = _HDR_FILL, _HDR_FONT, _HDR_ALIGN
        ws.column_dimensions[get_column_letter(c)].width = 16
    ws.freeze_panes = f"{L('Approved')}2"

    r = 2
    for row in rows:
        asin = row.get("asin")
        key = (asin or "").strip().upper()
        k = keepa.get(key, {}) if asin else {}
        pack = row.get("pack_qty") or 1
        bb, bb30, bb90 = (_f(k.get("current_buybox_usd")), _f(k.get("buybox_30d")),
                          _f(k.get("buybox_90d")))
        bb_cur = bb if bb is not None else (bb30 if bb30 is not None else (bb90 if bb90 is not None else 0))
        storage = live_storage.get(asin) or 0.1
        approved = live_elig.get(asin)
        rowd = {
            "SKU": row["sku"], "FBA SKU": row.get("fba_sku"),
            "UPC": int(row["upc"]) if str(row.get("upc") or "").isdigit() else row.get("upc"),
            "ASIN": asin,
            "Link": f'=HYPERLINK("https://www.amazon.com/dp/"&{L("ASIN")}{r})' if asin else None,
            "Approved": approved,
            "vendor title": row.get("vendor_title"), "Amazon title": k.get("title"),
            "FBA total": fba_total.get(key) if asin else None,
            "PO qty": row.get("po_qty"),
            "PO Asins": f'={L("PO qty")}{r}/{L("amz pack")}{r}',
            "30d sales": report30.get(key) if asin else None,
            "90d sales": None,                               # manual (no 90d report yet)
            "Estimated Monthly Units Sold": z(k.get("monthly_sold_quantity")),
            "FBA": None,                                      # manual (replenishment qty)
            "amz pack": pack,
            "Product Cost": row.get("product_cost"),
            "Product cost * amz pack": f'={L("Product Cost")}{r}*{L("amz pack")}{r}',
            "Buy Box Price current": round(bb_cur, 2) if bb_cur else 0,
            "BB Price 30d": z(k.get("buybox_30d")), "BB Price 90d": z(k.get("buybox_90d")),
            "Net Profit": (f'={L("Buy Box Price current")}{r}-{L("Product cost * amz pack")}{r}'
                           f'-{L("FBA Fee")}{r}-{L("Referral Fee")}{r}-{L("Storage Fee")}{r}'
                           f'-{L("Prep & Out fee")}{r}'),
            "Net Margin": f'=IFERROR({L("Net Profit")}{r}/{L("Buy Box Price current")}{r},0)',
            "ROI": f'=IFERROR({L("Net Profit")}{r}/{L("Product cost * amz pack")}{r},0)',
            "FBA Fee": z(k.get("fba_fees_usd")),
            "Referral Fee": (f'=IF({L("Buy Box Price current")}{r}>10,'
                              f'{L("Buy Box Price current")}{r}*0.15,{L("Buy Box Price current")}{r}*0.08)'),
            "Storage Fee": round(storage, 2),
            "Prep & Out fee": float(database.get_setting("prep_out_fee", "0.25")),
            "Rank (current)": z(k.get("sales_rank_current")), "Rank 30d": z(k.get("sales_rank_30d")),
            "BB seller": k.get("buybox_seller_name"),
            "BB share 30d": pct(k.get("top_seller_30d_percentage")),
            "BB share 90d": pct(k.get("top_seller_90d_percentage")),
            "Amazon 30d %": pct(k.get("amazon_bb_30d_percentage")),
            "Amazon 90d %": pct(k.get("amazon_bb_90d_percentage")),
            "FBM sellers": z(k.get("offer_count_fbm")), "FBA sellers": z(k.get("offer_count_fba")),
            "Brand": k.get("brand"), "Category": k.get("category"),
            "Total Ratings Count": z(k.get("reviews_rating_count")),
            "Variation Parent": k.get("parent_asin") or k.get("variation_asin"),
        }
        for name, c in COL.items():
            cell = ws.cell(row=r, column=c, value=rowd.get(name))
            if name in ("Product Cost", "FBA Fee", "Referral Fee", "Storage Fee", "Prep & Out fee"):
                cell.number_format = _CUR
            elif name in ("Buy Box Price current", "BB Price 30d", "BB Price 90d"):
                cell.number_format = _CUR
            elif name in ("Net Margin", "ROI", "BB share 30d", "BB share 90d",
                          "Amazon 30d %", "Amazon 90d %"):
                cell.number_format = "0%"
        r += 1

    ws2 = wb.create_sheet("PO")
    for c, name in enumerate(PO_SHEET_COLS, start=1):
        cell = ws2.cell(row=1, column=c, value=name)
        cell.fill, cell.font, cell.alignment = _HDR_FILL, _HDR_FONT, _HDR_ALIGN
        ws2.column_dimensions[get_column_letter(c)].width = 16

    r2 = 2
    for pr in gathered["po_rows"]:
        rowd2 = {
            "POItemID": pr["po_item_id"], "ProductID": pr["product_id"],
            "Vendor SKU": pr["vendor_sku"], "Product Name": pr["product_name"],
            "LOC": "",
            "Unit Price": pr["unit_price"], "Unit Discount": pr["unit_discount"],
            "Adjusted Price": f'={_pl("Unit Price")}{r2}/{_pl("Qty/Case")}{r2}',
            "Qty Ordered": pr["qty_ordered"], "Qty/Case": pr["qty_per_case"],
            "Qty Received": f'={_pl("Qty/Case")}{r2}*{_pl("Qty Ordered")}{r2}',
            "Local Notes": pr["local_notes"], "Sub Total": pr["sub_total"],
            "UPC": int(pr["upc"]) if str(pr.get("upc") or "").isdigit() else pr.get("upc"),
            "ExtraCostPerUnit": pr["extra_cost_per_unit"], "VendorName": pr["vendor_name"],
        }
        for name, c in PCOL.items():
            cell = ws2.cell(row=r2, column=c, value=rowd2.get(name))
            if name in ("Unit Price", "Unit Discount", "Adjusted Price", "Sub Total",
                       "ExtraCostPerUnit"):
                cell.number_format = _CUR
        r2 += 1

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def po_analytics_filename(po_numbers: list[int], brand_label: str) -> str:
    """'<PO#>[ <PO#>...] <Brand or Vendor>.xlsx' per the house naming convention."""
    nums = " ".join(str(n) for n in po_numbers)
    safe_brand = re.sub(r"[^A-Za-z0-9 &._-]", "", (brand_label or "").strip()) or "PO Analytics"
    return f"{nums} {safe_brand}.xlsx"
