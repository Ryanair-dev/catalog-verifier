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
    "Purchaser", "SOURCE_LEAD", "CompanyName",
]

# Create SKUs must only consider the user's OWN company's catalog — a brand/SKU that
# lives only under another company (or none) is treated as new. The wizard's company
# label maps to the CompanyName stored in the view.
_COMPANY_NAME = {"Ford Medical": "Ford Medical, LLC", "Turba": "Turba"}


def _co_key(s) -> str:
    return "".join(c for c in str(s or "").lower() if c.isalnum())


def _rows_for_company(rows: list[dict], company: str) -> list[dict]:
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

    conn = _connect()
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT {', '.join(_COLS)} FROM {_VIEW}")
        pos = {d[0]: i for i, d in enumerate(cur.description)}

        def g(row, name):
            i = pos.get(name)
            return row[i] if i is not None else None

        rows = [{
            "ProductID": _s(g(r, "ProductID")), "UPC": _s(g(r, "UPC")),
            "ManufacturerSKU": _s(g(r, "ManufacturerSKU")),
            "ManufacturerName": _s(g(r, "Manufacturer")),
            "BrandName": _s(g(r, "BrandName")), "ASIN": _s(g(r, "ASIN")),
            "ShadowOf": _s(g(r, "ShadowOf")), "QtyPerCase": _s(g(r, "QtyPerCase")),
            "CostPerCase": _s(g(r, "CostPerCase")), "FulfilledBy": _s(g(r, "FulfilledBy")),
            "ProductGroupName": _s(g(r, "ProductGroupName")),
            "CompanyName": _s(g(r, "CompanyName")),
            "_purchaser": _s(g(r, "Purchaser")), "_sourcer": _s(g(r, "SOURCE_LEAD")),
        } for r in cur.fetchall()]
    finally:
        conn.close()

    with _LOCK:
        _CACHE["rows"], _CACHE["at"] = rows, time.time()
    return rows


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
        if r["_purchaser"] and r["_purchaser"] != "0":
            e["pur"][r["_purchaser"]] += 1
        if r["_sourcer"] and r["_sourcer"] != "0":
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
