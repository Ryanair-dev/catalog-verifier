"""
SP-API Reports — GET_SALES_AND_TRAFFIC_REPORT → units ordered per ASIN.

The report covers the seller's WHOLE catalog for a date range (not per-ASIN), so
`get_units_30d()` generates it at most once/day and caches the {asin: unitsOrdered}
map (JSON in the settings kv table). Requires the SP-API app's Reports/"Brand
Analytics"/Selling-Partner-Insights role; on any failure it returns {} and callers
fall back to their other source.
"""
from __future__ import annotations

import datetime as dt
import gzip
import json
import logging
import threading
import time

import requests

from services import database
from services.spapi.auth import LWATokenManager
from services.spapi.config import load_sp_api_credentials, sp_api_configured

log = logging.getLogger(__name__)

ENDPOINT = "https://sellingpartnerapi-na.amazon.com"
_REPORT_TYPE = "GET_SALES_AND_TRAFFIC_REPORT"
_CACHE_JSON = "sales_traffic_30d_json"
_CACHE_AT = "sales_traffic_30d_at"
_LOCK = threading.Lock()


def _ctx():
    creds = load_sp_api_credentials()
    tm = LWATokenManager(client_id=creds.client_id, client_secret=creds.client_secret,
                         refresh_token=creds.refresh_token)
    return creds, tm


def _req(method: str, path: str, tm, **kw) -> requests.Response:
    for attempt in range(6):
        try:
            resp = requests.request(
                method, ENDPOINT + path,
                headers={"x-amz-access-token": tm.get_token(), "Content-Type": "application/json"},
                timeout=60, **kw)
        except requests.RequestException as exc:
            time.sleep(min(30, 2 ** attempt))
            log.warning("[reports] net error %s: %s", path, str(exc)[:80])
            continue
        if resp.status_code in (429, 500, 502, 503, 504):
            time.sleep(min(30, 2 ** attempt))
            continue
        return resp
    raise RuntimeError("SP-API reports request failed after retries")


def fetch_sales_traffic_units(days: int = 30, poll_timeout: int = 300) -> dict:
    """Generate the report for the last `days` and return {asin: unitsOrdered}."""
    if not sp_api_configured():
        return {}
    creds, tm = _ctx()
    end = dt.date.today() - dt.timedelta(days=1)      # yesterday (a complete day)
    start = end - dt.timedelta(days=days)
    body = {
        "reportType": _REPORT_TYPE,
        "marketplaceIds": [creds.marketplace_id],
        "dataStartTime": f"{start.isoformat()}T00:00:00Z",
        "dataEndTime": f"{end.isoformat()}T00:00:00Z",
        "reportOptions": {"dateGranularity": "DAY", "asinGranularity": "CHILD"},
    }
    r = _req("POST", "/reports/2021-06-30/reports", tm, data=json.dumps(body))
    if r.status_code >= 400:
        raise RuntimeError(f"createReport {r.status_code}: {r.text[:200]}")
    report_id = r.json()["reportId"]

    doc_id = None
    deadline = time.time() + poll_timeout
    while time.time() < deadline:
        j = _req("GET", f"/reports/2021-06-30/reports/{report_id}", tm).json()
        status = j.get("processingStatus")
        if status == "DONE":
            doc_id = j.get("reportDocumentId")
            break
        if status in ("CANCELLED", "FATAL"):
            raise RuntimeError(f"report {status}")
        time.sleep(20)
    if not doc_id:
        raise RuntimeError("report generation timed out")

    dj = _req("GET", f"/reports/2021-06-30/documents/{doc_id}", tm).json()
    raw = requests.get(dj["url"], timeout=180).content
    if dj.get("compressionAlgorithm") == "GZIP":
        raw = gzip.decompress(raw)
    data = json.loads(raw.decode("utf-8"))

    units: dict = {}
    for row in data.get("salesAndTrafficByAsin", []):
        asin = (row.get("childAsin") or row.get("parentAsin") or "").strip().upper()
        u = (row.get("salesByAsin") or {}).get("unitsOrdered")
        if asin and u is not None:
            units[asin] = int(u)
    return units


def get_units_30d(max_age_hours: float = 24, generate: bool = True) -> dict:
    """Cached {asin: unitsOrdered (30d)}. Regenerates (once) when the cache is older
    than max_age_hours. Best-effort — returns {} (or the stale cache) on failure."""
    now = time.time()
    at = database.get_setting(_CACHE_AT)
    cached_json = database.get_setting(_CACHE_JSON)
    fresh = at and (now - float(at)) < max_age_hours * 3600
    if cached_json and (fresh or not generate):
        try:
            return json.loads(cached_json)
        except Exception:  # noqa: BLE001
            pass
    if not generate:
        return {}
    with _LOCK:
        # re-check inside the lock (another export may have just refreshed it)
        at = database.get_setting(_CACHE_AT)
        if at and (time.time() - float(at)) < max_age_hours * 3600:
            try:
                return json.loads(database.get_setting(_CACHE_JSON) or "{}")
            except Exception:  # noqa: BLE001
                pass
        try:
            units = fetch_sales_traffic_units()
        except Exception as exc:  # noqa: BLE001
            log.warning("[reports] sales & traffic fetch failed: %s", str(exc)[:150])
            try:
                return json.loads(cached_json) if cached_json else {}
            except Exception:  # noqa: BLE001
                return {}
        database.set_setting(_CACHE_JSON, json.dumps(units))
        database.set_setting(_CACHE_AT, str(time.time()))
        log.info("[reports] sales & traffic cached: %d ASINs", len(units))
        return units


def cache_status() -> dict:
    at = database.get_setting(_CACHE_AT)
    try:
        n = len(json.loads(database.get_setting(_CACHE_JSON) or "{}"))
    except Exception:  # noqa: BLE001
        n = 0
    return {"asins": n, "as_of": (dt.datetime.fromtimestamp(float(at)).isoformat() if at else None)}
