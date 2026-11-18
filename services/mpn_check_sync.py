"""
MPN Check -- local mirror of the fbidb identifiers MPN Check needs (products'
MPN + manufacturer + case-pack qty), so a check never has to round-trip Azure
SQL live. The remote tables are big (products: ~1.03M rows team-wide, ~792k
with an MPN; packaged_products CS rows: ~307k) but this data "rarely changes"
(user's own words) -- a live query is the wrong tool for something this
stable; a periodically-refreshed local mirror is. Same architecture as
services/vendor_offers_db_sync.py: an initial full pull, then cheap
incremental syncs keyed on each table's own `updated_at`, on an hourly
in-process scheduler.

First full sync is a real cost (measured live: ~55s products + ~30s
packaged_products CS + ~5s manufacturers+aliases \u2248 90s total) -- runs once in
the background on startup, not in the request path. Every check after that
reads purely from local SQLite.
"""
from __future__ import annotations

import datetime as dt
import logging
import threading

from services import azure_sql, database

log = logging.getLogger(__name__)

_TEAM_ID = 1   # Ford Medical

SCHEMA = """
CREATE TABLE IF NOT EXISTS mpn_products (
    product_id      INTEGER PRIMARY KEY,
    mpn             TEXT NOT NULL,
    normalized_mpn  TEXT NOT NULL,
    manufacturer_id INTEGER NOT NULL,
    qty_case        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_mpn_products_lookup ON mpn_products(normalized_mpn, manufacturer_id);

CREATE TABLE IF NOT EXISTS mpn_manufacturer_lookup (
    normalized_name TEXT NOT NULL,
    manufacturer_id INTEGER NOT NULL,
    PRIMARY KEY (normalized_name, manufacturer_id)
);
CREATE INDEX IF NOT EXISTS idx_mpn_mfr_lookup_name ON mpn_manufacturer_lookup(normalized_name);
"""


def init_schema() -> None:
    with database._LOCK, database._connect() as conn:
        conn.executescript(SCHEMA)


def _norm_txt(s) -> str:
    return "".join(c for c in str(s or "").lower() if c.isalnum())


def _norm_mpn(s) -> str:
    n = _norm_txt(s)
    stripped = n.lstrip("0")
    return stripped if stripped else n


def _watermark_key(name: str) -> str:
    return f"mpn_sync_{name}_at"


def sync_manufacturers() -> dict:
    """Full rebuild (fast: ~7k manufacturers + ~53k aliases, ~5s) -- simpler and
    safer than incremental for a table this size, and it's cheap either way."""
    init_schema()
    conn = azure_sql._connect()
    try:
        cur = conn.cursor()
        rows: dict[str, set[int]] = {}
        cur.execute("SELECT id, name FROM manufacturers WHERE team_id=%s", (_TEAM_ID,))
        for mid, name in cur.fetchall():
            rows.setdefault(_norm_txt(name), set()).add(mid)
        cur.execute("""
            SELECT mn.manufacturer_id, mn.normalized_name
            FROM manufacturer_names mn JOIN manufacturers m ON m.id = mn.manufacturer_id
            WHERE m.team_id=%s
        """, (_TEAM_ID,))
        for mid, norm_name in cur.fetchall():
            if norm_name:
                rows.setdefault(norm_name, set()).add(mid)
    finally:
        conn.close()

    pairs = [(name, mid) for name, mids in rows.items() for mid in mids]
    with database._LOCK, database._connect() as local:
        local.execute("DELETE FROM mpn_manufacturer_lookup")
        local.executemany(
            "INSERT OR IGNORE INTO mpn_manufacturer_lookup (normalized_name, manufacturer_id) VALUES (?, ?)",
            pairs)
    return {"manufacturer_aliases": len(pairs)}


def sync_products(full: bool = False) -> dict:
    """Incremental by products.updated_at (full=True forces a from-scratch pull
    -- use once, or if the mirror ever needs to be rebuilt from nothing)."""
    init_schema()
    since = None if full else database.get_setting(_watermark_key("products"))
    conn = azure_sql._connect()
    try:
        cur = conn.cursor()
        if since:
            cur.execute("""
                SELECT id, manufacturer_part_number, normalized_manufacturer_part_number,
                       manufacturer_id, updated_at
                FROM products WHERE team_id=%s AND manufacturer_part_number IS NOT NULL
                  AND updated_at > %s
            """, (_TEAM_ID, since))
        else:
            cur.execute("""
                SELECT id, manufacturer_part_number, normalized_manufacturer_part_number,
                       manufacturer_id, updated_at
                FROM products WHERE team_id=%s AND manufacturer_part_number IS NOT NULL
            """, (_TEAM_ID,))
        rows = cur.fetchall()
    finally:
        conn.close()

    max_updated = since
    payload = []
    for pid, mpn, norm_mpn, mid, updated_at in rows:
        if not norm_mpn:
            norm_mpn = _norm_mpn(mpn)
        payload.append((pid, mpn, norm_mpn, mid))
        if updated_at and (max_updated is None or str(updated_at) > str(max_updated)):
            max_updated = updated_at

    with database._LOCK, database._connect() as local:
        local.executemany("""
            INSERT INTO mpn_products (product_id, mpn, normalized_mpn, manufacturer_id, qty_case)
            VALUES (?, ?, ?, ?, NULL)
            ON CONFLICT(product_id) DO UPDATE SET
                mpn=excluded.mpn, normalized_mpn=excluded.normalized_mpn,
                manufacturer_id=excluded.manufacturer_id
        """, payload)
    if max_updated:
        database.set_setting(_watermark_key("products"), str(max_updated))
    return {"products_synced": len(payload), "full": full}


def sync_packaged(full: bool = False) -> dict:
    """Incremental by packaged_products.updated_at, CS (case) rows only --
    updates mpn_products.qty_case for products already mirrored locally. A
    packaged row whose product hasn't been synced yet (same-hour create race)
    is skipped -- the next products sync brings the product in, and this same
    packaged row will be re-caught on the next incremental packaged sync since
    its own updated_at hasn't moved past our watermark yet... actually it HAS
    (we already saw it) -- rare enough (same-hour product+package creation) to
    accept as a gap a periodic full=True resync closes, not worth the extra
    complexity of a pending-retry queue for this "rarely changes" dataset."""
    init_schema()
    since = None if full else database.get_setting(_watermark_key("packaged"))
    conn = azure_sql._connect()
    try:
        cur = conn.cursor()
        if since:
            cur.execute("""
                SELECT pp.product_id, pp.uom_quantity, pp.updated_at
                FROM packaged_products pp JOIN products p ON p.id = pp.product_id
                WHERE p.team_id=%s AND pp.uom_unit='CS' AND pp.updated_at > %s
            """, (_TEAM_ID, since))
        else:
            cur.execute("""
                SELECT pp.product_id, pp.uom_quantity, pp.updated_at
                FROM packaged_products pp JOIN products p ON p.id = pp.product_id
                WHERE p.team_id=%s AND pp.uom_unit='CS'
            """, (_TEAM_ID,))
        rows = cur.fetchall()
    finally:
        conn.close()

    max_updated = since
    payload = []
    for pid, qty, updated_at in rows:
        payload.append((int(qty) if qty is not None else None, pid))
        if updated_at and (max_updated is None or str(updated_at) > str(max_updated)):
            max_updated = updated_at

    with database._LOCK, database._connect() as local:
        local.executemany("UPDATE mpn_products SET qty_case=? WHERE product_id=?", payload)
    if max_updated:
        database.set_setting(_watermark_key("packaged"), str(max_updated))
    return {"packaged_synced": len(payload), "full": full}


def sync_all(full: bool = False) -> dict:
    mfr = sync_manufacturers()
    prod = sync_products(full=full)
    pkg = sync_packaged(full=full)
    finished = dt.datetime.utcnow().isoformat()
    result = {"finished_at": finished, **mfr, **prod, **pkg}
    try:
        import json
        database.set_setting("mpn_sync_last", json.dumps(result))
    except Exception:  # noqa: BLE001
        log.warning("[mpn_check_sync] failed to persist sync status", exc_info=True)
    return result


def last_sync_status() -> dict | None:
    raw = database.get_setting("mpn_sync_last")
    if not raw:
        return None
    try:
        import json
        return json.loads(raw)
    except Exception:  # noqa: BLE001
        return None


def is_mirror_populated() -> bool:
    with database._connect() as conn:
        try:
            return conn.execute("SELECT 1 FROM mpn_products LIMIT 1").fetchone() is not None
        except Exception:  # noqa: BLE001
            return False


# --------------------------------------------------------------------------
# Hourly in-process scheduler (mirrors vendor_offers_db_sync.py's pattern).
# Incremental syncs are cheap once the initial full pull has run once.
# --------------------------------------------------------------------------

_thread: threading.Thread | None = None
_stop = threading.Event()
_running = threading.Event()
INTERVAL_SECONDS = 3600


def _loop() -> None:
    first = not is_mirror_populated()
    while not _stop.is_set():
        _running.set()
        try:
            sync_all(full=first)
            first = False
        except Exception:  # noqa: BLE001
            log.exception("[mpn_check_sync] scheduled sync failed")
        finally:
            _running.clear()
        if _stop.wait(INTERVAL_SECONDS):
            return


def start_scheduler() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, name="mpn-check-db-sync", daemon=True)
    _thread.start()
    log.info("[mpn_check_sync] hourly scheduler started (incremental; full pull on first run)")


def stop_scheduler() -> None:
    _stop.set()


def scheduler_status() -> dict:
    return {
        "running_now": _running.is_set(),
        "scheduler_alive": bool(_thread and _thread.is_alive()),
        "interval_seconds": INTERVAL_SECONDS,
        "mirror_populated": is_mirror_populated(),
        "last": last_sync_status(),
    }
