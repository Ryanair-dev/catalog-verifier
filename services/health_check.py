"""
Weekly catalog health check — Eligibility + DOG over the Pair Library ASINs.

Runs from INSIDE the app on a schedule (Friday 19:00 America/New_York, which
tracks EST/EDT automatically) and can also be triggered on demand.

Per ASIN it makes just TWO SP-API calls:
  • getCatalogItem (productTypes,summaries,attributes,salesRanks) — one call that
    yields DOG status (404 → dead/delisted ASIN), the Amazon brand + manufacturer,
    AND the package dimensions → FBA storage fee. (Doing DOG and dimensions in the
    same catalog call is the consolidation the storage/DOG checks were missing.)
  • getListingsRestrictions — CAN_SELL / NEEDS_APPROVAL / RESTRICTED eligibility.

Results are written to health_check_runs (one summary row) + health_check_results
(one row per ASIN). Brand/manufacturer are backfilled into the Pair Library from
Amazon's summaries (accurate, unlike the best-effort brand captured at import).
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo

import requests

from services import database
from services.restrictions import classify, get_restrictions_api
from services.spapi.client import get_catalog_api
from services.spapi.config import load_sp_api_credentials, sp_api_configured
from services.storage_fees import calc_storage_fee, extract_dimensions

log = logging.getLogger(__name__)

_TZ = ZoneInfo("America/New_York")   # 7pm ET, DST-aware
WORKERS = 5
_INCLUDED = "productTypes,summaries,attributes,salesRanks"

SCHEMA = """
CREATE TABLE IF NOT EXISTS health_check_runs (
    run_id         TEXT PRIMARY KEY,
    started_at     TEXT,
    finished_at    TEXT,
    trigger        TEXT,          -- 'scheduled' | 'manual'
    total          INTEGER,
    dog            INTEGER,
    can_sell       INTEGER,
    needs_approval INTEGER,
    restricted     INTEGER,
    errors         INTEGER
);
CREATE TABLE IF NOT EXISTS health_check_results (
    run_id           TEXT,
    asin             TEXT,
    checked_at       TEXT,
    dog              INTEGER,      -- 1 = DOG (404), 0 = live, NULL = unknown/error
    eligibility      TEXT,         -- CAN_SELL | NEEDS_APPROVAL | RESTRICTED | ERROR
    reasons          TEXT,
    brand            TEXT,
    manufacturer     TEXT,
    storage_fee      REAL,
    storage_fee_peak REAL,
    detail           TEXT,
    PRIMARY KEY (run_id, asin)
);
CREATE INDEX IF NOT EXISTS idx_hcr_asin ON health_check_results(asin);
"""


def init_schema() -> None:
    with database._LOCK, database._connect() as conn:
        conn.executescript(SCHEMA)


def _pair_library_asins() -> list[str]:
    with database._connect() as conn:
        try:
            return [r[0] for r in conn.execute("SELECT asin FROM pair_library ORDER BY asin")]
        except Exception:
            return []


def _check_one(asin: str, seller_id: str, rapi, capi) -> dict:
    r = {"asin": asin, "dog": None, "eligibility": None, "reasons": "",
         "brand": None, "manufacturer": None,
         "storage_fee": None, "storage_fee_peak": None, "detail": ""}

    # 1) catalog: DOG (404) + brand/manufacturer + dimensions → storage fee (ONE call)
    try:
        raw = capi.get_by_asin(asin, included_data=_INCLUDED)
        r["dog"] = 0
        summ = (raw.get("summaries") or [{}])[0]
        r["brand"] = summ.get("brand")
        r["manufacturer"] = summ.get("manufacturer")
        dims = extract_dimensions(raw.get("attributes") or {})
        off, peak = calc_storage_fee(dims["length_cm"], dims["width_cm"],
                                     dims["height_cm"], dims["weight_g"])
        r["storage_fee"], r["storage_fee_peak"] = off, peak
    except requests.HTTPError as exc:
        if getattr(exc.response, "status_code", None) == 404:
            r["dog"] = 1        # not in the catalog = dead/delisted ASIN
        else:
            r["detail"] = f"catalog {exc}"[:150]
    except Exception as exc:  # noqa: BLE001
        r["detail"] = f"catalog {exc}"[:150]

    # 2) eligibility (recorded even for DOGs, so the report is complete)
    try:
        raw = rapi.get_restrictions(asin, seller_id)
        cl = classify(asin, raw)
        r["eligibility"] = cl["status"]
        r["reasons"] = "; ".join(x.get("message", "") for x in cl.get("reasons", []))[:500]
    except Exception as exc:  # noqa: BLE001
        r["eligibility"] = "ERROR"
        r["detail"] = (r["detail"] + f" | elig {exc}").strip(" |")[:200]

    return r


def run_health_check(asins: list[str] | None = None, trigger: str = "manual") -> dict:
    """Run Eligibility + DOG (+ dims/storage) over `asins` (default: the Pair
    Library). Persists a run summary + per-ASIN rows, backfills Pair-Library
    brand/manufacturer, and returns the summary."""
    init_schema()
    if not sp_api_configured():
        return {"error": "SP-API not configured"}

    if asins is None:
        asins = _pair_library_asins()
    asins = [a.strip().upper() for a in (asins or []) if a and a.strip()]
    if not asins:
        return {"error": "no ASINs to check (Pair Library is empty)"}

    if _running.is_set():
        return {"error": "a health check is already running"}

    run_id = uuid.uuid4().hex[:12]
    started = dt.datetime.now(_TZ)
    creds = load_sp_api_credentials()
    seller_id = creds.seller_id
    rapi = get_restrictions_api()
    capi = get_catalog_api()

    results: list[dict] = []
    _running.set()
    try:
        with ThreadPoolExecutor(max_workers=WORKERS) as ex:
            futs = [ex.submit(_check_one, a, seller_id, rapi, capi) for a in asins]
            for f in as_completed(futs):
                results.append(f.result())
    finally:
        _running.clear()

    finished = dt.datetime.now(_TZ)
    dog   = sum(1 for r in results if r["dog"] == 1)
    can   = sum(1 for r in results if r["eligibility"] == "CAN_SELL")
    need  = sum(1 for r in results if r["eligibility"] == "NEEDS_APPROVAL")
    restr = sum(1 for r in results if r["eligibility"] == "RESTRICTED")
    err   = sum(1 for r in results if r["eligibility"] == "ERROR" or r["detail"])

    with database._LOCK, database._connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO health_check_runs "
            "(run_id,started_at,finished_at,trigger,total,dog,can_sell,needs_approval,restricted,errors) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (run_id, started.isoformat(), finished.isoformat(), trigger,
             len(results), dog, can, need, restr, err))
        conn.executemany(
            "INSERT OR REPLACE INTO health_check_results "
            "(run_id,asin,checked_at,dog,eligibility,reasons,brand,manufacturer,"
            " storage_fee,storage_fee_peak,detail) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [(run_id, r["asin"], finished.isoformat(), r["dog"], r["eligibility"],
              r["reasons"], r["brand"], r["manufacturer"], r["storage_fee"],
              r["storage_fee_peak"], r["detail"]) for r in results])

    # Backfill accurate brand/manufacturer into the Pair Library (only overwrites
    # when Amazon actually returned a value; upsert_library_pair guards empties).
    for r in results:
        if r["brand"] or r["manufacturer"]:
            try:
                database.upsert_library_pair(
                    r["asin"], ids={}, brand=r["brand"] or "",
                    manufacturer=r["manufacturer"] or "")
            except Exception:  # noqa: BLE001
                pass

    log.info("[health_check] run %s (%s): %d ASINs — %d DOG, %d need-approval, "
             "%d restricted, %d can-sell, %d err",
             run_id, trigger, len(results), dog, need, restr, can, err)
    return {"run_id": run_id, "trigger": trigger, "total": len(results),
            "dog": dog, "can_sell": can, "needs_approval": need,
            "restricted": restr, "errors": err,
            "started_at": started.isoformat(), "finished_at": finished.isoformat()}


# --------------------------------------------------------------------------
# Scheduler — Friday 19:00 America/New_York, in-process
# --------------------------------------------------------------------------

_thread: threading.Thread | None = None
_stop = threading.Event()
_running = threading.Event()   # set while a run is in progress (manual or scheduled)


def next_run_time(now: dt.datetime | None = None) -> dt.datetime:
    """Next Friday 19:00 ET at or after `now`."""
    now = now or dt.datetime.now(_TZ)
    target = now.replace(hour=19, minute=0, second=0, microsecond=0)
    target += dt.timedelta(days=(4 - now.weekday()) % 7)   # Friday = weekday 4
    if target <= now:
        target += dt.timedelta(days=7)
    return target


def _loop() -> None:
    while not _stop.is_set():
        target = next_run_time()
        log.info("[health_check] next scheduled run: %s", target.isoformat())
        while not _stop.is_set():
            remaining = (target - dt.datetime.now(_TZ)).total_seconds()
            if remaining <= 0:
                break
            if _stop.wait(min(remaining, 300)):   # re-check every 5 min (DST/clock)
                return
        if _stop.is_set():
            return
        try:
            run_health_check(trigger="scheduled")
        except Exception:  # noqa: BLE001
            log.exception("[health_check] scheduled run failed")
        _stop.wait(120)   # step past 19:00 before recomputing the next Friday


def start_scheduler() -> None:
    """Start the weekly scheduler thread (idempotent). Called on app startup."""
    global _thread
    if _thread and _thread.is_alive():
        return
    init_schema()
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="health-check-scheduler", daemon=True)
    _thread.start()
    log.info("[health_check] weekly scheduler started (Fri 19:00 America/New_York)")


def stop_scheduler() -> None:
    _stop.set()


def scheduler_status() -> dict:
    return {
        "running_now": _running.is_set(),
        "scheduler_alive": bool(_thread and _thread.is_alive()),
        "next_run": next_run_time().isoformat(),
        "timezone": "America/New_York",
    }
