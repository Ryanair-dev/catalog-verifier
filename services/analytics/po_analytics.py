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
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.styles import Font
from openpyxl.utils import get_column_letter

from services import azure_sql, database, sc_reference
from services import keepa as keepa_api
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


# Bundled template (assets/po_analytics_template.xlsx) — built once from the
# user's own ground-truth reference file (49932 Blazy Susan.xlsx, 2026-10-07)
# by copying its real per-column fills/fonts/number-formats/conditional-
# formatting rules (captured by header NAME, not position) onto this module's
# own ANALYTICS_COLS order. Row 1 = real styled headers; row 2 is a blank
# "style swatch" row whose per-column cell style is copied onto every actual
# data row at build time (see _copy_row_style below) — this is what makes the
# export's colours/number-formats/conditional-formatting pixel-identical to
# the reference file instead of an approximation.
_TEMPLATE = Path(__file__).resolve().parent.parent.parent / "assets" / "po_analytics_template.xlsx"
_STYLE_ROW = 2


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


# PurchaseOrderStatusesDto.Status enum (confirmed live via the SellerCloud swagger
# spec, 2026-10-08): 0=Saved, 1=Ordered, 2=Pending, 3=Received, 4=Cancelled,
# 5=Completed, -1=Select.
_PO_STATUS_NAMES = {0: "Saved", 1: "Ordered", 2: "Pending", 3: "Received",
                    4: "Cancelled", 5: "Completed", -1: "Select"}
_PO_STATUS_RECEIVED = 3


def _resolve_received_pos(client, po_id: int, visited: set[int], skipped: list[dict]) -> list[dict]:
    """The real Received PO(s) reachable from `po_id`: itself if it's already
    Received, else its split(s) (sellercloud.Purchase.SplittedFromPOId -- the
    real parent-PO link, confirmed live 2026-10-08; it's NOT exposed anywhere
    in the REST API's own PO payload), recursed the same way in case a split
    was itself later split again. `visited` guards against revisiting the same
    PO twice (a cycle, or two requested numbers converging on one split) and
    makes repeat lookups free. Every PO actually skipped along the way
    (including an intermediate one that correctly led somewhere, not just a
    final dead end) is recorded in `skipped` for visibility."""
    if po_id in visited:
        return []
    visited.add(po_id)
    po = client.get_purchase_order(po_id)
    status = (po.get("Statuses") or {}).get("Status")
    if status == _PO_STATUS_RECEIVED:
        return [po]
    status_name = _PO_STATUS_NAMES.get(status, str(status))
    splits = azure_sql.find_split_pos(po_id)
    if not splits:
        skipped.append({"po": po_id, "status": status_name})
        return []
    skipped.append({"po": po_id, "status": status_name,
                    "note": f"following its split PO(s): {', '.join(str(s) for s in splits)}"})
    out: list[dict] = []
    for sp in splits:
        out.extend(_resolve_received_pos(client, sp, visited, skipped))
    return out


def gather(po_numbers: list[int], company: str = "Ford Medical",
           require_received: bool = True) -> dict:
    """Fast phase: pull the PO(s) from SellerCloud, resolve every Main SKU to
    its FBA/FBM children via the local catalog snapshot, and build the base
    (un-enriched) row list + the raw PO-sheet rows. No Keepa/SP-API calls.

    `require_received=True` (default, the manual UI tool): only a RECEIVED
    PO's items are ever used (per user 2026-10-08: a split PO's cancelled
    parent must never be used, and any other split that hasn't itself been
    received yet must be left out). Entering a cancelled/not-yet-received PO
    number automatically follows its real split(s) instead of just erroring —
    see `_resolve_received_pos`. Every PO actually skipped along the way (the
    cancelled parent, and any split that also didn't pan out) is reported back
    in `skipped_pos` so the caller/UI can show exactly what was left out and
    why, instead of a silent row-count mismatch.

    `require_received=False` (the automated day-before-ETA pipeline, added
    2026-10-09): the PO is used exactly as given, in whatever SellerCloud
    status it's currently in (Ordered/Pending/Received) — the PO number comes
    straight from the Inbound Shipments Monday board's own `link` field, which
    a human has already pointed at the real current PO (including any split),
    so no further split-resolution is needed here. A Cancelled PO is still
    skipped (nothing to analyze). Per-line `po_qty` switches from the received
    quantity to the ORDERED quantity (`TotalCases * QtyPerCase`) in this mode,
    since the whole point of running this a day before arrival is that nothing
    has been received yet — see the `po_qty` computation below."""
    client = get_sellercloud_client()
    all_items: list[dict] = []
    vendor_ids: set[int] = set()
    skipped_pos: list[dict] = []
    pos: list[dict] = []

    if require_received:
        visited: set[int] = set()
        for num in po_numbers:
            pos.extend(_resolve_received_pos(client, num, visited, skipped_pos))
        if not pos:
            reasons = ", ".join(
                f"PO {s['po']} ({s['status']}" + (f" — {s['note']}" if s.get("note") else "") + ")"
                for s in skipped_pos
            )
            raise RuntimeError(
                f"None of the requested PO(s) (or their splits) are Received, so "
                f"there's nothing to analyze: {reasons}."
            )
    else:
        for num in po_numbers:
            po = client.get_purchase_order(num)
            status = (po.get("Statuses") or {}).get("Status")
            if status == 4:  # Cancelled
                skipped_pos.append({"po": num, "status": "Cancelled"})
                continue
            pos.append(po)
        if not pos:
            raise RuntimeError(
                f"PO(s) {po_numbers} are Cancelled — nothing to analyze."
            )

    for po in pos:
        items = po.get("Items") or []
        all_items.extend(items)
        vid = (po.get("Purchase") or {}).get("VendorId")
        if vid:
            vendor_ids.add(vid)

    brand_label = _resolve_brand_label(client, all_items, vendor_ids, company)

    # LIVE Azure only (2026-10-07, per explicit user instruction) — no local
    # xlsx-snapshot fallback. The snapshot was a repeated source of real bugs:
    # it sat 56 days stale with nobody noticing (ANH950685/ANH140702's real FBA
    # children were both invisible to it while genuinely active in
    # SellerCloud), and its source file is persistently locked (open in Excel)
    # so it often can't even refresh on demand. A full Azure pull costs ~165s
    # uncached, then is cached in-process for 30 min and kept warm by
    # azure_sql's own hourly scheduler — worth paying once per on-demand PO
    # Analytics run for data that's actually current. A real Azure outage now
    # surfaces as a clear, loud error instead of silently degrading to stale data.
    if not azure_sql.is_configured():
        raise RuntimeError(
            "Azure SQL isn't configured — PO Analytics requires the live catalog "
            "(no local-snapshot fallback). Check AZURE_* env vars."
        )
    idx = azure_sql.catalog_index(company)
    base_rows: list[dict] = []
    for it in all_items:
        main_sku = str(it.get("ProductID") or "").strip()
        if not main_sku:
            continue
        if require_received:
            po_qty = _f(it.get("QtyReceived")) or 0
        else:
            # Ordered mode: nothing has arrived yet, so the received qty is
            # genuinely 0 — use what was actually ordered instead.
            total_cases = _f(it.get("TotalCases")) or 0
            qty_per_case = _f(it.get("QtyPerCase")) or 0
            po_qty = total_cases * qty_per_case
        product_cost = _f(it.get("AdjustedPrice"))
        vendor_title = it.get("ProductName") or it.get("ProductNameFromProductTable") or ""
        upc = str(it.get("UPC") or "").strip()
        qty_per_case = _f(it.get("QtyPerCase"))

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
                "qty_per_case": qty_per_case,
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
                "qty_per_case": qty_per_case,
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

    # The PO(s) actually used -- may differ from the originally-requested
    # `po_numbers` when one of those was cancelled and auto-resolved to a real
    # split (see _resolve_received_pos). The filename should reflect what was
    # actually analyzed, not what was typed in.
    resolved_po_numbers = [p["Purchase"]["POId"] for p in pos if p.get("Purchase", {}).get("POId")]

    return {
        "po_numbers": po_numbers, "resolved_po_numbers": resolved_po_numbers,
        "brand_label": brand_label,
        "rows": base_rows, "po_rows": po_rows,
        "skipped_pos": skipped_pos,
        "require_received": require_received,
    }


def _fetch_dimensions(asins: list[str], on_progress=None, should_cancel=None) -> dict[str, dict]:
    """{asin: {length_in, width_in, height_in, weight_lb}} via a live SP-API
    getCatalogItem call per ASIN -- the "Jim" sheet's dimension columns. A
    small, separate pool (doesn't touch enrich_asins' shared signature, which
    the Price Desk "Offer Analytics" export also depends on) -- enrich_asins
    DOES compute these same dims internally for any ASIN it isn't already
    storage-cached for, but discards them immediately after turning them into
    a single storage-fee number, and the shared storage_fee_cache table only
    reliably carries dims for ASINs that happened to also go through the
    separate Tools-panel storage-fee job, not every ASIN PO Analytics needs --
    so a dedicated fetch here is the simplest way to guarantee every ASIN gets
    real dimensions. Best-effort: a failed/missing ASIN just gets all-None
    dims, never raises."""
    import requests
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from services.spapi.client import get_catalog_api
    from services.spapi.config import sp_api_configured
    from services.storage_fees import extract_dimensions

    out: dict[str, dict] = {}
    uniq = sorted({a.strip().upper() for a in asins if a})
    if not uniq or not sp_api_configured():
        if on_progress:
            on_progress(0, 0)
        return out
    capi = get_catalog_api()

    def one(asin: str):
        try:
            raw = capi.get_by_asin(asin, included_data="attributes")
            dims = extract_dimensions(raw.get("attributes") or {})
        except Exception:  # noqa: BLE001 -- 404/DOG, throttled, or any other failure
            dims = {"length_in": None, "width_in": None, "height_in": None, "weight_lb": None}
        return asin, dims

    done = 0
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = [ex.submit(one, a) for a in uniq]
        for f in as_completed(futs):
            if should_cancel and should_cancel():
                for fut in futs:
                    fut.cancel()
                raise ExportCancelled()
            a, dims = f.result()
            out[a] = dims
            done += 1
            if on_progress:
                on_progress(done, len(uniq))
    return out


JIM_COLS = ["Main SKU", "FBA SKU", "ASIN", "Description", "PO qty", "Qty/Case", "amz pack",
            "Length (in)", "Width (in)", "Height (in)", "Weight (lb)"]


def enrich_and_build(gathered: dict, on_progress=None, should_cancel=None) -> bytes:
    """Slow phase: live Keepa + SP-API eligibility/storage + 30d Sales & Traffic
    for every distinct ASIN, then assemble the workbook (Analytics + PO + Jim)."""
    rows = gathered["rows"]
    asins = sorted({r["asin"] for r in rows if r.get("asin")})

    _kp = (lambda d, t: on_progress(d, t, "Keepa live prices")) if on_progress else None
    keepa = keepa_api.fetch_products(asins, on_progress=_kp, should_cancel=should_cancel)

    _ep = (lambda d, t: on_progress(d, t, "eligibility check")) if on_progress else None
    live_elig, live_storage, live_generic = enrich_asins(asins, _ep, should_cancel, check_generic=True)

    _dp = (lambda d, t: on_progress(d, t, "fetching dimensions")) if on_progress else None
    dims_by_asin = _fetch_dimensions(asins, _dp, should_cancel)

    fba_total = _fba_total_by_asin(asins)
    report30 = sp_reports.get_units_30d()

    def z(v):
        f = _f(v)
        return f if f is not None else 0

    pct = lambda v: z(v) / 100

    wb = load_workbook(_TEMPLATE)
    ws = wb["Analytics"]      # headers (row 1) + a per-column style swatch (row 2) already in place
    if not gathered.get("require_received", True):
        # Automated day-before-ETA report: "PO qty" means ordered qty here,
        # not received qty (nothing has arrived yet) — relabel so nobody
        # confuses it with a post-arrival manual run's "PO qty" column.
        ws.cell(row=1, column=COL["PO qty"], value="PO qty (Ordered)")

    def _copy_row_style(dest_row: int) -> None:
        if dest_row == _STYLE_ROW:
            return
        for c in range(1, len(ANALYTICS_COLS) + 1):
            ws.cell(row=dest_row, column=c)._style = ws.cell(row=_STYLE_ROW, column=c)._style

    kept_rows: list[dict] = []   # reused below to build the "Jim" sheet off the SAME set
    r = 2
    for row in rows:
        asin = row.get("asin")
        # DOG (deactivated listing) / GENERIC ASINs are excluded entirely, per
        # the user (2026-10-07, example: B07C72FFSZ, a confirmed deactivated
        # DOG listing) — not just flagged, since a dead/generic ASIN has no
        # real profitability to analyze.
        if asin and (live_elig.get(asin) == "DOG" or live_generic.get(asin) == "GENERIC"):
            continue
        kept_rows.append(row)
        _copy_row_style(r)
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
            ws.cell(row=r, column=c, value=rowd.get(name))
        r += 1

    last = r - 1
    ws.auto_filter.ref = f"A1:{L(ANALYTICS_COLS[-1])}{max(last, 1)}"

    ws2 = wb["PO"]            # header row (plain, unstyled — matches the reference) already in place

    def _copy_po_row_style(dest_row: int) -> None:
        if dest_row == _STYLE_ROW:
            return
        for c in range(1, len(PO_SHEET_COLS) + 1):
            ws2.cell(row=dest_row, column=c)._style = ws2.cell(row=_STYLE_ROW, column=c)._style

    r2 = 2
    for pr in gathered["po_rows"]:
        _copy_po_row_style(r2)
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
            ws2.cell(row=r2, column=c, value=rowd2.get(name))
        r2 += 1

    # "Jim" sheet — a lean subset of the same kept rows (DOG/Generic already
    # excluded above) plus full SP-API package dimensions, per explicit request.
    # Not in the bundled template (no pre-built styling to reuse) -- plain
    # bold headers are enough since no one asked for pixel-matched formatting
    # on this one.
    ws3 = wb.create_sheet("Jim")
    ws3.append(JIM_COLS)
    for c in range(1, len(JIM_COLS) + 1):
        ws3.cell(row=1, column=c).font = Font(bold=True)
    for row in kept_rows:
        asin = row.get("asin")
        d = dims_by_asin.get((asin or "").strip().upper(), {}) if asin else {}
        ws3.append([
            row["sku"], row.get("fba_sku"), asin, row.get("vendor_title"),
            row.get("po_qty"), row.get("qty_per_case"), row.get("pack_qty") or 1,
            d.get("length_in"), d.get("width_in"), d.get("height_in"), d.get("weight_lb"),
        ])
    ws3.auto_filter.ref = f"A1:{get_column_letter(len(JIM_COLS))}{max(len(kept_rows) + 1, 1)}"
    for c, w in zip(range(1, len(JIM_COLS) + 1), (16, 18, 12, 40, 10, 10, 10, 11, 11, 11, 11)):
        ws3.column_dimensions[get_column_letter(c)].width = w

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def po_analytics_filename(po_numbers: list[int], brand_label: str) -> str:
    """'<PO#>[ <PO#>...] <Brand or Vendor>.xlsx' per the house naming convention."""
    nums = " ".join(str(n) for n in po_numbers)
    safe_brand = re.sub(r"[^A-Za-z0-9 &._-]", "", (brand_label or "").strip()) or "PO Analytics"
    return f"{nums} {safe_brand}.xlsx"
