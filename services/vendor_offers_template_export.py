"""
Price Desk → "Offer Analytics" export in Ford Medical's template format.

Reproduces the `1 offer analytics template.xlsx` Analytics sheet (35 columns,
Roboto 12, frozen at D2, the exact header color blocks) and fills it by joining:

  vendor offers (local)                 → UPC, Item ID, Vendor Title, qty, Product Cost
  analytics.keepa_barcodes (Azure)      → UPC → ASIN(s)   (one row per UPC×ASIN)
  analytics.keepa_ford_products (Azure) → Amazon title, Buy Box (current/30/90d),
                                          FBA fee, ranks, BB seller + share, Amazon %,
                                          FBA/FBM seller counts, brand, category,
                                          ratings, dims, monthly units, parent ASIN
  health_check_results (local)          → Approved (eligibility), where already checked

Computed: Referral Fee = 15% of Buy Box; Storage Fee = volume estimate (Keepa has
no weight, so this assumes standard tier); Net Profit / Margin / ROI. Prep & Out is
a caller-supplied per-unit constant. Rows are expanded per UPC×ASIN and carry a
cost column per vendor plus a "Cheapest Vendor" column (added, beyond the template).
"""
from __future__ import annotations

import datetime as dt
import io
import logging
import re
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.formatting.formatting import ConditionalFormattingList
from openpyxl.formatting.rule import Rule
from openpyxl.styles import Alignment, Color, Font, PatternFill
from openpyxl.styles.differential import DifferentialStyle
from openpyxl.utils import get_column_letter

from services import database
from services import keepa as keepa_api
from services import vendor_offers as vo
from services.cancellation import JobCancelled
from services.spapi import reports as sp_reports

log = logging.getLogger(__name__)

_TEMPLATE = Path(__file__).resolve().parent.parent / "assets" / "offer_analytics_template.xlsx"
AMAZON_SELLER_ID = "ATVPDKIKX0DER"
REFERRAL_RATE = 0.15                     # example: Referral = 0.15 × Buy Box
_ACCT = '_("$"* #,##0.00_);_("$"* \\(#,##0.00\\);_("$"* "-"??_);_(@_)'
# Simpler ("plain") currency format — distinct from _ACCT's accounting style
# (no star-padded $ / no "-" for zero). Confirmed against the user's own
# reference file (`Victory HL Analytics 7.13.2026.xlsx`, 2026-09-30): BB
# Price 30d/90d and FBA Fee use THIS format there, not the accounting one.
_CUR = r'"$"#,##0.00_);\("$"#,##0.00\)'

# Data-cell fills that match the manual analytics exactly (theme colors resolve
# against the bundled template's palette, so they render identically).
_FILL_BLUE = PatternFill("solid", fgColor=Color(theme=8, tint=0.8))   # UPC/ASIN/Link
_FILL_GMED = PatternFill("solid", fgColor=Color(theme=9, tint=0.4))   # Buy Box current
_FILL_GLT  = PatternFill("solid", fgColor=Color(theme=9, tint=0.8))   # BB 30d/90d
_FILLS = {"blue": _FILL_BLUE, "gmed": _FILL_GMED, "glt": _FILL_GLT}

# Full column layout: (header, data number_format, data fill key, data bold, center).
# Matched to `Atlantic analytics 6.8.2026.xlsx` PLUS 8 inserted SellerCloud columns:
# SKU/FBA SKU (after UPC); OR33/On order/FBA total/30d sales (after qty/case);
# Last cost/Vendor (after amz pack).
TEMPLATE_COLS = [
    ("UPC/EAN", "0", "blue", False, False),
    ("Offer Vendor", None, None, False, False),
    ("SKU", None, None, False, False), ("FBA SKU", None, None, False, False),
    ("ASIN", None, "blue", False, False), ("Link", None, "blue", False, False),
    ("Approved", None, None, False, False), ("Item ID", None, None, False, False),
    ("Vendor Title", None, None, False, False), ("Amazon title", None, None, False, False),
    ("qty/case", None, None, False, True), ("Available qty", None, None, False, True),
    ("OR33", None, None, False, True), ("On order", None, None, False, True),
    ("FBA total", None, None, False, True), ("30d sales", None, None, False, True),
    ("Estimated Monthly Units Sold", None, None, True, True), ("amz pack", None, None, True, True),
    ("Last cost", _ACCT, None, False, True), ("Vendor", None, None, False, False),
    ("Product Cost", _ACCT, None, False, False), ("Product cost * amz pack", _ACCT, None, False, False),
    ("Buy Box Price current", _ACCT, "gmed", True, True),
    ("BB Price 30d", _CUR, "glt", False, True), ("BB Price 90d", _CUR, "glt", False, True),
    ("Net Profit", "0.00", None, False, False), ("Net Margin", "0%", None, False, False),
    ("ROI", "0%", None, False, False),
    ("FBA Fee", _CUR, None, False, False), ("Referral Fee", _ACCT, None, False, False),
    ("Storage Fee", _ACCT, None, False, False), ("Prep & Out fee", None, None, False, False),
    ("Rank (current)", None, None, False, False), ("Rank 30d", None, None, False, False),
    ("BB seller", None, None, False, False), ("BB share 30d", "0%", None, False, False),
    ("BB share 90d", "0%", None, False, False), ("Amazon 30d %", "0%", None, False, False),
    ("Amazon 90d %", "0%", None, False, False),
    ("FBM sellers", None, None, False, True), ("FBA sellers", None, None, False, True),
    ("Brand", None, None, False, False), ("Category", None, None, False, False),
    ("Total Ratings Count", None, None, False, False), ("Variation Parent", None, None, False, False),
]
# 1-based column index by header name → lets formulas reference cells by name.
COL = {h: i + 1 for i, (h, *_ ) in enumerate(TEMPLATE_COLS)}
def L(name):
    return get_column_letter(COL[name])

# columns that are NEW (not in the loaded template) and need header styling
_NEW_HEADER_COLS = ("Offer Vendor", "SKU", "FBA SKU", "Available qty", "OR33", "On order", "FBA total",
                    "30d sales", "Last cost", "Vendor")

_ROBOTO = Font(name="Roboto", size=12)                 # header font (matches template)
_BOLD = Font(name="Calibri", size=11, bold=True)       # bold data (matches manual)
_CENTER = Alignment(horizontal="center")
# The Link column's =HYPERLINK() formula doesn't get Excel's automatic blue/
# underlined "Hyperlink" style just from being a formula -- the reference
# file has it applied explicitly (theme10 = the "Hyperlink" theme colour
# slot, single underline). Confirmed against the real file, 2026-09-30.
_HYPERLINK_FONT = Font(name="Calibri", size=11, color=Color(theme=10, tint=0.0), underline="single")
_HDR_GREY = PatternFill("solid", fgColor="FFE9E9E9")
_HDR_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)


class ExportCancelled(Exception):
    """Raised when a running export is cancelled by the user."""


def _f(v):
    """Decimal/None-safe float."""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _bkey(s) -> str | None:
    """Barcode match key: digits, leading zeros stripped (UPC-A ↔ EAN-13 agree)."""
    if s is None:
        return None
    d = re.sub(r"\D", "", str(s)).lstrip("0")
    return d or None


def _azure_rows(sql: str) -> list[tuple]:
    from services import azure_sql
    conn = azure_sql._connect()
    try:
        cur = conn.cursor()
        cur.execute(sql)
        return cur.fetchall(), [d[0] for d in cur.description]
    finally:
        conn.close()


# ── data gathering ─────────────────────────────────────────────────────────

def _vendor_offers_by_upc(upcs: set[str] | None):
    """{upc: {vendor: {cost, qty, item_id, title, avail}}} — best (cheapest)
    offer per (upc, vendor). Also returns the ordered vendor list.

    Reads from vo_vendor_best_offer (the SQL view every other "cheapest
    offer per vendor" consumer in this app already uses -- see
    services/vendor_offers.py's own definition) rather than hand-rolling a
    second "best offer" resolver here. Fixed 2026-10-27: the old hand-rolled
    version only kept a row on STRICT cost improvement (`c < prev["cost"]`)
    with no tie-break at all -- on a cost TIE between two offer rows for the
    same (upc, vendor) (e.g. the same UPC re-synced same-day with an
    unchanged price, which is exactly what happened right after the
    2026-10-27 Cencora CS/PK parser fix added new offer rows alongside
    older ones at an identical cost), it silently kept whichever row the
    unordered SQL query happened to return first -- which could easily be a
    stale row with avail_qty=NULL, even when a same-cost row with real,
    current stock also existed. That directly broke `only_available=True`
    for every UPC with this shape: `win.get("avail")` resolved to the stale
    NULL instead of the real quantity, confirmed live against UPC
    012044010693/Cencora (avail=72 as of today, avail=NULL from an older
    row, both at $4.45 -- the auto-alert's 70-minute Cencora run came back
    with ZERO qualifying products specifically because of this). The view's
    own tie-break (cost, then avail_qty DESC, then qty_per_case ASC, then
    offer_id ASC) is correct and already battle-tested elsewhere; reusing
    it here instead of a second, buggier implementation fixes this for
    every vendor, not just Cencora."""
    with database._connect() as conn:
        vendors = [r[0] for r in conn.execute(
            "SELECT DISTINCT vendor_name FROM vo_vendor_best_offer ORDER BY vendor_name")]
        rows = conn.execute("""
            SELECT vendor_name, upc, vendor_item_id, cost, qty_per_case, description, avail_qty
            FROM vo_vendor_best_offer
        """).fetchall()
    by_upc: dict = {}
    for vname, upc, iid, cost, qty, desc, avail in rows:
        if upcs is not None and upc not in upcs:
            continue
        c = _f(cost)
        if c is None:
            continue
        by_upc.setdefault(upc, {})[vname] = {
            "cost": c, "qty": qty, "item_id": iid, "title": desc, "avail": _f(avail),
        }
    return by_upc, vendors


def _assigned_asins_by_upc() -> dict:
    """{upc: [asins]} from our own Pair-Library assignments (vo_asin_by_upc) —
    unioned with the Keepa barcodes so 'Has ASIN' items always carry their ASIN."""
    out: dict = {}
    with database._connect() as conn:
        try:
            for upc, asin in conn.execute("SELECT upc, asin FROM vo_asin_by_upc"):
                if upc and asin:
                    out.setdefault(upc, [])
                    if asin not in out[upc]:
                        out[upc].append(asin)
        except Exception:
            pass
    return out


def _amz_pack_by_asin() -> dict:
    with database._connect() as conn:
        return {r[0]: r[1] for r in conn.execute(
            "SELECT asin, amazon_pack_size FROM vo_asin_listings "
            "WHERE amazon_pack_size IS NOT NULL")}


def _eligibility_by_asin() -> dict:
    """Latest eligibility per ASIN from the weekly health check (if any)."""
    with database._connect() as conn:
        try:
            return {r[0]: r[1] for r in conn.execute(
                "SELECT asin, eligibility FROM health_check_results "
                "WHERE eligibility IS NOT NULL ORDER BY checked_at")}
        except Exception:
            return {}


def _barcode_to_asins(keys: set[str]) -> dict:
    """{barcode_key: [asins]} restricted to the barcodes we care about."""
    rows, _ = _azure_rows("SELECT asin, barcode FROM analytics.keepa_barcodes")
    out: dict = {}
    for asin, barcode in rows:
        k = _bkey(barcode)
        if k and k in keys and asin:
            out.setdefault(k, [])
            if asin not in out[k]:
                out[k].append(asin)
    return out


_KEEPA_COLS = ("asin,title,monthly_sold_quantity,current_buybox_usd,buybox_30d,buybox_90d,"
               "fba_fees_usd,sales_rank_current,sales_rank_30d,buybox_seller_name,"
               "top_seller_30d_percentage,top_seller_90d_percentage,amazon_bb_30d_percentage,"
               "amazon_bb_90d_percentage,offer_count_fbm,offer_count_fba,brand,category,"
               "reviews_rating_count,parent_asin,variation_asin,height,length,width,url")


def _keepa_by_asin(asins: list[str]) -> dict:
    out: dict = {}
    clean = sorted({re.sub(r"[^A-Za-z0-9]", "", a) for a in asins if a})
    for i in range(0, len(clean), 900):
        chunk = clean[i:i + 900]
        inlist = ",".join(f"'{a}'" for a in chunk)
        rows, cols = _azure_rows(
            f"SELECT {_KEEPA_COLS} FROM analytics.keepa_ford_products WHERE asin IN ({inlist})")
        for r in rows:
            d = dict(zip(cols, r))
            out[d["asin"]] = d
    return out


def _chunks(items, n=600):
    items = sorted({x for x in items if x})
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _inlist(vals):
    return ",".join("'" + str(v).replace("'", "") + "'" for v in vals)


def _sku_data_by_asin(asins: list[str], asin_to_upc: dict[str, str] | None = None) -> dict:
    """{asin: {main_sku, fba_sku, on_order, or33, upc}} from the live SellerCloud
    view (Ford Medical).

    Main SKU is looked up by UPC, not ASIN -- mirroring the user's own "SC
    data.xlsx" reference file (its "Sku Data extended" tab keys the parent/
    main SKU off UPC; its "FBA"/"FBM" Power Queries separately group by ASIN).
    Fixed 2026-10-27, per user report that SKU/FBA SKU (among other columns)
    were coming back inaccurate: the true parent/main SellerCloud SKU is
    often never itself listed on Amazon -- only its FBA/FBM shadow children
    carry a real ASIN -- so filtering this view by ASIN AND FulfilledBy=
    'Merchant' could silently return nothing, or the wrong row, whenever the
    real main SKU's own ASIN column was blank or stale. UPC doesn't have
    that problem; it's the stable identifier of the underlying product.
    FBA SKU keeps the ASIN-based lookup (FulfilledBy='Amazon'), since that
    IS the SKU actually listed under that exact ASIN -- that half was never
    the bug.

    `asin_to_upc` is the caller's own UPC -> ASIN(s) assignment (an ASIN has
    exactly one UPC; a UPC can have several ASINs, so this is the per-ASIN
    reverse lookup) -- Vendor Offers' export always has this (it starts from
    a UPC). Seller Analytics (`services/seller_analytics.py`) does NOT --
    it starts from a bare ASIN list with no known UPC yet, and actually uses
    this function's own `upc` return value to discover one. For any ASIN
    with no entry in `asin_to_upc`, this falls back to the original
    ASIN-keyed main-SKU query for just that ASIN, so that caller doesn't
    regress -- it was never the half of this that was wrong."""
    asin_to_upc = asin_to_upc or {}
    clean_asins = [a.strip().upper() for a in asins if a]
    upcs = sorted({u for u in asin_to_upc.values() if u})
    asins_without_upc = [a for a in clean_asins if not asin_to_upc.get(a)]

    main_by_upc: dict = {}
    for chunk in _chunks(upcs):
        try:
            rows, cols = _azure_rows(
                "SELECT UPC, ProductID, ShadowOf, FulfilledBy, Status, "
                "OnOrder, InventoryPhysicalQty, LastPurchasedOn "
                "FROM analytics.sku_data_extended_view "
                f"WHERE CompanyName LIKE 'Ford%' AND UPC IN ({_inlist(chunk)})")
        except Exception as exc:  # noqa: BLE001
            log.warning("[template_export] sku view (main by UPC) query failed: %s", str(exc)[:120])
            continue
        by: dict = {}
        for r in rows:
            d = dict(zip(cols, r))
            by.setdefault(d["UPC"], []).append(d)
        for upc, rs in by.items():
            merch = [r for r in rs if r["FulfilledBy"] == "Merchant"]
            mkey = lambda r: (r["Status"] == "Active", not (r["ShadowOf"] or ""),
                              r["LastPurchasedOn"] or dt.datetime.min)
            main = max(merch, key=mkey) if merch else (max(rs, key=mkey) if rs else None)
            # main SKU = the base/parent: prefer ShadowOf; else strip the shadow
            # suffixes (-FBA/-FBM/_QY<n>) off the ProductID → the base SKU.
            base = ((main or {}).get("ShadowOf") or (main or {}).get("ProductID")) if main else None
            main_sku = re.sub(r"(_QY\d+|-FB[AM])+$", "", base) if base else None
            main_by_upc[upc] = {
                "main_sku": main_sku,
                "on_order": (main or {}).get("OnOrder"),
                "or33": (main or {}).get("InventoryPhysicalQty"),
            }

    fba_by_asin: dict = {}
    for chunk in _chunks(clean_asins):
        try:
            rows, cols = _azure_rows(
                "SELECT LTRIM(RTRIM(ASIN)) asin, ProductID, UPC "
                "FROM analytics.sku_data_extended_view "
                f"WHERE CompanyName LIKE 'Ford%' AND FulfilledBy = 'Amazon' "
                f"AND LTRIM(RTRIM(ASIN)) IN ({_inlist(chunk)})")
        except Exception as exc:  # noqa: BLE001
            log.warning("[template_export] sku view (fba by ASIN) query failed: %s", str(exc)[:120])
            continue
        for r in rows:
            d = dict(zip(cols, r))
            fba_by_asin.setdefault(d["asin"], d)   # first Amazon row wins, same as before

    # Fallback for callers with no known UPC per ASIN (Seller Analytics) --
    # the original ASIN-keyed main-SKU query, scoped to just those ASINs, so
    # that caller keeps the exact behavior it had before this fix.
    main_by_asin_fallback: dict = {}
    for chunk in _chunks(asins_without_upc):
        try:
            rows, cols = _azure_rows(
                "SELECT LTRIM(RTRIM(ASIN)) asin, ProductID, ShadowOf, FulfilledBy, Status, "
                "OnOrder, InventoryPhysicalQty, LastPurchasedOn "
                "FROM analytics.sku_data_extended_view "
                f"WHERE CompanyName LIKE 'Ford%' AND LTRIM(RTRIM(ASIN)) IN ({_inlist(chunk)}) "
                "AND FulfilledBy = 'Merchant'")
        except Exception as exc:  # noqa: BLE001
            log.warning("[template_export] sku view (main by ASIN fallback) query failed: %s", str(exc)[:120])
            continue
        by: dict = {}
        for r in rows:
            d = dict(zip(cols, r))
            by.setdefault(d["asin"], []).append(d)
        for asin, rs in by.items():
            mkey = lambda r: (r["Status"] == "Active", not (r["ShadowOf"] or ""),
                              r["LastPurchasedOn"] or dt.datetime.min)
            main = max(rs, key=mkey)
            base = main.get("ShadowOf") or main.get("ProductID")
            main_sku = re.sub(r"(_QY\d+|-FB[AM])+$", "", base) if base else None
            main_by_asin_fallback[asin] = {
                "main_sku": main_sku,
                "on_order": main.get("OnOrder"),
                "or33": main.get("InventoryPhysicalQty"),
            }

    out: dict = {}
    for asin in clean_asins:
        upc = asin_to_upc.get(asin)
        fba = fba_by_asin.get(asin)
        entry = dict(main_by_upc.get(upc, {})) if upc else dict(main_by_asin_fallback.get(asin, {}))
        entry["fba_sku"] = (fba or {}).get("ProductID")
        # Only needed by Seller Analytics (an ASIN we don't carry has no
        # vendor-offer UPC to key off of, so this is the fallback source
        # when Keepa's own UPC/EAN list comes back empty for it).
        entry["upc"] = upc or (fba or {}).get("UPC")
        out[asin] = entry
    return out


def _fba_total_by_asin(asins: list[str]) -> dict:
    """{asin: FBA total qty} from amazon.amazon_fulfilled_by_amazon_manage_inventory,
    summed across that ASIN's FBA SKUs on the table's latest daily snapshot.

    Uses the table-wide MAX(dte), not each ASIN's own latest date: when Amazon
    moves a SKU to another ASIN the old ASIN drops out of the daily feed, and
    its last-ever snapshot kept showing stock that no longer exists (2026-10-28:
    45 ASINs / 3,599 phantom units, e.g. B00AU0OSTE still showing 363). An ASIN
    absent from the latest snapshot gets no entry (blank), same as never-stocked."""
    out: dict = {}
    for chunk in _chunks([a.strip().upper() for a in asins if a]):
        try:
            rows, cols = _azure_rows(
                "SELECT LTRIM(RTRIM(asin)) asin, SUM(fulfilled_by_amazon_total_quantity) q "
                "FROM amazon.amazon_fulfilled_by_amazon_manage_inventory "
                "WHERE dte = (SELECT MAX(dte) FROM amazon.amazon_fulfilled_by_amazon_manage_inventory) "
                f"AND LTRIM(RTRIM(asin)) IN ({_inlist(chunk)}) GROUP BY LTRIM(RTRIM(asin))")
        except Exception as exc:  # noqa: BLE001
            log.warning("[template_export] fba inventory query failed: %s", str(exc)[:120])
            continue
        for r in rows:
            d = dict(zip(cols, r))
            out[d["asin"]] = _f(d["q"]) or 0
    return out


def _sellersnap_30d_by_asin(asins: list[str]) -> dict:
    """{asin: sum(total_ordered_items)} from SellerSnap. Empty dict if the source
    isn't available (the column/table may not exist in every environment)."""
    out: dict = {}
    for chunk in _chunks([a.strip().upper() for a in asins if a]):
        try:
            rows, cols = _azure_rows(
                "SELECT LTRIM(RTRIM(asin)) asin, SUM(total_ordered_items) s "
                "FROM sellersnap.all_stores_product_listings "
                f"WHERE LTRIM(RTRIM(asin)) IN ({_inlist(chunk)}) GROUP BY LTRIM(RTRIM(asin))")
        except Exception as exc:  # noqa: BLE001
            log.warning("[template_export] sellersnap query failed: %s", str(exc)[:120])
            return {}
        for r in rows:
            d = dict(zip(cols, r))
            out[d["asin"]] = _f(d["s"])
    return out


def _units_30d(asin, report30: dict, snap30: dict):
    """30d sales for one ASIN. The SP-API Sales & Traffic report covers our whole
    catalog, so once it has loaded an ASIN missing from it sold 0 -- falling
    back to SellerSnap there mixed in unrelated numbers (summed across every
    SellerSnap store; e.g. B00AU0OSTE showed 317 from SellerSnap). SellerSnap is
    only used when the report didn't load at all (live=False or fetch failed)."""
    a = (asin or "").strip().upper()
    if report30:
        return report30.get(a, 0) if a else None
    return snap30.get(a)


def _last_po_by_sku(skus: list[str]) -> dict:
    """{sku: (unit_price, vendor)} = the latest purchase order (PODate ≥ 2025) per
    SellerCloud SKU, from analytics.purchases_view. Older POs are excluded."""
    out: dict = {}
    for chunk in _chunks([s for s in skus if s]):
        try:
            rows, cols = _azure_rows(
                "SELECT ProductID, PODate, UnitPrice, Vendor FROM analytics.purchases_view "
                f"WHERE PODate >= '2025-01-01' AND ProductID IN ({_inlist(chunk)})")
        except Exception as exc:  # noqa: BLE001
            log.warning("[template_export] purchases_view query failed: %s", str(exc)[:120])
            continue
        best: dict = {}   # sku -> (podate, price, vendor)
        for r in rows:
            d = dict(zip(cols, r))
            sku, pod = d["ProductID"], d["PODate"]
            if pod is None:
                continue
            if sku not in best or pod > best[sku][0]:
                best[sku] = (pod, _f(d["UnitPrice"]), d["Vendor"])
        for sku, (_pod, price, vendor) in best.items():
            out[sku] = (price, vendor)
    return out


def _storage_est(h, l, w) -> float | None:
    """Volume-only FBA storage estimate (off-peak standard rate). Keepa carries no
    weight, so the tier is assumed standard — used only as a fallback when the live
    SP-API dimensions (which include weight) aren't available."""
    h, l, w = _f(h), _f(l), _f(w)
    if not (h and l and w):
        return None
    cm_in = 0.393701
    cubic_ft = (h * cm_in) * (l * cm_in) * (w * cm_in) / 1728
    return round(cubic_ft * 0.78, 2)


def enrich_asins(asins, on_progress=None, should_cancel=None,
                  check_generic: bool = False) -> tuple[dict, dict, dict, dict]:
    """Live per-ASIN SP-API enrichment → ({asin: eligibility}, {asin: storage_fee_offpeak},
    {asin: generic_status}, {asin: storage_fee_peak}). Eligibility (getListingsRestrictions)
    is checked FIRST for every ASIN (not cached or live-checked); getCatalogItem (dims →
    storage fee off-peak AND Q4 peak) only runs when the ASIN is confirmed NOT dead AND its
    storage isn't already cached (150-day TTL) — a DOG ASIN (determined via classify()'s own
    `dog` field, same signal the restrictions check already produces) never needs
    getCatalogItem at all, since there are no dimensions to compute for a delisted product.
    This ordering (2026-10-19, eligibility before catalog — was the reverse) both removes a
    redundant getCatalogItem-based DOG check that duplicated what eligibility already told
    us, and shifts load off getCatalogItem's much tighter rate limit (2 req/sec vs
    getListingsRestrictions' 5 req/sec) onto the endpoint actually least likely to 429.

    Storage is cached via the SAME shared `storage_fee_cache` table the Toolbox's
    ASIN-lookup tool uses (services/storage_fees.py storage_cache_get/put, which persists
    the full result incl. both fees in a `data_json` column) -- this function used to keep
    its OWN simpler cache that only ever wrote the off-peak figure, silently discarding
    the peak value it had already computed on every live lookup (fixed 2026-10-19, per
    user request: "let's store all the peak and non peak storage"). Sharing one cache also
    means a Vendor-Offers export and a Toolbox storage check for the same ASIN now reuse
    each other's work instead of each paying for their own getCatalogItem call.

    Eligibility (getListingsRestrictions) is ALSO cached now (14-day TTL since 2026-10-28, was 7;
    services.restrictions.eligibility_cache_get/put), per user request
    (2026-10-19: "cache eligibility for 1 week") — this is scoped to THIS
    pipeline only; the Toolbox's own standalone eligibility checker stays
    fully live, unaffected, by design (see that cache helper's own docstring).
    A fresh DOG finding from the catalog call (the defense-in-depth fallback
    described above) always overrides whatever eligibility said, and every
    newly-determined (or corrected) eligibility result is written back to the
    cache, refreshing it.

    When check_generic=True, an additional Generic-ASIN classification
    (services.generic_check.classify_generic -- one more catalog call + a non-persisting
    putListingsItem VALIDATION_PREVIEW per ASIN) runs alongside the others in the same
    worker pool -- opt-in because it doubles the SP-API load per ASIN; skipped for an ASIN
    already known DOG. 5 workers; failures skipped (that cell falls back / stays blank)."""
    import requests
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from services.spapi.client import lease_catalog_api
    from services.spapi.config import load_sp_api_credentials, sp_api_configured
    from services.restrictions import classify, get_restrictions_api, eligibility_cache_get, eligibility_cache_put
    from services.storage_fees import calc_storage_fee, extract_dimensions, storage_cache_get, storage_cache_put
    from services.generic_check import classify_generic

    elig: dict = {}
    generic: dict = {}
    storage_peak: dict = {}
    uniq = sorted({a.strip().upper() for a in asins if a})
    if not uniq or not sp_api_configured():
        if on_progress:
            on_progress(0, 0)
        return elig, {}, generic, storage_peak

    cached = storage_cache_get(uniq)                          # {asin: full result dict} within TTL
    storage: dict = {}
    for a, r in cached.items():
        if r.get("fee_offpeak") is not None:
            storage[a] = r["fee_offpeak"]
        if r.get("fee_peak") is not None:
            storage_peak[a] = r["fee_peak"]
    to_cache: list[dict] = []

    # Eligibility cache (14-day TTL) — OPT-IN here per user request (2026-10-19);
    # see services/restrictions.py's own eligibility_cache_get/put docstring for
    # why this is scoped to this export pipeline only, not the Toolbox's
    # standalone "always live" ASIN-lookup eligibility checker.
    elig_cached = eligibility_cache_get(uniq)
    elig_to_cache: dict = {}

    seller = load_sp_api_credentials().seller_id
    rapi = get_restrictions_api()

    # Store-leased for the whole run (2026-10-20, per user request) -- if
    # another batch job (PO Analytics, Brand Analytics, a second Vendor
    # Offers export, ...) is ALSO leasing "default" right now, this run gets
    # pinned to "turba" instead so the two never fight over one store's
    # 2 req/sec getCatalogItem ceiling. See lease_catalog_api()'s own
    # docstring for why this differs from the per-request round-robin used
    # by catalog search.
    with lease_catalog_api() as capi:
        def one(asin: str):
            e = s = s_peak = g = status = None
            elig_from_cache = False
            have = asin in cached

            # Eligibility FIRST, not the catalog -- two reasons (per user question
            # 2026-10-19, "don't we find dog asins when we do the eligibility
            # check?"): (1) classify()'s own `dog` field (derived from Amazon's
            # ASIN_NOT_FOUND reasonCode -- confirmed live, 2026-09-04) is a real,
            # already-working DOG signal that this function used to ignore
            # entirely, instead paying for a SEPARATE getCatalogItem call just to
            # find the exact same thing via a 404; (2) getListingsRestrictions has
            # a looser rate limit than getCatalogItem (5 req/sec vs 2 req/sec --
            # the tighter one is what's actually been producing 429s), so
            # resolving DOG here first means a dead ASIN can skip getCatalogItem
            # entirely -- no dimensions are needed for a product that no longer
            # exists in the catalog.
            cached_status = elig_cached.get(asin)
            if cached_status is not None:
                e = cached_status
                elig_from_cache = True
            else:
                try:
                    cls = classify(asin, rapi.get_restrictions(asin, seller, should_cancel=should_cancel))
                    e = "DOG" if cls.get("dog") else cls["status"]
                except JobCancelled:
                    raise  # let this propagate -- NOT a per-ASIN failure, the whole run is being stopped
                except Exception:  # noqa: BLE001
                    pass

            if e != "DOG" and not have:   # only hit the catalog when genuinely needed: live + dims not cached
                try:
                    raw = capi.get_by_asin(asin, included_data="attributes,summaries",
                                           should_cancel=should_cancel)
                    dims = extract_dimensions(raw.get("attributes") or {})
                    s, s_peak = calc_storage_fee(dims["length_cm"], dims["width_cm"],
                                                 dims["height_cm"], dims["weight_g"])
                    status = "ok" if s is not None else "no_dimensions"
                except JobCancelled:
                    raise  # ditto -- a cancelled run, not a failed ASIN
                except requests.HTTPError as ex:
                    if getattr(ex.response, "status_code", None) == 404:
                        # Defense-in-depth fallback: eligibility said "not DOG" (or
                        # wasn't checked), but the catalog itself 404s -- this is a
                        # fresher, more authoritative signal and always overrides.
                        e = "DOG"
                        status = "dog"
                except Exception:  # noqa: BLE001
                    status = "error"

            if check_generic and e != "DOG":
                try:
                    gj = classify_generic(asin)
                    g = gj["status"]
                    if g == "DOG":
                        e = "DOG"
                except Exception:  # noqa: BLE001
                    pass
            return asin, e, s, s_peak, have, elig_from_cache, g, status

        done = 0
        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = [ex.submit(one, a) for a in uniq]
            for f in as_completed(futs):
                if should_cancel and should_cancel():
                    for fut in futs:
                        fut.cancel()
                    raise ExportCancelled()
                try:
                    a, e, s, s_peak, have, elig_from_cache, g, status = f.result()
                except JobCancelled:
                    # A worker noticed should_cancel() mid-retry-backoff (see
                    # services.cancellation.JobCancelled) and bailed out early
                    # instead of finishing its full ~2.5min retry sequence first --
                    # this is what makes "Cancel" actually take effect promptly
                    # during a 429 storm instead of being stuck behind it.
                    for fut in futs:
                        fut.cancel()
                    raise ExportCancelled()
                if e is not None:
                    elig[a] = e
                    if not elig_from_cache:                        # newly-determined (or a fresher DOG) -> (re)cache it
                        elig_to_cache[a] = e
                if g is not None:
                    generic[a] = g
                if not have:
                    if status in ("ok", "no_dimensions", "dog"):   # cache even a None fee (avoids refetch)
                        to_cache.append({"asin": a, "status": status, "fee_offpeak": s, "fee_peak": s_peak})
                    if s is not None:
                        storage[a] = s
                    if s_peak is not None:
                        storage_peak[a] = s_peak
                done += 1
                if on_progress:
                    on_progress(done, len(uniq))
    storage_cache_put(to_cache)
    eligibility_cache_put(elig_to_cache)
    return elig, storage, generic, storage_peak


# ── build ──────────────────────────────────────────────────────────────────

# "Sellable & selling only" filter thresholds (services/vendor_offers_template_export
# .build_template_xlsx, apply_filters=True) -- per user request 2026-09-23.
MIN_MONTHLY_UNITS_SOLD = 50
MAX_AMAZON_30D_BB_SHARE = 0.90     # exclude ASINs where Amazon itself holds >=90% of the Buy Box
EXCLUDED_APPROVAL_STATUSES = {"RESTRICTED", "DOG"}


def _passes_keepa_filters(k: dict) -> bool:
    """The part of apply_filters that Keepa alone decides (units sold + Amazon's
    Buy Box share). Blank Keepa values count as 0, same as the row builder."""
    monthly = _f(k.get("monthly_sold_quantity")) or 0
    amz30 = (_f(k.get("amazon_bb_30d_percentage")) or 0) / 100
    return monthly >= MIN_MONTHLY_UNITS_SOLD and amz30 < MAX_AMAZON_30D_BB_SHARE


def build_template_xlsx(upcs: list[str] | None = None, focus_vendor: str | None = None,
                        prep_fee: float | None = None, has_asin: bool = False,
                        only_available: bool = False, limit: int | None = None,
                        live: bool = True, apply_filters: bool = False,
                        populate_keepa: bool = True,
                        on_progress=None, should_cancel=None) -> bytes:
    # apply_filters needs Keepa's monthly-units/Amazon-30d-% fields -- without Keepa
    # every item would read 0 units sold and fail the floor, filtering out everything.
    # The UI already disables "Apply filters" when Keepa is skipped; this guards any
    # other caller from the same silently-empty-export trap.
    if not populate_keepa:
        apply_filters = False
    if prep_fee is None:
        try:
            prep_fee = float(database.get_setting("prep_out_fee", "0.25"))
        except (TypeError, ValueError):
            prep_fee = 0.25

    target = set(upcs) if upcs else None
    by_upc, vendors = _vendor_offers_by_upc(target)

    # Focus vendor: keep only items where that vendor is the cheapest (or the only
    # vendor); its offer supplies Product Cost / Item ID / Vendor Title / qty.
    focus = focus_vendor if focus_vendor in vendors else None
    kept: dict = {}
    for upc, vmap in by_upc.items():
        cheapest = min(v["cost"] for v in vmap.values())
        winners = [vn for vn, v in vmap.items() if round(v["cost"], 4) == round(cheapest, 4)]
        if focus:
            if focus not in winners:
                continue
            win_vendor = focus
        else:
            win_vendor = sorted(winners)[0]
        win = vmap[win_vendor]
        # "Only items with available qty" → skip offers with no/zero availability
        if only_available and not ((win.get("avail") or 0) > 0):
            continue
        kept[upc] = (win, win["cost"], win_vendor)
    upc_list = list(kept.keys())
    if limit:
        upc_list = upc_list[:limit]

    # UPC → ALL of its ASINs: our curated Pair-Library assignments UNION the
    # Keepa-barcode ASINs (a UPC legitimately has many ASINs; an ASIN has one UPC).
    # Each (UPC, ASIN) becomes its own export row.
    keys = {_bkey(u) for u in upc_list if _bkey(u)}
    bmap = _barcode_to_asins(keys)
    assigned = _assigned_asins_by_upc()

    def asins_for(upc):
        return list(dict.fromkeys(list(assigned.get(upc, [])) + list(bmap.get(_bkey(upc), []))))

    chosen = {u: asins_for(u) for u in upc_list}                 # upc -> [asin, ...]
    chosen_asins = sorted({a for lst in chosen.values() for a in lst})
    # Reverse lookup for _sku_data_by_asin's UPC-keyed main-SKU query -- an
    # ASIN has exactly one UPC (a UPC can have several ASINs), so this is a
    # clean 1:1 map even though `chosen` itself isn't.
    asin_to_upc = {a: u for u, lst in chosen.items() for a in lst}

    # Keepa (live /product pricing) and the per-ASIN SP-API enrichment (eligibility +
    # storage fee, both off-peak and Q4 peak) hit two entirely DIFFERENT external
    # services with their own independent rate limiters -- there's no reason they
    # were ever run one after the other. Per user report (2026-10-19): "if I choose
    # with keepa data - does it first find keepa data, and only then finds the
    # storage and eligibility? because storage should be instant no?" -- confirmed
    # that's exactly what the old code did (one call, then the other); now both run
    # concurrently in their own threads, joined before building rows, while the
    # (Azure-only, no Keepa/SP-API involved) SellerCloud/SellerSnap lookups run on
    # this thread in the meantime -- three independent data sources overlapped
    # instead of chained. Progress from both threads is combined into one running
    # done/total so the job's own progress bar still reads sensibly.
    import threading
    _prog_lock = threading.Lock()
    _prog = {"kd": 0, "kt": 0, "ed": 0, "et": 0}

    def _combined_progress():
        if not on_progress:
            return
        with _prog_lock:
            kd, kt, ed, et = _prog["kd"], _prog["kt"], _prog["ed"], _prog["et"]
        kd_open, ed_open = kt and kd < kt, et and ed < et
        if kd_open and ed_open:
            phase = "Keepa live prices + eligibility/storage"
        elif kd_open:
            phase = "Keepa live prices"
        elif ed_open:
            phase = "eligibility check"
        else:
            phase = "finishing"
        on_progress(kd + ed, kt + et, phase)

    def _kp(d, t):
        with _prog_lock:
            _prog["kd"], _prog["kt"] = d, t
        _combined_progress()

    def _ep(d, t):
        with _prog_lock:
            _prog["ed"], _prog["et"] = d, t
        _combined_progress()

    keepa_box: list = [{}]
    elig_box: list = [({}, {}, {}, {})]
    thread_errors: list = []

    def _run_keepa():
        try:
            if populate_keepa:
                keepa_box[0] = keepa_api.fetch_products(chosen_asins, on_progress=_kp,
                                                         should_cancel=should_cancel)
            else:
                _kp(0, 0)
        except JobCancelled:
            # keepa.fetch_products raises the generic cancellation signal (it
            # doesn't know about this module's own ExportCancelled) -- convert
            # it here so the main thread's `raise thread_errors[0]` below
            # surfaces the SAME exception type enrich_asins' own cancel path
            # does, which routers/vendor_offers.py specifically catches.
            thread_errors.append(ExportCancelled())
        except Exception as exc:  # noqa: BLE001
            thread_errors.append(exc)

    def _run_elig():
        try:
            if live:
                targets = chosen_asins
                if apply_filters:
                    # Keepa first (2026-10-28, per user): rows failing the Keepa-only
                    # filters are dropped regardless of eligibility/storage/generic,
                    # so SP-API is only spent on survivors -- in a Cencora sample
                    # ~77% of ASINs failed units/BB-share, and those SP-API calls
                    # (eligibility + getCatalogItem + generic) were pure waste.
                    t_keepa.join()
                    if thread_errors:
                        return
                    kp = keepa_box[0]
                    targets = [a for a in chosen_asins
                               if _passes_keepa_filters(kp.get(a.strip().upper(), {}))]
                elig_box[0] = enrich_asins(targets, _ep, should_cancel, check_generic=apply_filters)
            else:
                _ep(0, 0)
        except Exception as exc:  # noqa: BLE001
            thread_errors.append(exc)

    t_keepa = threading.Thread(target=_run_keepa, name="offer-export-keepa", daemon=True)
    t_elig = threading.Thread(target=_run_elig, name="offer-export-elig", daemon=True)
    t_keepa.start()
    t_elig.start()

    amz_pack = _amz_pack_by_asin()
    elig_cache = _eligibility_by_asin()

    # SellerCloud + Amazon + SellerSnap enrichment for the chosen ASINs
    sku_by_asin = _sku_data_by_asin(chosen_asins, asin_to_upc)   # SKU / FBA SKU / on order / OR33
    fba_total = _fba_total_by_asin(chosen_asins)                 # FBA total (Amazon FBA inventory)
    snap30 = _sellersnap_30d_by_asin(chosen_asins)               # 30d sales fallback (SellerSnap)
    report30 = sp_reports.get_units_30d() if live else {}        # 30d sales (SP-API Sales & Traffic, cached daily)
    last_po = _last_po_by_sku(                                    # Last cost + Vendor (latest PO ≥ 2025)
        [v["main_sku"] for v in sku_by_asin.values() if v.get("main_sku")])

    t_keepa.join()
    t_elig.join()
    if thread_errors:
        raise thread_errors[0]

    keepa = keepa_box[0]
    live_elig, live_storage, live_generic, live_storage_peak = elig_box[0]

    def z(v):                                        # Keepa numeric: blank → 0
        f = _f(v)
        return f if f is not None else 0
    pct = lambda v: z(v) / 100

    # one record per (UPC, ASIN) — a UPC expands to ALL its ASINs — then sort by
    # Est Monthly Units desc.
    records: list[dict] = []
    for upc in upc_list:
        win, prod_cost, win_vendor = kept[upc]
        row_asins = chosen.get(upc) or []
        if not row_asins:
            if has_asin:                   # "Has ASIN" filter → skip items with no ASIN
                continue
            row_asins = [None]             # keep the UPC as a single no-ASIN row
        for asin in row_asins:
            key = (asin or "").strip().upper()
            k = keepa.get(key, {}) if asin else {}
            sku = sku_by_asin.get(key, {})
            pack = amz_pack.get(asin) or k.get("package_qty") or 1
            try:
                pack = int(float(pack))
            except (TypeError, ValueError):
                pack = 1
            bb, bb30, bb90 = _f(k.get("current_buybox_usd")), _f(k.get("buybox_30d")), _f(k.get("buybox_90d"))
            bb_cur = bb if bb is not None else (bb30 if bb30 is not None else (bb90 if bb90 is not None else 0))
            storage = live_storage.get(asin)
            if storage is None:
                storage = _storage_est(k.get("height"), k.get("length"), k.get("width"))
            storage = storage if storage else 0.1
            approved = live_elig.get(asin) or elig_cache.get(asin)
            monthly = z(k.get("monthly_sold_quantity"))
            if apply_filters:
                if (not _passes_keepa_filters(k)
                        or approved in EXCLUDED_APPROVAL_STATUSES
                        or live_generic.get(asin) == "GENERIC"):
                    continue
            records.append({
                "upc": upc, "asin": asin, "k": k, "win": win, "prod_cost": prod_cost,
                "pack": pack, "bb_cur": bb_cur, "storage": storage, "sku": sku,
                "approved": approved,
                "monthly": monthly, "win_vendor": win_vendor,
            })
    records.sort(key=lambda d: d["monthly"], reverse=True)   # highest est. monthly sales first

    # ── workbook: template + the 8 inserted columns (insert_cols keeps the styled
    #    headers of the shifted columns; we only style the new blanks) ──
    wb = load_workbook(_TEMPLATE)
    ws = wb["Analytics"]
    ws.insert_cols(2, 3)     # Offer Vendor, SKU, FBA SKU  (after UPC)
    ws.insert_cols(12, 5)    # Available qty, OR33, On order, FBA total, 30d sales (after qty/case)
    ws.insert_cols(19, 2)    # Last cost, Vendor  (after amz pack)
    ws.cell(row=1, column=COL["qty/case"]).value = "qty/case"
    for name in _NEW_HEADER_COLS:
        c = COL[name]
        hc = ws.cell(row=1, column=c, value=name)
        hc.fill, hc.font, hc.alignment = _HDR_GREY, _ROBOTO, _HDR_ALIGN
        ws.column_dimensions[get_column_letter(c)].width = 14
    ws.conditional_formatting = ConditionalFormattingList()   # drop the template's old CF
    ws.freeze_panes = f"{L('Approved')}2"                      # keep UPC..Link visible

    r = 2
    for d in records:
        k, win, upc, asin, pack, sku = d["k"], d["win"], d["upc"], d["asin"], d["pack"], d["sku"]
        rowd = {
            "UPC/EAN": int(upc) if str(upc).isdigit() else upc,
            "SKU": sku.get("main_sku"), "FBA SKU": sku.get("fba_sku"),
            "ASIN": asin,
            "Link": f'=HYPERLINK("https://www.amazon.com/dp/"&{L("ASIN")}{r})' if asin else None,
            "Offer Vendor": d["win_vendor"],
            "Approved": d["approved"], "Item ID": win["item_id"], "Vendor Title": win["title"],
            "Amazon title": k.get("title"), "qty/case": win["qty"],
            "Available qty": win.get("avail"),
            "OR33": sku.get("or33"), "On order": sku.get("on_order"),
            "FBA total": fba_total.get((asin or "").strip().upper()),
            "30d sales": _units_30d(asin, report30, snap30),
            "Estimated Monthly Units Sold": d["monthly"], "amz pack": pack,
            "Last cost": (last_po.get(sku.get("main_sku")) or (None, None))[0],
            "Vendor": (last_po.get(sku.get("main_sku")) or (None, None))[1],
            "Product Cost": round(d["prod_cost"], 4),
            "Product cost * amz pack": f'={L("Product Cost")}{r}*{L("amz pack")}{r}',
            "Buy Box Price current": round(d["bb_cur"], 2),
            "BB Price 30d": z(k.get("buybox_30d")), "BB Price 90d": z(k.get("buybox_90d")),
            "Net Profit": (f'={L("Buy Box Price current")}{r}-{L("Product cost * amz pack")}{r}'
                           f'-{L("FBA Fee")}{r}-{L("Referral Fee")}{r}-{L("Storage Fee")}{r}-{L("Prep & Out fee")}{r}'),
            "Net Margin": f'=IFERROR({L("Net Profit")}{r}/{L("Buy Box Price current")}{r},0)',
            "ROI": f'=IFERROR({L("Net Profit")}{r}/{L("Product cost * amz pack")}{r},0)',
            "FBA Fee": z(k.get("fba_fees_usd")),
            "Referral Fee": (f'=IF({L("Buy Box Price current")}{r}>10,'
                             f'{L("Buy Box Price current")}{r}*0.15,{L("Buy Box Price current")}{r}*0.08)'),
            "Storage Fee": round(d["storage"], 2), "Prep & Out fee": prep_fee,
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

    # highlight duplicate UPC + ASIN values (Excel-standard red) + restore the
    # template's own red-flag rules on the financial columns -- both were wiped
    # by the `ConditionalFormattingList()` reset above (needed because the
    # template's OWN ranges reference pre-insertion column letters that no
    # longer line up once the 10 new columns are inserted), so they must be
    # rebuilt here at the columns' NEW (post-insertion) positions via L().
    # Thresholds confirmed straight from the template file itself (2026-10-06):
    # Net Margin < 10%, ROI < 15%, Rank (current) > 100,000 all flag red; Net
    # Profit < $0 is a new addition (the template never had one for that column).
    last = r - 1
    if last >= 2:
        dxf = DifferentialStyle(
            fill=PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid"),
            font=Font(color="9C0006"))
        for col in (L("UPC/EAN"), L("ASIN")):
            ws.conditional_formatting.add(f"{col}2:{col}{last}", Rule(type="duplicateValues", dxf=dxf))
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

    # AutoFilter: insert_cols shifts cell data/styles but does NOT widen the
    # template's own auto_filter.ref ("A1:AI1", its original 35-column width)
    # -- so after the 10 inserted columns, the filter dropdowns silently
    # stopped at column AI and never covered AJ-AS. Re-set it to the sheet's
    # real final width every export.
    ws.auto_filter.ref = f"A1:{L(TEMPLATE_COLS[-1][0])}{max(last, 1)}"

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_filename(focus_vendor: str | None = None) -> str:
    """`<Vendor> <date> <NN>.xlsx`, NN incrementing per vendor per day."""
    date = dt.date.today().isoformat()
    base = re.sub(r"[^A-Za-z0-9 &._-]", "", (focus_vendor or "Offer Analytics").strip()) or "export"
    n = database.next_seq(f"export:{base}:{date}")
    return f"{base} {date} {n:02d}.xlsx"
