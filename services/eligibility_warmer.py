"""
Idle-time eligibility warmer for the Price Desk (2026-10-28, per user: "when the
sp api isnt used (preferred pharmacy one) we can silently run the eligibility
check for all the asins stored on the price desk").

Keeps `eligibility_cache` (services/restrictions.py, 14-day TTL) filled for every
ASIN the Price Desk export / auto opportunity alert could need -- the Pair-Library
assignments (vo_asin_by_upc) plus the Keepa-barcode ASINs of every UPC on the desk,
the same union build_template_xlsx expands UPCs into -- so those runs mostly hit
the cache instead of calling getListingsRestrictions per ASIN.

"Silently" is enforced via the default store's rate limiter: it only checks while
no real caller has touched the default store's SP-API buckets (catalog search,
getCatalogItem or restrictions) for IDLE_SECONDS, and pauses the moment one does.
Its own calls run inside rate_limiter.background_use(), so they don't count as
activity. It also paces itself to RATE_PER_SEC (below Amazon's 5/s for this
operation) so a user starting a job mid-check still has headroom.

Order: ASINs with no cache entry first, then the oldest; entries are refreshed
once older than REFRESH_AFTER_DAYS (before the 14-day TTL lapses). Only writes
the export-only cache -- the Toolbox's own Eligibility Check stays fully live.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time

from services import database

log = logging.getLogger(__name__)

IDLE_SECONDS = 120          # quiet time on the default store before checking
RATE_PER_SEC = 3.0
REFRESH_AFTER_DAYS = 11     # eligibility_cache TTL is 14 days
REBUILD_EVERY = 3600        # re-read the desk's ASIN list at least hourly
FLUSH_EVERY = 25            # cache writes per DB transaction

_thread: threading.Thread | None = None
_stop = threading.Event()
_state: dict = {
    "phase": "waiting for its startup slot (+630s)", "queue": 0, "checked": 0, "errors": 0,
    "desk_asins": 0, "last_checked_at": None, "last_error": None,
}


def _desk_asins() -> list[str]:
    from services import vendor_offers_template_export as tpl
    with database._connect() as conn:
        asins = {r[0] for r in conn.execute(
            "SELECT DISTINCT asin FROM vo_asin_by_upc WHERE asin IS NOT NULL")}
        upcs = [r[0] for r in conn.execute("SELECT DISTINCT upc FROM vo_vendor_offers")]
    try:
        bmap = tpl._barcode_to_asins({tpl._bkey(u) for u in upcs if tpl._bkey(u)})
        asins |= {a for lst in bmap.values() for a in lst}
    except Exception as exc:  # noqa: BLE001 -- Azure down: still warm the assigned ones
        log.warning("[eligibility_warmer] keepa barcode lookup failed: %s", str(exc)[:120])
    return sorted({a.strip().upper() for a in asins if a and a.strip()})


def _due(asins: list[str]) -> list[str]:
    """Never-cached ASINs first, then cached ones older than REFRESH_AFTER_DAYS, oldest first."""
    from services.restrictions import _ensure_eligibility_cache_table
    as_of: dict = {}
    with database._connect() as conn:
        _ensure_eligibility_cache_table(conn)
        for i in range(0, len(asins), 400):
            ch = asins[i:i + 400]
            q = ",".join("?" * len(ch))
            for a, ts in conn.execute(
                    f"SELECT asin, as_of FROM eligibility_cache WHERE asin IN ({q})", ch):
                as_of[a] = ts or ""
    cutoff = (dt.datetime.now() - dt.timedelta(days=REFRESH_AFTER_DAYS)).isoformat()
    missing = [a for a in asins if a not in as_of]
    stale = sorted((a for a in asins if a in as_of and as_of[a] < cutoff), key=lambda a: as_of[a])
    return missing + stale


def _loop() -> None:
    from services.restrictions import classify, eligibility_cache_put, get_restrictions_api
    from services.spapi.config import load_sp_api_credentials, sp_api_configured
    from services.spapi.rate_limiter import background_use, foreground_idle_seconds

    with background_use():
        while not _stop.is_set():
            if not sp_api_configured():
                _state["phase"] = "SP-API not configured"
                if _stop.wait(REBUILD_EVERY):
                    return
                continue
            try:
                _state["phase"] = "building list"
                desk = _desk_asins()
                queue = _due(desk)
                seller = load_sp_api_credentials().seller_id
                rapi = get_restrictions_api()
            except Exception as exc:  # noqa: BLE001
                log.exception("[eligibility_warmer] could not build the ASIN list")
                _state.update(phase="error", last_error=str(exc)[:200])
                if _stop.wait(600):
                    return
                continue
            _state.update(desk_asins=len(desk), queue=len(queue))
            built = time.monotonic()
            pending: dict = {}

            def flush():
                if pending:
                    eligibility_cache_put(dict(pending))
                    pending.clear()

            for asin in queue:
                if _stop.is_set() or time.monotonic() - built > REBUILD_EVERY:
                    break
                while (idle := foreground_idle_seconds()) < IDLE_SECONDS:
                    flush()
                    _state["phase"] = "paused (SP-API in use)"
                    if _stop.wait(min(30.0, max(1.0, IDLE_SECONDS - idle))):
                        flush()
                        return
                _state["phase"] = "checking"
                try:
                    cls = classify(asin, rapi.get_restrictions(asin, seller))
                    pending[asin] = "DOG" if cls.get("dog") else cls["status"]
                    _state["checked"] += 1
                    _state["last_checked_at"] = dt.datetime.now().isoformat(timespec="seconds")
                except PermissionError as exc:   # 403: app lacks the Listings role -- don't hammer
                    _state.update(phase="error", last_error=str(exc)[:200])
                    flush()
                    if _stop.wait(REBUILD_EVERY):
                        return
                    break
                except Exception as exc:  # noqa: BLE001 -- one bad ASIN, keep going
                    _state["errors"] += 1
                    _state["last_error"] = f"{asin}: {str(exc)[:150]}"
                _state["queue"] = max(0, _state["queue"] - 1)
                if len(pending) >= FLUSH_EVERY:
                    flush()
                if _stop.wait(1 / RATE_PER_SEC):
                    flush()
                    return
            flush()
            if not _stop.is_set() and _state["queue"] == 0:
                _state["phase"] = "up to date"
                if _stop.wait(REBUILD_EVERY):
                    return


def start_scheduler() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="eligibility-warmer", daemon=True)
    _thread.start()
    log.info("[eligibility_warmer] started (idle-only, %.0f/s)", RATE_PER_SEC)


def stop_scheduler() -> None:
    _stop.set()


def scheduler_status() -> dict:
    s = dict(_state)
    summary = f"{s['phase']} — {s['checked']} checked this session, {s['queue']} due of {s['desk_asins']} desk ASINs"
    if s["errors"]:
        summary += f", {s['errors']} errors"
    return {
        "running_now": s["phase"] == "checking",
        "scheduler_alive": bool(_thread and _thread.is_alive()),
        "summary": summary,
        **s,
    }
