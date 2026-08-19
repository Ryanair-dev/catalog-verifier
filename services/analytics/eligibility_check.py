"""
Eligibility (CAN_SELL / NEEDS_APPROVAL / RESTRICTED) + FBA storage-fee enrichment
for an analytics run's APPROVED + REVIEW candidates.

- Storage fee is computed from the Amazon dimensions ALREADY stored on each candidate
  (`data_json.amazon._raw.attributes`) — no extra API call.
- Eligibility is one live `getListingsRestrictions` call per UNIQUE ASIN (deduped across
  rows), so only the Approved/Review slice is queried — the fast path.

Progress → analytics_runs.elig_check_status/done/total. Results → analytics_candidates
.eligibility_status / storage_fee / storage_fee_peak (written to EVERY row sharing the
ASIN), so the run detail + export can show them.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed

from services import database
from services.storage_fees import calc_storage_fee, extract_dimensions

log = logging.getLogger(__name__)

WORKERS = 5   # getListingsRestrictions is rate-limited server-side too

_RUNNING: dict[int, bool] = {}
_STOP: dict[int, threading.Event] = {}
_LOCK = threading.Lock()


def is_running(run_id: int) -> bool:
    with _LOCK:
        return bool(_RUNNING.get(run_id))


def stop_eligibility_check(run_id: int) -> None:
    with _LOCK:
        ev = _STOP.get(run_id)
    if ev:
        ev.set()


def approved_review_asins(run_id: int) -> int:
    """Count of DISTINCT Approved/Review ASINs — the number of eligibility calls."""
    with database._connect() as conn:
        row = conn.execute(
            "SELECT COUNT(DISTINCT asin) FROM analytics_candidates "
            "WHERE run_id=? AND verdict IN ('verified','review') AND asin<>''",
            (run_id,),
        ).fetchone()
    return int(row[0] or 0)


def start_eligibility_check(run_id: int) -> None:
    """Launch the enrichment in a background daemon thread. No-op if already running."""
    with _LOCK:
        if _RUNNING.get(run_id):
            return
        _RUNNING[run_id] = True
        ev = _STOP.get(run_id)
        if ev:
            ev.clear()
        else:
            _STOP[run_id] = threading.Event()
    threading.Thread(target=_pipeline, args=(run_id,), daemon=True).start()


def _set_status(run_id: int, status: str, done: int, total: int) -> None:
    with database._LOCK, database._connect() as conn:
        conn.execute(
            "UPDATE analytics_runs SET elig_check_status=?, elig_check_done=?, "
            "elig_check_total=? WHERE id=?",
            (status, done, total, run_id),
        )


def _write_asin(run_id: int, asin: str, status: str | None,
                fee: tuple | None) -> None:
    off = fee[0] if fee else None
    peak = fee[1] if fee else None
    with database._LOCK, database._connect() as conn:
        conn.execute(
            "UPDATE analytics_candidates SET eligibility_status=?, storage_fee=?, "
            "storage_fee_peak=? WHERE run_id=? AND asin=? AND verdict IN ('verified','review')",
            (status, off, peak, run_id, asin),
        )


def _storage_for(data_json: str) -> tuple | None:
    """(off_peak, peak) monthly per-unit storage fee from the stored Amazon dims, or None."""
    try:
        data = json.loads(data_json or "{}")
        attrs = ((data.get("amazon") or {}).get("_raw") or {}).get("attributes") or {}
        dims = extract_dimensions(attrs)
        if all(dims.get(k) is not None for k in ("length_cm", "width_cm", "height_cm", "weight_g")):
            return calc_storage_fee(dims["length_cm"], dims["width_cm"],
                                    dims["height_cm"], dims["weight_g"])
    except Exception:
        pass
    return None


def _pipeline(run_id: int) -> None:
    try:
        _set_status(run_id, "Running", 0, 0)

        with database._connect() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT asin, data_json FROM analytics_candidates "
                "WHERE run_id=? AND verdict IN ('verified','review')",
                (run_id,),
            ).fetchall()

        if not rows:
            _set_status(run_id, "Done", 0, 0)
            return

        # group by ASIN; storage fee is per-product so compute it once per ASIN
        by_asin: dict[str, list] = defaultdict(list)
        for r in rows:
            by_asin[(r["asin"] or "").strip()].append(r)
        asins = [a for a in by_asin if a]
        storage = {a: _storage_for(by_asin[a][0]["data_json"]) for a in asins}

        # seller id + restrictions API (eligibility). If unavailable, still write storage.
        try:
            from services.spapi.config import load_sp_api_credentials
            creds = load_sp_api_credentials()
            seller_id = creds.seller_id or ""
        except Exception as exc:
            log.warning("[eligibility] SP-API creds unavailable: %s", exc)
            seller_id = ""

        total = len(asins)
        _set_status(run_id, "Running", 0, total)
        ev = _STOP.get(run_id)
        done = 0

        if not seller_id:
            # No seller id / SP-API → storage only; flag eligibility as unavailable.
            for a in asins:
                _write_asin(run_id, a, "UNAVAILABLE", storage.get(a))
                done += 1
            _set_status(run_id, "Done (storage only — set AMZ_SELLER_ID for eligibility)",
                        done, total)
            return

        from services.restrictions import classify, get_restrictions_api

        def _check(asin: str) -> tuple[str, str]:
            try:
                raw = get_restrictions_api().get_restrictions(asin, seller_id)
                return asin, classify(asin, raw)["status"]
            except PermissionError:
                return asin, "ERROR (Listings role?)"
            except Exception as exc:  # noqa: BLE001
                log.warning("[eligibility] %s failed: %s", asin, str(exc)[:120])
                return asin, "ERROR"

        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {pool.submit(_check, a): a for a in asins}
            for fut in as_completed(futures):
                if ev and ev.is_set():
                    break
                asin, status = fut.result()
                _write_asin(run_id, asin, status, storage.get(asin))
                done += 1
                if done % WORKERS == 0 or done == total:
                    _set_status(run_id, "Running", done, total)

        stopped = bool(ev and ev.is_set())
        _set_status(run_id, "Stopped" if stopped else "Done", done, total)
    except Exception as exc:  # noqa: BLE001
        log.exception("[eligibility] pipeline failed")
        _set_status(run_id, f"Error: {exc}", 0, 0)
    finally:
        with _LOCK:
            _RUNNING[run_id] = False
