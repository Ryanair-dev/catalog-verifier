"""
Live SellerCloud catalog via Azure SQL — READ-ONLY reference source.

Reads FordMed's `[analytics].[sku_data_extended_view]` (a live mirror of the
SellerCloud catalog, ~176k rows) and turns it into the same shapes Create SKUs
already consumes:
  * a `CatalogIndex` (existence by UPC/MPN, shadows by ASIN, per-brand prefix)
  * a brand map  { brand_lower: {brand, manufacturer, purchaser, sourcer} }

This is reference-only: we PULL data to check/derive SKUs — we never write to
SellerCloud from here. Rows are pulled once and cached in memory (30-min TTL);
callers fall back to the local snapshot (catalog_index.local_index / brand_map)
when Azure isn't configured or reachable.

Connection: pure-Python pytds + an AAD token from a service principal
(ClientSecretCredential). No ODBC driver needed. `urllib3` IPv6 is disabled just
before the token call to avoid the known login.microsoftonline.com IPv6 stall.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import Counter, defaultdict

from services.brand_map import fordmed_email
from services.sellercloud.catalog_index import CatalogIndex, build_from_rows

log = logging.getLogger(__name__)

_VIEW = "[analytics].[sku_data_extended_view]"
_COLS = [
    "ProductID", "UPC", "ManufacturerSKU", "Manufacturer", "BrandName", "ASIN",
    "ShadowOf", "QtyPerCase", "CostPerCase", "FulfilledBy", "ProductGroupName",
    "Purchaser", "SOURCE_LEAD", "CompanyName", "Status",
]
# Columns that MAY exist in the view — pulled when present, skipped via a graceful
# retry when the SELECT rejects them, so a wrong column name can never break the pull.
_OPTIONAL_COLS = ["ProductName"]

# Create SKUs must only consider the user's OWN company's catalog — a brand/SKU that
# lives only under another company (or none) is treated as new. The wizard's company
# label maps to the CompanyName stored in the view.
_COMPANY_NAME = {"Ford Medical": "Ford Medical, LLC", "Turba": "Turba"}

# Turba's catalog is mostly an ADDITIVE OVERLAY on top of Ford Medical's, not a fully
# separate one: confirmed live (2026-10-08) that most of a shared product's real MAIN
# SKU lives under CompanyName='Ford Medical, LLC', while only Turba's OWN listings --
# notably its '-FBATRB'/'-FBMTRB' shadows -- are tagged CompanyName='Turba' (only 33 of
# Turba's 1,417 rows are genuinely standalone Turba-only mains). A Turba batch scoped
# to 'Turba' alone therefore can't see either its own shared main (wrongly flagged
# "new") or, if scoped to 'Ford Medical' alone instead, its own already-existing
# -FBATRB/-FBMTRB shadows (the actual bug found: the collision-avoidance ladder
# silently minted FBA SKU names that were already assigned to a DIFFERENT ASIN,
# because that existing shadow lives under CompanyName='Turba', invisible to a
# Ford-Medical-only pull). A Turba batch needs the UNION of both scopes. Ford Medical
# itself stays scoped to its own company only, by the original design intent (a
# brand that lives only under Turba should still be "new" for a Ford Medical batch).
_COMPANY_UNION = {"Turba": ("Ford Medical, LLC", "Turba")}


def _co_key(s) -> str:
    return "".join(c for c in str(s or "").lower() if c.isalnum())


def _rows_for_company(rows: list[dict], company: str) -> list[dict]:
    if company in _COMPANY_UNION:
        targets = {_co_key(c) for c in _COMPANY_UNION[company]}
        return [r for r in rows if _co_key(r.get("CompanyName")) in targets]
    target = _co_key(_COMPANY_NAME.get(company, company))
    if not target:
        return rows
    return [r for r in rows if _co_key(r.get("CompanyName")) == target]

_CACHE_TTL = 1800.0   # seconds; re-pull the view at most this often
_LOCK = threading.Lock()
_CACHE: dict = {"at": 0.0, "rows": None}

_ENV_KEYS = ("AZURE_CLIENT_ID", "AZURE_CLIENT_SECRET", "AZURE_TENANT_ID",
             "AZURE_SQL_SERVER_HOST", "AZURE_SQL_SERVER_DATABASE")


def is_configured() -> bool:
    return all(os.getenv(k) for k in _ENV_KEYS)


def _s(v) -> str:
    return "" if v is None else str(v).strip()


def _connect():
    import urllib3.util.connection as uc
    uc.HAS_IPV6 = False   # else azure-identity stalls on login.microsoftonline.com
    import certifi
    import pytds
    from azure.identity import ClientSecretCredential

    cred = ClientSecretCredential(
        os.getenv("AZURE_TENANT_ID"), os.getenv("AZURE_CLIENT_ID"),
        os.getenv("AZURE_CLIENT_SECRET"),
    )
    return pytds.connect(
        server=os.getenv("AZURE_SQL_SERVER_HOST"), port=1433,
        database=os.getenv("AZURE_SQL_SERVER_DATABASE"),
        access_token_callable=lambda: cred.get_token(
            "https://database.windows.net/.default").token,
        cafile=certifi.where(), validate_host=False,
        login_timeout=30, timeout=180,
    )


def fetch_rows(force: bool = False) -> list[dict]:
    """Pull + cache the view rows. Keys match `build_from_rows` inputs plus
    `_purchaser` / `_sourcer` for the brand map."""
    with _LOCK:
        cached = _CACHE["rows"]
        if not force and cached is not None and (time.time() - _CACHE["at"]) < _CACHE_TTL:
            return cached

    def _pull(cols: list[str]) -> list[dict]:
        conn = _connect()
        try:
            cur = conn.cursor()
            cur.execute(f"SELECT {', '.join(cols)} FROM {_VIEW}")
            pos = {d[0]: i for i, d in enumerate(cur.description)}

            def g(row, name):
                i = pos.get(name)
                return row[i] if i is not None else None

            return [{
                "ProductID": _s(g(r, "ProductID")), "UPC": _s(g(r, "UPC")),
                "ManufacturerSKU": _s(g(r, "ManufacturerSKU")),
                "ManufacturerName": _s(g(r, "Manufacturer")),
                "BrandName": _s(g(r, "BrandName")), "ASIN": _s(g(r, "ASIN")),
                "ShadowOf": _s(g(r, "ShadowOf")), "QtyPerCase": _s(g(r, "QtyPerCase")),
                "CostPerCase": _s(g(r, "CostPerCase")), "FulfilledBy": _s(g(r, "FulfilledBy")),
                "ProductGroupName": _s(g(r, "ProductGroupName")),
                "ProductName": _s(g(r, "ProductName")),   # "" when the column isn't pulled
                "CompanyName": _s(g(r, "CompanyName")),
                "Status": _s(g(r, "Status")),   # "Active" / "Disabled" -- a Disabled row
                                                 # is never a real reference match (2026-10-07)

                "_purchaser": _s(g(r, "Purchaser")), "_sourcer": _s(g(r, "SOURCE_LEAD")),
            } for r in cur.fetchall()]
        finally:
            conn.close()

    try:
        rows = _pull(_COLS + _OPTIONAL_COLS)
    except Exception as exc:
        log.warning("[azure_sql] optional columns rejected (%s) — retrying without them",
                    str(exc)[:120])
        rows = _pull(_COLS)

    with _LOCK:
        _CACHE["rows"], _CACHE["at"] = rows, time.time()
    return rows


# ── hourly background refresh ────────────────────────────────────────────────
# The 30-min _CACHE_TTL above only refreshes WHEN something happens to call
# fetch_rows() after the TTL has expired -- between calls (e.g. the hours
# between two on-demand PO Analytics runs) the cache can sit stale well past
# 30 min with nothing proactively refreshing it. This forces a real pull once
# an hour regardless of demand, so any caller (PO Analytics, Create SKUs,
# sc_reference) almost always finds an already-warm cache instead of paying
# the ~165s pull cost inline. Mirrors services/vendor_offers_db_sync.py's
# exact hourly-scheduler shape.
INTERVAL_SECONDS = 3600
_sched_thread: threading.Thread | None = None
_sched_stop = threading.Event()
_sched_running = threading.Event()
_last_refresh: dict = {"at": None, "rows": None, "error": None}


def _sched_loop() -> None:
    while not _sched_stop.is_set():
        _sched_running.set()
        try:
            rows = fetch_rows(force=True)
            _last_refresh["at"], _last_refresh["rows"], _last_refresh["error"] = time.time(), len(rows), None
            # Keep the brand-prefix/manufacturer reference tables (Create SKUs,
            # PO Analytics' brand-label resolution) current too -- force=False
            # here reuses the rows just pulled above rather than a 2nd full pull.
            try:
                from services import sc_reference
                sc_reference.build_reference(force=False)
            except Exception:  # noqa: BLE001
                log.exception("[azure_sql] sc_reference refresh failed")
        except Exception as exc:  # noqa: BLE001
            _last_refresh["error"] = str(exc)[:300]
            log.exception("[azure_sql] scheduled catalog refresh failed")
        finally:
            _sched_running.clear()
        if _sched_stop.wait(INTERVAL_SECONDS):
            return


def start_scheduler() -> None:
    """Start the hourly Azure catalog refresh thread (idempotent)."""
    global _sched_thread
    if _sched_thread is not None and _sched_thread.is_alive():
        return
    _sched_stop.clear()
    _sched_thread = threading.Thread(target=_sched_loop, name="azure-sql-refresh", daemon=True)
    _sched_thread.start()
    log.info("[azure_sql] hourly catalog refresh scheduler started")


def stop_scheduler() -> None:
    _sched_stop.set()


def scheduler_status() -> dict:
    next_run = (_last_refresh["at"] + INTERVAL_SECONDS) if _last_refresh["at"] else None
    return {
        "running_now": _sched_running.is_set(),
        "scheduler_alive": bool(_sched_thread and _sched_thread.is_alive()),
        "interval_seconds": INTERVAL_SECONDS,
        "last_refresh_at": _last_refresh["at"],
        "last_refresh_rows": _last_refresh["rows"],
        "last_refresh_error": _last_refresh["error"],
        "next_run": next_run,
        "cache_age_seconds": (time.time() - _CACHE["at"]) if _CACHE["at"] else None,
    }


def catalog_index(company: str = "Ford Medical") -> CatalogIndex:
    return build_from_rows(_rows_for_company(fetch_rows(), company), built_at=time.time())


def brand_map(company: str = "Ford Medical") -> dict[str, dict]:
    """brand_lower -> {brand, manufacturer, purchaser, sourcer} (most-common
    non-blank value per brand), restricted to `company`'s SKUs."""
    agg: dict = defaultdict(lambda: {"brand": "", "mfr": Counter(), "pur": Counter(), "src": Counter()})
    for r in _rows_for_company(fetch_rows(), company):
        b = r["BrandName"]
        if not b:
            continue
        e = agg[b.strip().lower()]
        if not e["brand"]:
            e["brand"] = b
        if r["ManufacturerName"]:
            e["mfr"][r["ManufacturerName"]] += 1
        # "Other" is a real SellerCloud placeholder (unassigned), not a person --
        # exclude it from the vote like "0" so it can never outvote an actual
        # purchaser/sourcer name just because more legacy rows were left unassigned.
        if r["_purchaser"] and r["_purchaser"] != "0" and r["_purchaser"].strip().lower() != "other":
            e["pur"][r["_purchaser"]] += 1
        if r["_sourcer"] and r["_sourcer"] != "0" and r["_sourcer"].strip().lower() != "other":
            e["src"][r["_sourcer"]] += 1

    def top(c: Counter) -> str:
        return c.most_common(1)[0][0] if c else ""

    return {k: {"brand": e["brand"], "manufacturer": top(e["mfr"]),
                "purchaser": fordmed_email(top(e["pur"])),
                "sourcer": fordmed_email(top(e["src"]))}
            for k, e in agg.items()}


def catalog_source(company: str = "Ford Medical") -> tuple[CatalogIndex, dict]:
    """(CatalogIndex, brand_map) from the live view, restricted to `company`'s SKUs
    — both derive from one cached pull. Raises on any connection/query failure so
    callers can fall back."""
    fetch_rows()   # prime the cache (one pull; all companies, filtered per call)
    return catalog_index(company), brand_map(company)


def find_split_pos(po_id: int) -> list[int]:
    """PO IDs that were split off FROM `po_id` -- i.e. every row in the live
    `sellercloud.Purchase` mirror whose `SplittedFromPOId` equals it. Confirmed
    live (2026-10-08, user-identified table): a cancelled "main" PO's real
    replacement/split PO records this parent link in `SplittedFromPOId` -- it's
    NOT exposed anywhere in the SellerCloud REST API's PO payload (checked:
    `RelatedItems`/`RelatedFbaId`/`RelatedPOFBAPlanningId` are all empty/zero
    even on a confirmed real split pair), so this direct SQL table is the only
    way to discover it. A small, uncached, on-demand query (not the big
    sku_data_extended_view pull) -- never raises; callers should catch and
    degrade (e.g. treat as "no splits found") on any connection problem, same
    as `catalog_source`."""
    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT ID FROM sellercloud.Purchase WHERE SplittedFromPOId = %s", (int(po_id),)
        )
        return [int(r[0]) for r in cur.fetchall()]
    finally:
        conn.close()
