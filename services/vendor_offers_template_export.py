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
from services.spapi import reports as sp_reports

log = logging.getLogger(__name__)

_TEMPLATE = Path(__file__).resolve().parent.parent / "assets" / "offer_analytics_template.xlsx"
AMAZON_SELLER_ID = "ATVPDKIKX0DER"
REFERRAL_RATE = 0.15                     # example: Referral = 0.15 × Buy Box
_ACCT = '_("$"* #,##0.00_);_("$"* \\(#,##0.00\\);_("$"* "-"??_);_(@_)'

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
    ("BB Price 30d", _ACCT, "glt", False, True), ("BB Price 90d", _ACCT, "glt", False, True),
    ("Net Profit", "0.00", None, False, False), ("Net Margin", "0%", None, False, False),
    ("ROI", "0%", None, False, False),
    ("FBA Fee", _ACCT, None, False, True), ("Referral Fee", _ACCT, None, False, False),
    ("Storage Fee", _ACCT, None, False, False), ("Prep & Out fee", _ACCT, None, False, False),
    ("Rank (current)", None, None, False, True), ("Rank 30d", None, None, False, True),
    ("BB seller", None, None, False, False), ("BB share 30d", "0%", None, False, True),
    ("BB share 90d", "0%", None, False, True), ("Amazon 30d %", "0%", None, False, True),
    ("Amazon 90d %", "0%", None, False, True),
    ("FBM sellers", None, None, False, True), ("FBA sellers", None, None, False, True),
    ("Brand", None, None, False, False), ("Category", None, None, False, False),
    ("Total Ratings Count", None, None, False, True), ("Variation Parent", None, None, False, False),
]
# 1-based column index by header name → lets formulas reference cells by name.
COL = {h: i + 1 for i, (h, *_ ) in enumerate(TEMPLATE_COLS)}
def L(name):
    return get_column_letter(COL[name])

# columns that are NEW (not in the loaded template) and need header styling
_NEW_HEADER_COLS = ("SKU", "FBA SKU", "Available qty", "OR33", "On order", "FBA total",
                    "30d sales", "Last cost", "Vendor")

_ROBOTO = Font(name="Roboto", size=12)                 # header font (matches template)
_BOLD = Font(name="Calibri", size=11, bold=True)       # bold data (matches manual)
_CENTER = Alignment(horizontal="center")
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
    """{upc: {vendor: {cost, qty, item_id, title}}} — best (cheapest) offer per
    (upc, vendor). Also returns the ordered vendor list."""
    with database._connect() as conn:
        vendors = [r[0] for r in conn.execute(
            "SELECT DISTINCT vendor_name FROM vo_vendor_best_offer ORDER BY vendor_name")]
        rows = conn.execute("""
            SELECT v.vendor_name, o.upc, o.vendor_item_id, o.cost, o.qty_per_case,
                   o.description, o.avail_qty
            FROM vo_vendor_offers o JOIN vo_vendors v ON v.vendor_id = o.vendor_id
        """).fetchall()
    by_upc: dict = {}
    for vname, upc, iid, cost, qty, desc, avail in rows:
        if upcs is not None and upc not in upcs:
            continue
        c = _f(cost)
        if c is None:
            continue
        slot = by_upc.setdefault(upc, {})
        prev = slot.get(vname)
        if prev is None or c < prev["cost"]:
            slot[vname] = {"cost": c, "qty": qty, "item_id": iid, "title": desc, "avail": _f(avail)}
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


def _sku_data_by_asin(asins: list[str]) -> dict:
    """{asin: {main_sku, fba_sku, on_order, or33}} from the live SellerCloud view
    (Ford Medical). Main SKU = the Merchant (parent) SKU, FBA SKU = the Amazon
    shadow. On order = OnOrder; OR33 = InventoryPhysicalQty (OR33 is the warehouse)."""
    out: dict = {}
    for chunk in _chunks([a.strip().upper() for a in asins if a]):
        try:
            rows, cols = _azure_rows(
                "SELECT LTRIM(RTRIM(ASIN)) asin, ProductID, ShadowOf, FulfilledBy, Status, "
                "OnOrder, InventoryPhysicalQty, LastPurchasedOn "
                "FROM analytics.sku_data_extended_view "
                f"WHERE CompanyName LIKE 'Ford%' AND LTRIM(RTRIM(ASIN)) IN ({_inlist(chunk)})")
        except Exception as exc:  # noqa: BLE001
            log.warning("[template_export] sku view query failed: %s", str(exc)[:120])
            continue
        by: dict = {}
        for r in rows:
            d = dict(zip(cols, r))
            by.setdefault(d["asin"], []).append(d)
        for asin, rs in by.items():
            merch = [r for r in rs if r["FulfilledBy"] == "Merchant"]
            amz = [r for r in rs if r["FulfilledBy"] == "Amazon"]
            mkey = lambda r: (r["Status"] == "Active", not (r["ShadowOf"] or ""),
                              r["LastPurchasedOn"] or dt.datetime.min)
            main = max(merch, key=mkey) if merch else (max(rs, key=mkey) if rs else None)
            fba = amz[0] if amz else None
            # main SKU = the base/parent: prefer ShadowOf; else strip the shadow
            # suffixes (-FBA/-FBM/_QY<n>) off the ProductID → the base SKU.
            base = ((main or {}).get("ShadowOf") or (main or {}).get("ProductID")) if main else None
            main_sku = re.sub(r"(_QY\d+|-FB[AM])+$", "", base) if base else None
            out[asin] = {
                "main_sku": main_sku,
                "fba_sku": (fba or {}).get("ProductID"),
                "on_order": (main or {}).get("OnOrder"),
                "or33": (main or {}).get("InventoryPhysicalQty"),
            }
    return out


def _fba_total_by_asin(asins: list[str]) -> dict:
    """{asin: FBA total qty} from amazon.amazon_fulfilled_by_amazon_manage_inventory
    (latest snapshot per ASIN, summed across stores)."""
    out: dict = {}
    for chunk in _chunks([a.strip().upper() for a in asins if a]):
        try:
            rows, cols = _azure_rows(
                "SELECT LTRIM(RTRIM(asin)) asin, dte, fulfilled_by_amazon_total_quantity q "
                "FROM amazon.amazon_fulfilled_by_amazon_manage_inventory "
                f"WHERE LTRIM(RTRIM(asin)) IN ({_inlist(chunk)})")
        except Exception as exc:  # noqa: BLE001
            log.warning("[template_export] fba inventory query failed: %s", str(exc)[:120])
            continue
        latest: dict = {}   # asin -> max dte
        for r in rows:
            d = dict(zip(cols, r))
            a, dte = d["asin"], d["dte"]
            if dte is not None and (a not in latest or dte > latest[a]):
                latest[a] = dte
        for r in rows:
            d = dict(zip(cols, r))
            a = d["asin"]
            if d["dte"] == latest.get(a):
                out[a] = (out.get(a) or 0) + (_f(d["q"]) or 0)
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


_STORAGE_TTL_DAYS = 60   # bi-monthly: cached storage older than this is re-fetched


def _storage_cache_get(asins) -> dict:
    """{asin: storage_fee (may be None)} for entries cached within the TTL."""
    cutoff = (dt.datetime.now() - dt.timedelta(days=_STORAGE_TTL_DAYS)).isoformat()
    out: dict = {}
    clean = sorted({a.strip().upper() for a in asins if a})
    with database._connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS storage_fee_cache "
                     "(asin TEXT PRIMARY KEY, storage_fee REAL, as_of TEXT)")
        for i in range(0, len(clean), 400):
            ch = clean[i:i + 400]
            q = ",".join("?" * len(ch))
            for asin, fee in conn.execute(
                    f"SELECT asin, storage_fee FROM storage_fee_cache "
                    f"WHERE as_of >= ? AND asin IN ({q})", [cutoff, *ch]):
                out[asin] = fee
    return out


def _storage_cache_put(mapping: dict) -> None:
    if not mapping:
        return
    now = dt.datetime.now().isoformat()
    with database._LOCK, database._connect() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS storage_fee_cache "
                     "(asin TEXT PRIMARY KEY, storage_fee REAL, as_of TEXT)")
        conn.executemany(
            "INSERT INTO storage_fee_cache(asin, storage_fee, as_of) VALUES(?,?,?) "
            "ON CONFLICT(asin) DO UPDATE SET storage_fee=excluded.storage_fee, as_of=excluded.as_of",
            [(a, fee, now) for a, fee in mapping.items()])


def enrich_asins(asins, on_progress=None, should_cancel=None) -> tuple[dict, dict]:
    """Live per-ASIN SP-API enrichment → ({asin: eligibility}, {asin: storage_fee}).
    getListingsRestrictions (Approved) runs for every ASIN; getCatalogItem (dims →
    storage fee, 404=DOG) is SKIPPED for ASINs whose storage is already cached
    (bi-monthly TTL), which removes most of the getCatalogItem throttling on repeat
    exports. 5 workers; failures skipped (that cell falls back / stays blank)."""
    import requests
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from services.spapi.client import get_catalog_api
    from services.spapi.config import load_sp_api_credentials, sp_api_configured
    from services.restrictions import classify, get_restrictions_api
    from services.storage_fees import calc_storage_fee, extract_dimensions

    elig: dict = {}
    uniq = sorted({a.strip().upper() for a in asins if a})
    if not uniq or not sp_api_configured():
        if on_progress:
            on_progress(0, 0)
        return elig, {}

    cached = _storage_cache_get(uniq)                        # {asin: fee|None} within TTL
    storage: dict = {a: f for a, f in cached.items() if f is not None}
    to_cache: dict = {}

    seller = load_sp_api_credentials().seller_id
    rapi = get_restrictions_api()
    capi = get_catalog_api()

    def one(asin: str):
        e = s = None
        have = asin in cached
        if not have:                                         # only hit the catalog when not cached
            try:
                raw = capi.get_by_asin(asin, included_data="attributes,summaries")
                dims = extract_dimensions(raw.get("attributes") or {})
                s, _peak = calc_storage_fee(dims["length_cm"], dims["width_cm"],
                                            dims["height_cm"], dims["weight_g"])
            except requests.HTTPError as ex:
                if getattr(ex.response, "status_code", None) == 404:
                    e = "DOG"
            except Exception:  # noqa: BLE001
                pass
        if e != "DOG":
            try:
                e = classify(asin, rapi.get_restrictions(asin, seller))["status"]
            except Exception:  # noqa: BLE001
                pass
        return asin, e, s, have

    done = 0
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = [ex.submit(one, a) for a in uniq]
        for f in as_completed(futs):
            if should_cancel and should_cancel():
                for fut in futs:
                    fut.cancel()
                raise ExportCancelled()
            a, e, s, have = f.result()
            if e is not None:
                elig[a] = e
            if not have:
                to_cache[a] = s                              # cache even None (avoids refetch)
                if s is not None:
                    storage[a] = s
            done += 1
            if on_progress:
                on_progress(done, len(uniq))
    _storage_cache_put(to_cache)
    return elig, storage


# ── build ──────────────────────────────────────────────────────────────────

def build_template_xlsx(upcs: list[str] | None = None, focus_vendor: str | None = None,
                        prep_fee: float | None = None, has_asin: bool = False,
                        only_available: bool = False, limit: int | None = None,
                        live: bool = True, on_progress=None, should_cancel=None) -> bytes:
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
            win = vmap[focus]
        else:
            win = vmap[sorted(winners)[0]]
        # "Only items with available qty" → skip offers with no/zero availability
        if only_available and not ((win.get("avail") or 0) > 0):
            continue
        kept[upc] = (win, win["cost"])
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

    # LIVE Keepa /product for the chosen ASINs (NOT the stale Azure table).
    _kp = (lambda d, t: on_progress(d, t, "Keepa live prices")) if on_progress else None
    keepa = keepa_api.fetch_products(chosen_asins, on_progress=_kp,
                                     should_cancel=should_cancel) if live else {}

    amz_pack = _amz_pack_by_asin()
    elig_cache = _eligibility_by_asin()

    # SellerCloud + Amazon + SellerSnap enrichment for the chosen ASINs
    sku_by_asin = _sku_data_by_asin(chosen_asins)                # SKU / FBA SKU / on order / OR33
    fba_total = _fba_total_by_asin(chosen_asins)                 # FBA total (Amazon FBA inventory)
    snap30 = _sellersnap_30d_by_asin(chosen_asins)               # 30d sales fallback (SellerSnap)
    report30 = sp_reports.get_units_30d() if live else {}        # 30d sales (SP-API Sales & Traffic, cached daily)
    last_po = _last_po_by_sku(                                    # Last cost + Vendor (latest PO ≥ 2025)
        [v["main_sku"] for v in sku_by_asin.values() if v.get("main_sku")])

    live_elig, live_storage = ({}, {})
    if live:
        _ep = (lambda d, t: on_progress(d, t, "eligibility check")) if on_progress else None
        live_elig, live_storage = enrich_asins(chosen_asins, _ep, should_cancel)

    def z(v):                                        # Keepa numeric: blank → 0
        f = _f(v)
        return f if f is not None else 0
    pct = lambda v: z(v) / 100

    # one record per (UPC, ASIN) — a UPC expands to ALL its ASINs — then sort by
    # Est Monthly Units desc.
    records: list[dict] = []
    for upc in upc_list:
        win, prod_cost = kept[upc]
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
            records.append({
                "upc": upc, "asin": asin, "k": k, "win": win, "prod_cost": prod_cost,
                "pack": pack, "bb_cur": bb_cur, "storage": storage, "sku": sku,
                "approved": live_elig.get(asin) or elig_cache.get(asin),
                "monthly": z(k.get("monthly_sold_quantity")),
            })
    records.sort(key=lambda d: d["monthly"], reverse=True)   # highest est. monthly sales first

    # ── workbook: template + the 8 inserted columns (insert_cols keeps the styled
    #    headers of the shifted columns; we only style the new blanks) ──
    wb = load_workbook(_TEMPLATE)
    ws = wb["Analytics"]
    ws.insert_cols(2, 2)     # SKU, FBA SKU  (after UPC)
    ws.insert_cols(11, 5)    # Available qty, OR33, On order, FBA total, 30d sales (after qty/case)
    ws.insert_cols(18, 2)    # Last cost, Vendor  (after amz pack)
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
            "Approved": d["approved"], "Item ID": win["item_id"], "Vendor Title": win["title"],
            "Amazon title": k.get("title"), "qty/case": win["qty"],
            "Available qty": win.get("avail"),
            "OR33": sku.get("or33"), "On order": sku.get("on_order"),
            "FBA total": fba_total.get((asin or "").strip().upper()),
            "30d sales": (report30.get((asin or "").strip().upper())
                          if report30.get((asin or "").strip().upper()) is not None
                          else snap30.get((asin or "").strip().upper())),
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
            if center:
                cell.alignment = _CENTER
        r += 1

    # highlight duplicate UPC + ASIN values (Excel-standard red)
    last = r - 1
    if last >= 2:
        dxf = DifferentialStyle(
            fill=PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid"),
            font=Font(color="9C0006"))
        for col in (L("UPC/EAN"), L("ASIN")):
            ws.conditional_formatting.add(f"{col}2:{col}{last}", Rule(type="duplicateValues", dxf=dxf))

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def export_filename(focus_vendor: str | None = None) -> str:
    """`<Vendor> <date> <NN>.xlsx`, NN incrementing per vendor per day."""
    date = dt.date.today().isoformat()
    base = re.sub(r"[^A-Za-z0-9 &._-]", "", (focus_vendor or "Offer Analytics").strip()) or "export"
    n = database.next_seq(f"export:{base}:{date}")
    return f"{base} {date} {n:02d}.xlsx"
