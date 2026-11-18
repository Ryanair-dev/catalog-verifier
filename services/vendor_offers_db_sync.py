"""
Vendor Offer Analytics -- direct Azure SQL sync (replaces manual file upload for
the 5 vendors below, per user request 2026-09-23).

Ford Medical's internal "FBI" platform (fbi.fordmed.com) stores every vendor
offer someone on the team uploads. Source tables (Azure SQL, database `fbidb`,
same server/connection as azure_sql.py's SellerCloud mirror -- different
schema, `dbo`, reached through the exact same pytds+AAD `_connect()`):

  dbo.vendor_accounts                  -- one row per vendor (id, name, ...)
  dbo.vendor_account_product_listings  -- one row per vendor's offer on a product
      (vendor_account_id -> vendor_accounts.id, packaged_product_id -> packaged_products.id,
       vendor_price_in_currency = the real cost -- vendor_price_usd is ALWAYS NULL,
       currency_id -- filtered to 1 = USD, confirmed 100% for all 5 vendors below)
  dbo.packaged_products                -- uom_unit/uom_quantity (case-pack size), product_id -> products.id
  dbo.products                         -- manufacturer_part_number -- the ONLY identifier field.

INVESTIGATED AND REJECTED as an alternative: the FBI REST API (fbi.fordmed.com,
FBI_API_KEY in .env). It wraps these SAME tables (its Offer/Package/Product
schemas mirror vendor_account_product_listings/packaged_products/products
field-for-field, including the identical manufacturer_part_number-only
limitation -- no dedicated UPC/barcode field anywhere), but /offers has NO
vendor filter (only product_id/package_id), so pulling just 5 of the team's many
vendor accounts means paging through the ENTIRE team's active offers and
filtering client-side -- confirmed live at ~100 offers/1.6s, which would take
many minutes to hours for the volumes below, vs. this direct join finishing in
seconds per vendor. Its "search reaches the barcodes a product's packages
carry" claim was also tested live against real vendor UPCs and returned
unrelated products (noise, not a real barcode index) -- not a usable UPC path.

Only manufacturer_part_number exists as a barcode candidate here too, and it is
genuinely a real UPC-A for some vendors and an arbitrary internal part number
for others (confirmed via checksum validation on live samples: Quality King,
Diamond, Bilo, Victory are >99% valid UPC-A; Cencora is only ~6% -- mostly
internal ABC warehouse codes, not barcodes). `_valid_upc_a()` below enforces a
real UPC-A checksum before accepting a manufacturer_part_number as `upc`, so a
vendor whose MPNs aren't barcodes (chiefly Cencora) simply contributes fewer
usable offers rather than polluting the desk with fake UPCs -- this is the
same failure mode either integration path would hit; it's a data-quality limit
of the source, not something this sync (or the FBI API) can fix.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time

from services import azure_sql, database
from services import vendor_offers as vo

log = logging.getLogger(__name__)

# Our internal vendor name (vo_vendors.vendor_name) -> dbo.vendor_accounts.id.
# Looked up live 2026-09-23 via `SELECT id,name FROM vendor_accounts WHERE name
# LIKE ...` for each of vo.VENDOR_NAMES. Victory has no file-upload parser
# (services.vendor_offers.VENDOR_LOADERS) but IS DB-syncable -- this sync is
# the first way to get Victory offers into the desk at all.
AZURE_VENDOR_ACCOUNT_ID: dict[str, int] = {
    "Quality King Distributors": 171,
    "Cencora": 45,
    "Diamond Wholesale": 173,
    "BILO": 187,
    "Victory Wholesale Grocers": 172,
}

_QUERY = """
SELECT
    l.vendor_sku, p.manufacturer_part_number, l.vendor_price_in_currency,
    pp.uom_quantity, l.quantity_available_in_uom, l.vendor_title, l.updated_at
FROM vendor_account_product_listings l
LEFT JOIN packaged_products pp ON pp.id = l.packaged_product_id
LEFT JOIN products p ON p.id = pp.product_id
WHERE l.vendor_account_id = %s
  AND l.trashed_at IS NULL
  AND l.discontinued_at IS NULL
  AND l.currency_id = 1
  AND (l.expires_at IS NULL OR l.expires_at > SYSUTCDATETIME())
  AND (l.activates_at IS NULL OR l.activates_at <= SYSUTCDATETIME())
"""


def _valid_upc_a(raw) -> str | None:
    """norm_upc() the value, then require it to be a genuine checksum-valid
    12-digit UPC-A -- rejects internal part numbers that merely happen to be
    all-digit (e.g. Cencora's ABC warehouse codes). Delegates the actual
    checksum check to vo.is_valid_upc_a() -- the SAME validator now also
    applied to the file-upload loaders (services/vendor_offers.py), added
    2026-09-24 after a Diamond CSV item's fake '12DGTUPC' code silently
    orphaned its offers from every other vendor's real-UPC listing for the
    same product. One validator, not two copies that could drift."""
    digits = vo.norm_upc(raw)
    return digits if vo.is_valid_upc_a(raw) else None


def fetch_vendor_rows(vname: str) -> tuple[list, list]:
    """Pull `vname`'s active offers from Azure -> [(stamp, rows)] batches (one
    per distinct updated_at date, so the existing date-guarded UPSERT_SQL merge
    semantics apply exactly as they do to a file upload) + a skip log."""
    azure_id = AZURE_VENDOR_ACCOUNT_ID.get(vname)
    if azure_id is None:
        raise ValueError(f"{vname!r} has no Azure vendor_accounts mapping.")

    conn = azure_sql._connect()
    try:
        cur = conn.cursor()
        cur.execute(_QUERY, (azure_id,))
        raw_rows = cur.fetchall()
    finally:
        conn.close()

    by_date: dict[str, list] = {}
    skipped = []
    for sku, mpn, cost, qty_case, avail, title, updated_at in raw_rows:
        upc = _valid_upc_a(mpn)
        cost_f = float(cost) if cost is not None else None
        reason = None
        if upc is None:
            reason = "manufacturer_part_number isn't a valid UPC-A"
        elif cost_f is None or cost_f <= 0:
            reason = "missing or non-positive vendor_price_in_currency"
        if reason:
            skipped.append({"vendor": vname, "item": sku, "description": title, "reason": reason})
            continue
        stamp = (updated_at.date() if isinstance(updated_at, dt.datetime) else
                  updated_at or dt.date.today()).isoformat() if updated_at else dt.date.today().isoformat()
        by_date.setdefault(stamp, []).append({
            "vendor_item_id": (sku or "").strip() or None, "upc": upc, "cost": cost_f,
            "qty_per_case": int(qty_case) if qty_case not in (None, "") else None,
            "avail_qty": float(avail) if avail not in (None, "") else None,
            "description": (title or "").strip() or None, "seen": stamp,
        })

    batches = [(stamp, rows) for stamp, rows in by_date.items()]
    return batches, skipped


def sync_vendor(vname: str) -> dict:
    """Pull + merge one vendor's live Azure offers. Returns the same shape as
    vo.ingest_vendor_file (+ azure_rows / valid_rows for diagnostics)."""
    vo.init_schema()
    batches, skipped = fetch_vendor_rows(vname)
    azure_rows = sum(len(rows) for _, rows in batches) + len(skipped)
    valid_rows = sum(len(rows) for _, rows in batches)
    result = vo.merge_batches(vname, batches, skipped)
    result["azure_rows"] = azure_rows
    result["valid_rows"] = valid_rows
    return result


def sync_all(vendors: list[str] | None = None) -> dict:
    """Best-effort sync of every mapped vendor (default: all of
    AZURE_VENDOR_ACCOUNT_ID). One vendor's failure doesn't block the rest.
    Also clears avail_qty on any offer (any vendor, any source -- file upload
    or DB sync) that's gone stale (services.vendor_offers.STALE_AVAIL_QTY_DAYS)
    -- an old quantity is no more trustworthy than an old price, and this piggy
    -backs on the same hourly cadence rather than needing its own scheduler."""
    names = vendors or list(AZURE_VENDOR_ACCOUNT_ID)
    results, errors = {}, {}
    for v in names:
        try:
            results[v] = sync_vendor(v)
        except Exception as exc:  # noqa: BLE001
            log.exception("[vendor_offers_db_sync] %s failed", v)
            errors[v] = str(exc)[:300]
    try:
        stale = vo.clear_stale_available_qty()
    except Exception:  # noqa: BLE001
        log.exception("[vendor_offers_db_sync] stale avail_qty cleanup failed")
        stale = {"cleared": 0, "cutoff": None}
    finished = dt.datetime.utcnow().isoformat()
    summary = {"finished_at": finished, "results": results, "errors": errors, "stale_avail_qty": stale}
    try:
        database.set_setting("vendor_offers_db_sync_last", __import__("json").dumps(summary))
    except Exception:  # noqa: BLE001
        log.warning("[vendor_offers_db_sync] failed to persist sync status", exc_info=True)
    return summary


def last_sync_status() -> dict | None:
    try:
        raw = database.get_setting("vendor_offers_db_sync_last")
    except Exception:  # noqa: BLE001
        return None
    if not raw:
        return None
    try:
        return __import__("json").loads(raw)
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Hourly in-process scheduler (mirrors services/health_check.py's pattern)
# --------------------------------------------------------------------------

_thread: threading.Thread | None = None
_stop = threading.Event()
_running = threading.Event()
INTERVAL_SECONDS = 3600


def _loop() -> None:
    while not _stop.is_set():
        _running.set()
        try:
            sync_all()
        except Exception:  # noqa: BLE001
            log.exception("[vendor_offers_db_sync] scheduled sync failed")
        finally:
            _running.clear()
        if _stop.wait(INTERVAL_SECONDS):
            return


def start_scheduler() -> None:
    """Start the hourly sync thread (idempotent). Called on app startup."""
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="vendor-offers-db-sync", daemon=True)
    _thread.start()
    log.info("[vendor_offers_db_sync] hourly scheduler started")


def stop_scheduler() -> None:
    _stop.set()


def scheduler_status() -> dict:
    last = last_sync_status()
    next_run = None
    if last and last.get("finished_at"):
        try:
            last_dt = dt.datetime.fromisoformat(last["finished_at"])
            next_run = (last_dt + dt.timedelta(seconds=INTERVAL_SECONDS)).isoformat()
        except Exception:  # noqa: BLE001
            pass
    return {
        "running_now": _running.is_set(),
        "scheduler_alive": bool(_thread and _thread.is_alive()),
        "interval_seconds": INTERVAL_SECONDS,
        "next_run": next_run,
        "last": last,
        "vendors": list(AZURE_VENDOR_ACCOUNT_ID),
    }
