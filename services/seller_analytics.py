"""
Seller Analytics — analyze a competitor's (or your own) full Amazon seller
ASIN catalog, producing literally the same Offer-Analytics template workbook
(services/vendor_offers_template_export.py), just sourced from a Seller ID's
ASIN roster instead of our own vendor offers' UPCs.

Where Offer Analytics starts from a UPC (a vendor offer we've received) and
has to resolve it to an ASIN, Seller Analytics starts from a Seller ID, and
Keepa's /seller?storefront=1 endpoint already hands back that seller's
complete CURRENT ASIN list directly (flat ~10 tokens, confirmed live) — there
is no UPC/ASIN "recovery pass" needed here, unlike the vendor-offer path,
because the seller ID itself is the confirmed identity; nothing to resolve.

From there it's the exact same enrichment Offer Analytics already does for
every ASIN: Keepa /product (pricing, fees, rank, brand, category, dims) +
our own SellerCloud catalog (Main SKU / FBA SKU / FBA total) + live SP-API
(Approved / storage).

The one real difference: Offer Analytics' "Product Cost" column is always a
real vendor-quoted cost. A seller's ASIN we DON'T carry has no such cost, so
for those rows "Product Cost" becomes a computed TARGET cost instead — the
highest per-unit price we could pay and still clear the caller-chosen target
ROI%, solved from the exact same Net Profit formula every other row already
uses:

    Target Pack Cost = (BuyBoxCurrent - AllFees) / (1 + roi_pct/100)
    Target Unit Cost = Target Pack Cost / amz pack

For an ASIN we already DO carry (a Main SKU exists in SellerCloud), the real
latest PO cost + vendor name are shown instead (same as Offer Analytics'
"Last cost"/"Vendor" columns) — the target-cost math is skipped entirely for
those rows, since a real number is always preferred over an estimate.
"""
from __future__ import annotations

import datetime as dt
import io
import logging
import re

from openpyxl import load_workbook
from openpyxl.formatting.formatting import ConditionalFormattingList
from openpyxl.formatting.rule import Rule
from openpyxl.styles import Font, PatternFill
from openpyxl.styles.differential import DifferentialStyle
from openpyxl.utils import get_column_letter

from services import database
from services import keepa as keepa_api
from services.spapi import reports as sp_reports
from services.vendor_offers_template_export import (
    _TEMPLATE, TEMPLATE_COLS, COL, L, _NEW_HEADER_COLS, _ROBOTO, _BOLD, _CENTER,
    _HYPERLINK_FONT, _HDR_GREY, _HDR_ALIGN, _FILLS, _f, _storage_est,
    _sku_data_by_asin, _fba_total_by_asin, _last_po_by_sku,
    _sellersnap_30d_by_asin, enrich_asins, ExportCancelled,
)

log = logging.getLogger(__name__)

DEFAULT_ROI_PCT = 30.0


def seller_lookup(seller_id: str) -> dict:
    """What the UI's 'Look up seller' step calls before committing to a full
    run — just the ASIN count + identity, no enrichment yet."""
    info = keepa_api.seller_asins((seller_id or "").strip())
    return {
        "seller_id": (seller_id or "").strip(),
        "seller_name": info.get("seller_name"),
        "asin_count": len(info.get("asins") or []),
        "has_fba": info.get("has_fba"),
        "rating": info.get("rating"),
        "rating_count": info.get("rating_count"),
        "error": info.get("error"),
    }


def build_seller_template_xlsx(seller_id: str, roi_pct: float = DEFAULT_ROI_PCT,
                                live: bool = True, on_progress=None,
                                should_cancel=None) -> tuple[bytes, dict]:
    """Returns (xlsx bytes, {seller_name, asin_count}). Raises RuntimeError /
    ValueError on a genuine failure (no seller ID, Keepa lookup failed, or the
    seller's current storefront snapshot is empty)."""
    seller_id = (seller_id or "").strip()
    if not seller_id:
        raise ValueError("A seller ID is required.")
    try:
        roi_mult = 1 + (float(roi_pct) / 100.0)
    except (TypeError, ValueError):
        roi_mult = 1 + (DEFAULT_ROI_PCT / 100.0)
    if roi_mult <= 0:
        roi_mult = 1 + (DEFAULT_ROI_PCT / 100.0)

    info = keepa_api.seller_asins(seller_id)
    if info.get("error"):
        raise RuntimeError(f"Keepa seller lookup failed: {info['error']}")
    asins = info.get("asins") or []
    if not asins:
        raise RuntimeError(
            f"Seller {seller_id!r} has no ASINs in their current Keepa storefront snapshot.")
    seller_label = info.get("seller_name") or seller_id

    _kp = (lambda d, t: on_progress(d, t, "Keepa live prices")) if on_progress else None
    keepa = keepa_api.fetch_products(asins, on_progress=_kp, should_cancel=should_cancel)

    sku_by_asin = _sku_data_by_asin(asins)
    fba_total = _fba_total_by_asin(asins)
    snap30 = _sellersnap_30d_by_asin(asins)
    report30 = sp_reports.get_units_30d() if live else {}
    last_po = _last_po_by_sku(
        [v["main_sku"] for v in sku_by_asin.values() if v.get("main_sku")])

    live_elig, live_storage, live_generic = ({}, {}, {})
    if live:
        _ep = (lambda d, t: on_progress(d, t, "eligibility check")) if on_progress else None
        live_elig, live_storage, live_generic = enrich_asins(asins, _ep, should_cancel)

    def z(v):
        fv = _f(v)
        return fv if fv is not None else 0

    def pct(v):
        return z(v) / 100

    records: list[dict] = []
    for asin in asins:
        key = asin.strip().upper()
        k = keepa.get(key, {})
        sku = sku_by_asin.get(key, {})
        has_main = bool(sku.get("main_sku"))
        pack = k.get("package_qty") or 1
        try:
            pack = int(float(pack))
        except (TypeError, ValueError):
            pack = 1
        if pack < 1:
            pack = 1
        bb = _f(k.get("current_buybox_usd"))
        bb30 = _f(k.get("buybox_30d"))
        bb90 = _f(k.get("buybox_90d"))
        bb_cur = bb if bb is not None else (bb30 if bb30 is not None else (bb90 if bb90 is not None else 0))
        storage = live_storage.get(asin)
        if storage is None:
            storage = _storage_est(k.get("height"), k.get("length"), k.get("width"))
        storage = storage if storage else 0.1
        fba_fee = z(k.get("fba_fees_usd"))
        referral = (bb_cur * 0.15) if bb_cur > 10 else (bb_cur * 0.08)
        last_cost, last_vendor = (last_po.get(sku.get("main_sku")) or (None, None))
        if has_main and last_cost is not None:
            prod_cost = round(last_cost, 4)
            vendor_label = last_vendor or ""
        else:
            target_pack = max((bb_cur - fba_fee - referral - storage) / roi_mult, 0)
            prod_cost = round(target_pack / pack, 4)
            vendor_label = f"Target @ {roi_pct:g}% ROI"
        upc_val = k.get("upc") or k.get("ean") or sku.get("upc") or ""
        records.append({
            "asin": asin, "k": k, "sku": sku, "pack": pack, "bb_cur": bb_cur,
            "storage": storage, "fba_fee": fba_fee, "referral": referral,
            "prod_cost": prod_cost, "vendor_label": vendor_label,
            "has_main": has_main, "upc": upc_val,
            "approved": live_elig.get(asin),
            "monthly": z(k.get("monthly_sold_quantity")),
        })
    records.sort(key=lambda d: d["monthly"], reverse=True)

    wb = load_workbook(_TEMPLATE)
    ws = wb["Analytics"]
    ws.insert_cols(2, 3)     # Offer Vendor, SKU, FBA SKU  (after UPC)
    ws.insert_cols(12, 5)    # Available qty, OR33, On order, FBA total, 30d sales
    ws.insert_cols(19, 2)    # Last cost, Vendor
    ws.cell(row=1, column=COL["qty/case"]).value = "qty/case"
    for name in _NEW_HEADER_COLS:
        c = COL[name]
        hc = ws.cell(row=1, column=c, value=name)
        hc.fill, hc.font, hc.alignment = _HDR_GREY, _ROBOTO, _HDR_ALIGN
        ws.column_dimensions[get_column_letter(c)].width = 14
    ws.conditional_formatting = ConditionalFormattingList()
    ws.freeze_panes = f"{L('Approved')}2"

    r = 2
    for d in records:
        k, sku, asin, pack = d["k"], d["sku"], d["asin"], d["pack"]
        upc = d["upc"]
        rowd = {
            "UPC/EAN": int(upc) if str(upc).isdigit() else (upc or None),
            "SKU": sku.get("main_sku"), "FBA SKU": sku.get("fba_sku"),
            "ASIN": asin,
            "Link": f'=HYPERLINK("https://www.amazon.com/dp/"&{L("ASIN")}{r})',
            "Offer Vendor": seller_label,
            "Approved": d["approved"],
            "Item ID": None, "Vendor Title": None,
            "Amazon title": k.get("title"), "qty/case": None,
            "Available qty": None,
            "OR33": sku.get("or33"), "On order": sku.get("on_order"),
            "FBA total": fba_total.get(asin.strip().upper()),
            "30d sales": (report30.get(asin.strip().upper())
                          if report30.get(asin.strip().upper()) is not None
                          else snap30.get(asin.strip().upper())),
            "Estimated Monthly Units Sold": d["monthly"], "amz pack": pack,
            "Last cost": d["prod_cost"] if d["has_main"] else None,
            "Vendor": d["vendor_label"],
            "Product Cost": d["prod_cost"],
            "Product cost * amz pack": f'={L("Product Cost")}{r}*{L("amz pack")}{r}',
            "Buy Box Price current": round(d["bb_cur"], 2),
            "BB Price 30d": z(k.get("buybox_30d")), "BB Price 90d": z(k.get("buybox_90d")),
            "Net Profit": (f'={L("Buy Box Price current")}{r}-{L("Product cost * amz pack")}{r}'
                           f'-{L("FBA Fee")}{r}-{L("Referral Fee")}{r}-{L("Storage Fee")}{r}-{L("Prep & Out fee")}{r}'),
            "Net Margin": f'=IFERROR({L("Net Profit")}{r}/{L("Buy Box Price current")}{r},0)',
            "ROI": f'=IFERROR({L("Net Profit")}{r}/{L("Product cost * amz pack")}{r},0)',
            "FBA Fee": d["fba_fee"],
            "Referral Fee": (f'=IF({L("Buy Box Price current")}{r}>10,'
                             f'{L("Buy Box Price current")}{r}*0.15,{L("Buy Box Price current")}{r}*0.08)'),
            "Storage Fee": round(d["storage"], 2), "Prep & Out fee": 0,
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
        for c, (header, nf, fill_key, bold, center) in enumerate(TEMPLATE_COLS, start=1):
            cell = ws.cell(row=r, column=c, value=rowd.get(header))
            if nf:
                cell.number_format = nf
            if fill_key:
                cell.fill = _FILLS[fill_key]
            if bold:
                cell.font = _BOLD
            elif header == "Link":
                cell.font = _HYPERLINK_FONT
            if center:
                cell.alignment = _CENTER
        r += 1

    last = r - 1
    if last >= 2:
        dxf = DifferentialStyle(
            fill=PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid"),
            font=Font(color="9C0006"))
        ws.conditional_formatting.add(
            f"{L('ASIN')}2:{L('ASIN')}{last}", Rule(type="duplicateValues", dxf=dxf))
        for col, operator, formula in (
            (L("Net Profit"), "lessThan", "0"),
            (L("Net Margin"), "lessThan", "0.1"),
            (L("ROI"), "lessThan", "0.15"),
            (L("Rank (current)"), "greaterThan", "100000"),
        ):
            ws.conditional_formatting.add(
                f"{col}2:{col}{last}",
                Rule(type="cellIs", operator=operator, formula=[formula], dxf=dxf),
            )
    ws.auto_filter.ref = f"A1:{L(TEMPLATE_COLS[-1][0])}{max(last, 1)}"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue(), {"seller_name": seller_label, "asin_count": len(asins)}


def seller_filename(seller_id: str, seller_name: str | None = None) -> str:
    """`<Seller> <date> <NN>.xlsx`, NN incrementing per seller per day."""
    date = dt.date.today().isoformat()
    base = re.sub(r"[^A-Za-z0-9 &._-]", "", (seller_name or seller_id or "Seller").strip()) or "seller"
    n = database.next_seq(f"seller-export:{seller_id}:{date}")
    return f"{base} {date} {n:02d}.xlsx"
