"""
SQLite persistence layer.

All tables live in ``catalog_verifier.db`` next to ``main.py``. The module is
idempotent — importing it or calling :func:`init_db` creates tables if missing
and performs additive migrations for v3 (scan-centric) columns.

v3 tables
---------
* ``scans``                   — one row per verification scan (lifecycle state)
* ``scan_catalog_rows``       — raw catalog rows per scan (cascade-deleted)
* ``scan_amazon_rows``        — Amazon/Keepa rows per scan (cascade-deleted)
* ``scan_results``            — per-row verification output (cascade-deleted)

Legacy (retained)
-----------------
* ``keepa_imports``           — global Keepa cache keyed by ASIN
* ``amazon_imports``          — global Amazon cache keyed by ASIN
* ``blacklisted_pairs``       — UPC/ASIN pairs rejected by user or engine
* ``verified_items``          — historical global approved rows
* ``attribute_cache``         — normalised attributes per UPC/ASIN
* ``abbreviation_library``    — categorised abbreviation/full-form mappings
* ``settings``                — key/value for tunable config
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

DB_PATH = Path(__file__).resolve().parent.parent / "catalog_verifier.db"
_LOCK = threading.Lock()

# In-memory cache for flat_library() — invalidated on any write to abbreviation_library.
# TTL is a safety net so a server restart isn't needed to pick up external DB edits.
_LIBRARY_CACHE: list[dict] | None = None
_LIBRARY_CACHE_AT: float = 0.0
_LIBRARY_CACHE_TTL = 300.0  # seconds
_LIBRARY_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Connection helpers
# --------------------------------------------------------------------------- #

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r["name"] == column for r in rows)


def init_db() -> None:
    """Create tables and seed defaults. Safe to call repeatedly."""
    with _LOCK, _connect() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS keepa_imports (
            asin TEXT PRIMARY KEY,
            data_json TEXT NOT NULL,
            imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS amazon_imports (
            asin TEXT PRIMARY KEY,
            data_json TEXT NOT NULL,
            imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS blacklisted_pairs (
            upc TEXT NOT NULL,
            asin TEXT NOT NULL,
            confidence_score REAL,
            failed_signals TEXT,
            date_blacklisted TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (upc, asin)
        );
        CREATE INDEX IF NOT EXISTS idx_bl_upc  ON blacklisted_pairs(upc);
        CREATE INDEX IF NOT EXISTS idx_bl_asin ON blacklisted_pairs(asin);

        CREATE TABLE IF NOT EXISTS verified_items (
            upc TEXT NOT NULL,
            asin TEXT NOT NULL,
            data_json TEXT NOT NULL,
            review_status TEXT DEFAULT '',
            verified_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (upc, asin)
        );
        CREATE TABLE IF NOT EXISTS attribute_cache (
            upc TEXT NOT NULL,
            asin TEXT NOT NULL,
            attributes_json TEXT NOT NULL,
            source TEXT,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (upc, asin)
        );
        CREATE TABLE IF NOT EXISTS abbreviation_library (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            abbr TEXT NOT NULL,
            full_form TEXT NOT NULL,
            added_by TEXT DEFAULT 'system',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(category, abbr)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );

        -- v3 scan-centric tables -------------------------------------------
        CREATE TABLE IF NOT EXISTS scans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            marketplace TEXT DEFAULT 'US',
            condition TEXT DEFAULT 'New',
            status TEXT DEFAULT 'pending',
            mapping_json TEXT,
            catalog_filename TEXT,
            catalog_count INTEGER DEFAULT 0,
            amazon_filename TEXT,
            amazon_source TEXT,
            amazon_count INTEGER DEFAULT 0,
            ai_mode INTEGER DEFAULT 0,
            verified_count INTEGER DEFAULT 0,
            review_count INTEGER DEFAULT 0,
            not_approved_count INTEGER DEFAULT 0,
            reviewed_count INTEGER DEFAULT 0,
            exported_at TIMESTAMP,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS scan_catalog_rows (
            scan_id INTEGER NOT NULL,
            row_idx INTEGER NOT NULL,
            data_json TEXT NOT NULL,
            PRIMARY KEY (scan_id, row_idx),
            FOREIGN KEY (scan_id) REFERENCES scans(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS scan_amazon_rows (
            scan_id INTEGER NOT NULL,
            asin TEXT NOT NULL,
            data_json TEXT NOT NULL,
            PRIMARY KEY (scan_id, asin),
            FOREIGN KEY (scan_id) REFERENCES scans(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS scan_results (
            scan_id INTEGER NOT NULL,
            row_idx INTEGER NOT NULL,
            upc TEXT,
            asin TEXT,
            verdict TEXT,
            score REAL,
            review_status TEXT DEFAULT '',
            data_json TEXT NOT NULL,
            PRIMARY KEY (scan_id, row_idx),
            FOREIGN KEY (scan_id) REFERENCES scans(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_scan_results_scan      ON scan_results(scan_id);
        CREATE INDEX IF NOT EXISTS idx_scans_created_at       ON scans(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_scan_catalog_rows_scan ON scan_catalog_rows(scan_id);
        CREATE INDEX IF NOT EXISTS idx_scan_amazon_rows_scan  ON scan_amazon_rows(scan_id);
        CREATE INDEX IF NOT EXISTS idx_scan_results_upc_asin  ON scan_results(upc, asin);
        CREATE INDEX IF NOT EXISTS idx_analytics_catalog_run  ON analytics_catalog_rows(run_id);

        -- Analytics tab (ROI & Cost) ---------------------------------------
        -- One row per run. search_methods is a JSON array like
        -- ["UPC","ItemID","Title"]. status is 'Searching' / 'Vetting' /
        -- 'Complete' / 'Error'.
        CREATE TABLE IF NOT EXISTS analytics_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT,
            marketplace TEXT DEFAULT 'US',
            search_methods TEXT,
            pages_per_title INTEGER DEFAULT 5,
            ai_clean_titles INTEGER DEFAULT 0,
            total_catalog_items INTEGER DEFAULT 0,
            total_candidates_found INTEGER DEFAULT 0,
            verified_count INTEGER DEFAULT 0,
            review_count INTEGER DEFAULT 0,
            not_approved_count INTEGER DEFAULT 0,
            status TEXT DEFAULT 'Pending',
            progress_phase TEXT,
            progress_done INTEGER DEFAULT 0,
            progress_total INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        -- One row per uploaded catalog line, keyed to a run.
        CREATE TABLE IF NOT EXISTS analytics_catalog_rows (
            run_id INTEGER NOT NULL,
            row_idx INTEGER NOT NULL,
            data_json TEXT NOT NULL,
            PRIMARY KEY (run_id, row_idx),
            FOREIGN KEY (run_id) REFERENCES analytics_runs(id) ON DELETE CASCADE
        );
        -- One row per (catalog_row × candidate_asin) — the vetting result
        -- table. sources is a JSON array (e.g. ["UPC","Title"]).
        CREATE TABLE IF NOT EXISTS analytics_candidates (
            run_id INTEGER NOT NULL,
            row_idx INTEGER NOT NULL,
            asin TEXT NOT NULL,
            sources TEXT,
            confidence REAL,
            verdict TEXT,
            amz_pack INTEGER,
            review_status TEXT DEFAULT '',
            data_json TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (run_id, row_idx, asin),
            FOREIGN KEY (run_id) REFERENCES analytics_runs(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_analytics_candidates_run ON analytics_candidates(run_id);
        CREATE INDEX IF NOT EXISTS idx_analytics_runs_created   ON analytics_runs(created_at DESC);
        """)

        # additive migration — add added_by to libraries that predate v3
        if not _column_exists(conn, "abbreviation_library", "added_by"):
            conn.execute(
                "ALTER TABLE abbreviation_library "
                "ADD COLUMN added_by TEXT DEFAULT 'system'"
            )

        # additive migration — add sales_rank for rank-based filtering
        if not _column_exists(conn, "analytics_candidates", "sales_rank"):
            conn.execute(
                "ALTER TABLE analytics_candidates ADD COLUMN sales_rank INTEGER"
            )

        # additive migration — max_rank / min_rank caps applied during run / rescore
        if not _column_exists(conn, "analytics_runs", "max_rank"):
            conn.execute(
                "ALTER TABLE analytics_runs ADD COLUMN max_rank INTEGER DEFAULT 0"
            )
        if not _column_exists(conn, "analytics_runs", "min_rank"):
            conn.execute(
                "ALTER TABLE analytics_runs ADD COLUMN min_rank INTEGER DEFAULT 0"
            )

        # additive migration — extracted brand/product fields per catalog row
        if not _column_exists(conn, "analytics_catalog_rows", "extracted_json"):
            conn.execute(
                "ALTER TABLE analytics_catalog_rows ADD COLUMN extracted_json TEXT"
            )

        # additive migration — analytics AI check results
        if not _column_exists(conn, "analytics_candidates", "ai_verdict"):
            conn.execute("ALTER TABLE analytics_candidates ADD COLUMN ai_verdict TEXT")
        if not _column_exists(conn, "analytics_candidates", "ai_reasoning"):
            conn.execute("ALTER TABLE analytics_candidates ADD COLUMN ai_reasoning TEXT")
        if not _column_exists(conn, "analytics_runs", "ai_check_status"):
            conn.execute("ALTER TABLE analytics_runs ADD COLUMN ai_check_status TEXT")
        if not _column_exists(conn, "analytics_runs", "ai_check_done"):
            conn.execute("ALTER TABLE analytics_runs ADD COLUMN ai_check_done INTEGER DEFAULT 0")
        if not _column_exists(conn, "analytics_runs", "ai_check_total"):
            conn.execute("ALTER TABLE analytics_runs ADD COLUMN ai_check_total INTEGER DEFAULT 0")

        _seed_settings(conn)
        _seed_library(conn)


def _seed_settings(conn: sqlite3.Connection) -> None:
    defaults = {
        "threshold_verified": "85",
        "threshold_review":   "35",
    }
    for key, value in defaults.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)",
            (key, value),
        )


def _seed_library(conn: sqlite3.Connection) -> None:
    """Only seed if the table is empty — avoids clobbering user edits."""
    row = conn.execute("SELECT COUNT(*) AS n FROM abbreviation_library").fetchone()
    if row and row["n"] > 0:
        return
    seed_path = DB_PATH.parent / "abbreviations.json"
    if not seed_path.exists():
        return
    try:
        data = json.loads(seed_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return
    rows: list[tuple[str, str, str, str]] = []
    categories = data.get("categories") or {}
    for category, entries in categories.items():
        for entry in entries:
            if isinstance(entry, str):
                abbr = entry
                full = entry
            elif isinstance(entry, dict):
                abbr = entry.get("abbr", "")
                full = entry.get("full", abbr)
            else:
                continue
            if not abbr:
                continue
            rows.append((category, abbr.strip(), (full or abbr).strip(), "system"))
    conn.executemany(
        "INSERT OR IGNORE INTO abbreviation_library "
        "(category, abbr, full_form, added_by) VALUES (?, ?, ?, ?)",
        rows,
    )


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #

def get_thresholds() -> dict:
    with _connect() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    values = {r["key"]: r["value"] for r in rows}
    return {
        "verified": float(values.get("threshold_verified", "85")),
        "review":   float(values.get("threshold_review", "35")),
    }


def set_thresholds(verified: float, review: float) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('threshold_verified', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(verified),),
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES ('threshold_review', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(review),),
        )


# --------------------------------------------------------------------------- #
# Imports (legacy global pool — still used as a cross-scan cache)
# --------------------------------------------------------------------------- #

def save_imports(table: str, rows: list[dict]) -> int:
    if table not in ("keepa_imports", "amazon_imports"):
        raise ValueError("table must be keepa_imports or amazon_imports")
    saved = 0
    with _LOCK, _connect() as conn:
        for row in rows:
            asin = (row.get("ASIN") or row.get("asin") or "").strip().upper()
            if not asin:
                continue
            conn.execute(
                f"INSERT INTO {table}(asin, data_json, imported_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP) "
                "ON CONFLICT(asin) DO UPDATE SET "
                "  data_json=excluded.data_json, imported_at=CURRENT_TIMESTAMP",
                (asin, json.dumps(row, default=str)),
            )
            saved += 1
    return saved


def load_imports(table: str) -> dict[str, dict]:
    if table not in ("keepa_imports", "amazon_imports"):
        raise ValueError("table must be keepa_imports or amazon_imports")
    out: dict[str, dict] = {}
    with _connect() as conn:
        for r in conn.execute(f"SELECT asin, data_json FROM {table}"):
            try:
                out[r["asin"]] = json.loads(r["data_json"])
            except json.JSONDecodeError:
                continue
    return out


# --------------------------------------------------------------------------- #
# Blacklist
# --------------------------------------------------------------------------- #

def add_to_blacklist(
    upc: str, asin: str, confidence: float | None,
    failed_signals: Iterable[str],
) -> None:
    if not upc or not asin:
        return
    failed = ", ".join(sorted(set(failed_signals)))
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO blacklisted_pairs(upc, asin, confidence_score, "
            "    failed_signals, date_blacklisted) "
            "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(upc, asin) DO UPDATE SET "
            "  confidence_score=excluded.confidence_score, "
            "  failed_signals=excluded.failed_signals, "
            "  date_blacklisted=CURRENT_TIMESTAMP",
            (str(upc).strip(), str(asin).strip().upper(),
             None if confidence is None else float(confidence), failed),
        )


def remove_from_blacklist(upc: str, asin: str) -> int:
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "DELETE FROM blacklisted_pairs WHERE upc=? AND asin=?",
            (str(upc).strip(), str(asin).strip().upper()),
        )
        return cur.rowcount


def search_blacklist(query: str) -> list[dict]:
    q = (query or "").strip().upper()
    if not q:
        return []
    with _connect() as conn:
        rows = conn.execute(
            "SELECT upc, asin, confidence_score, failed_signals, "
            "       date_blacklisted "
            "FROM blacklisted_pairs "
            "WHERE upc LIKE ? OR asin LIKE ? "
            "ORDER BY date_blacklisted DESC LIMIT 200",
            (f"%{q}%", f"%{q}%"),
        ).fetchall()
    return [dict(r) for r in rows]


def is_blacklisted(upc: str, asin: str) -> bool:
    if not upc or not asin:
        return False
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM blacklisted_pairs WHERE upc=? AND asin=?",
            (str(upc).strip(), str(asin).strip().upper()),
        ).fetchone()
    return bool(row)


def load_blacklist_set() -> set[tuple[str, str]]:
    """Return all blacklisted (upc, asin) pairs as a set for O(1) bulk lookup."""
    with _connect() as conn:
        rows = conn.execute("SELECT upc, asin FROM blacklisted_pairs").fetchall()
    return {(r["upc"], r["asin"].upper()) for r in rows}


# --------------------------------------------------------------------------- #
# Verified items (legacy / global)
# --------------------------------------------------------------------------- #

def save_verified(upc: str, asin: str, data: dict, review_status: str = "") -> None:
    if not upc or not asin:
        return
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO verified_items(upc, asin, data_json, review_status, "
            "    verified_at) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(upc, asin) DO UPDATE SET "
            "  data_json=excluded.data_json, "
            "  review_status=excluded.review_status, "
            "  verified_at=CURRENT_TIMESTAMP",
            (str(upc).strip(), str(asin).strip().upper(),
             json.dumps(data, default=str), review_status),
        )


# --------------------------------------------------------------------------- #
# Attribute cache
# --------------------------------------------------------------------------- #

def cache_attributes(upc: str, asin: str, attributes: dict, source: str) -> None:
    if not upc or not asin:
        return
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO attribute_cache(upc, asin, attributes_json, source, "
            "    last_updated) VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(upc, asin) DO UPDATE SET "
            "  attributes_json=excluded.attributes_json, "
            "  source=excluded.source, last_updated=CURRENT_TIMESTAMP",
            (str(upc).strip(), str(asin).strip().upper(),
             json.dumps(attributes, default=str), source),
        )


def get_cached_attributes(upc: str, asin: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT attributes_json, source, last_updated "
            "FROM attribute_cache WHERE upc=? AND asin=?",
            (str(upc).strip(), str(asin).strip().upper()),
        ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(row["attributes_json"])
    except json.JSONDecodeError:
        return None
    payload["_source"] = row["source"]
    payload["_last_updated"] = row["last_updated"]
    return payload


def clear_cached_attributes(upc: str, asin: str) -> int:
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "DELETE FROM attribute_cache WHERE upc=? AND asin=?",
            (str(upc).strip(), str(asin).strip().upper()),
        )
        return cur.rowcount


# --------------------------------------------------------------------------- #
# Abbreviation library
# --------------------------------------------------------------------------- #

VALID_CATEGORIES = [
    "Colors", "Sizes", "UOMs", "Forms", "Sterility", "Materials",
    "Scents", "Flavors", "Packaging", "Product Attributes",
]


def list_library() -> dict[str, list[dict]]:
    """Return a map of category → list of {id, abbr, full, added_by} entries."""
    out: dict[str, list[dict]] = {c: [] for c in VALID_CATEGORIES}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, category, abbr, full_form, added_by, created_at "
            "FROM abbreviation_library "
            "ORDER BY category, abbr"
        ).fetchall()
    for r in rows:
        cat = r["category"] if r["category"] in VALID_CATEGORIES else "Product Attributes"
        out.setdefault(cat, []).append({
            "id": r["id"],
            "abbr": r["abbr"],
            "full": r["full_form"],
            "added_by": r["added_by"] or "system",
            "created_at": r["created_at"],
        })
    return out


def flat_library() -> list[dict]:
    """Flat list suitable for the rule-based extractor. Cached with TTL."""
    global _LIBRARY_CACHE, _LIBRARY_CACHE_AT
    with _LIBRARY_LOCK:
        if _LIBRARY_CACHE is not None and (time.monotonic() - _LIBRARY_CACHE_AT) < _LIBRARY_CACHE_TTL:
            return _LIBRARY_CACHE
        flat: list[dict] = []
        for entries in list_library().values():
            for e in entries:
                flat.append({"abbr": e["abbr"], "full": e["full"]})
        _LIBRARY_CACHE = flat
        _LIBRARY_CACHE_AT = time.monotonic()
        return flat


def _invalidate_library_cache() -> None:
    global _LIBRARY_CACHE
    with _LIBRARY_LOCK:
        _LIBRARY_CACHE = None


def add_library_entry(
    category: str, abbr: str, full: str, added_by: str = "user",
) -> tuple[bool, dict | None]:
    """Return (created, existing_entry).

    If the abbr already exists in the same category, ``created`` is False and
    the existing entry is returned so the UI can highlight the duplicate.
    """
    if category not in VALID_CATEGORIES:
        category = "Product Attributes"
    abbr = (abbr or "").strip()
    full = (full or "").strip() or abbr
    if added_by not in ("system", "user", "ai"):
        added_by = "user"
    if not abbr:
        return False, None
    with _LOCK, _connect() as conn:
        row = conn.execute(
            "SELECT id, abbr, full_form, added_by FROM abbreviation_library "
            "WHERE category=? AND LOWER(abbr)=LOWER(?)",
            (category, abbr),
        ).fetchone()
        if row:
            return False, {
                "id": row["id"], "abbr": row["abbr"],
                "full": row["full_form"], "added_by": row["added_by"] or "system",
            }
        conn.execute(
            "INSERT INTO abbreviation_library "
            "(category, abbr, full_form, added_by) VALUES (?, ?, ?, ?)",
            (category, abbr, full, added_by),
        )
        new_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
    _invalidate_library_cache()
    return True, {
        "id": new_id, "abbr": abbr, "full": full,
        "added_by": added_by, "category": category,
    }


def delete_library_entry(entry_id: int) -> int:
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "DELETE FROM abbreviation_library WHERE id=?", (entry_id,),
        )
        rowcount = cur.rowcount
    _invalidate_library_cache()
    return rowcount


def has_library_entry(abbr: str) -> bool:
    """Check against the in-memory cache — avoids a DB round-trip per call."""
    token = (abbr or "").strip().lower()
    if not token:
        return False
    return any(e["abbr"].lower() == token for e in flat_library())


# --------------------------------------------------------------------------- #
# Scans (v3)
# --------------------------------------------------------------------------- #

SCAN_STATUSES = {
    "pending",                       # awaiting Amazon data
    "ready",                         # catalog + Amazon ready, not yet verified
    "verifying",                     # in-flight
    "verified_unreviewed",           # finished, no review touches yet
    "verified_partial",              # some rows reviewed, some pending
    "verified_complete",             # fully reviewed / exported
}


def _scan_row_to_dict(r: sqlite3.Row) -> dict:
    d = dict(r)
    try:
        d["mapping"] = json.loads(d.pop("mapping_json") or "{}")
    except json.JSONDecodeError:
        d["mapping"] = {}
    return d


def create_scan(
    name: str,
    mapping: dict,
    marketplace: str = "US",
    condition: str = "New",
    catalog_filename: str | None = None,
    catalog_count: int = 0,
    ai_mode: bool = False,
) -> int:
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO scans(name, marketplace, condition, mapping_json, "
            "    catalog_filename, catalog_count, status, ai_mode) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)",
            (name, marketplace, condition, json.dumps(mapping, default=str),
             catalog_filename, int(catalog_count), 1 if ai_mode else 0),
        )
        return cur.lastrowid


def list_scans() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, marketplace, condition, status, mapping_json, "
            "       catalog_filename, catalog_count, amazon_filename, "
            "       amazon_source, amazon_count, ai_mode, verified_count, "
            "       review_count, not_approved_count, reviewed_count, "
            "       exported_at, created_at, updated_at "
            "FROM scans ORDER BY datetime(created_at) DESC"
        ).fetchall()
    return [_scan_row_to_dict(r) for r in rows]


def get_scan(scan_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            "SELECT id, name, marketplace, condition, status, mapping_json, "
            "       catalog_filename, catalog_count, amazon_filename, "
            "       amazon_source, amazon_count, ai_mode, verified_count, "
            "       review_count, not_approved_count, reviewed_count, "
            "       exported_at, created_at, updated_at "
            "FROM scans WHERE id=?", (scan_id,),
        ).fetchone()
    return _scan_row_to_dict(row) if row else None


def update_scan(scan_id: int, **fields: Any) -> None:
    if not fields:
        return
    safe = {}
    allowed = {
        "name", "marketplace", "condition", "status",
        "catalog_filename", "catalog_count",
        "amazon_filename", "amazon_source", "amazon_count",
        "ai_mode",
        "verified_count", "review_count", "not_approved_count",
        "reviewed_count", "exported_at",
    }
    for k, v in fields.items():
        if k in allowed:
            safe[k] = int(v) if isinstance(v, bool) else v
    if not safe:
        return
    keys = ", ".join(f"{k}=?" for k in safe)
    params = list(safe.values()) + [scan_id]
    with _LOCK, _connect() as conn:
        conn.execute(
            f"UPDATE scans SET {keys}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            params,
        )


def delete_scan(scan_id: int) -> int:
    with _LOCK, _connect() as conn:
        cur = conn.execute("DELETE FROM scans WHERE id=?", (scan_id,))
        return cur.rowcount


def save_scan_catalog_rows(scan_id: int, rows: list[dict]) -> int:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM scan_catalog_rows WHERE scan_id=?", (scan_id,))
        conn.executemany(
            "INSERT INTO scan_catalog_rows(scan_id, row_idx, data_json) "
            "VALUES (?, ?, ?)",
            [(scan_id, i, json.dumps(r, default=str)) for i, r in enumerate(rows)],
        )
    return len(rows)


def load_scan_catalog_rows(scan_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT data_json FROM scan_catalog_rows "
            "WHERE scan_id=? ORDER BY row_idx",
            (scan_id,),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            out.append(json.loads(r["data_json"]))
        except json.JSONDecodeError:
            continue
    return out


def save_scan_amazon_rows(scan_id: int, rows: list[dict]) -> int:
    saved = 0
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM scan_amazon_rows WHERE scan_id=?", (scan_id,))
        for row in rows:
            asin = (row.get("ASIN") or row.get("asin") or "").strip().upper()
            if not asin:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO scan_amazon_rows"
                "(scan_id, asin, data_json) VALUES (?, ?, ?)",
                (scan_id, asin, json.dumps(row, default=str)),
            )
            saved += 1
    return saved


def load_scan_amazon_rows(scan_id: int) -> dict[str, dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT asin, data_json FROM scan_amazon_rows WHERE scan_id=?",
            (scan_id,),
        ).fetchall()
    out: dict[str, dict] = {}
    for r in rows:
        try:
            out[r["asin"]] = json.loads(r["data_json"])
        except json.JSONDecodeError:
            continue
    return out


def save_scan_results(scan_id: int, results: list[dict]) -> int:
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM scan_results WHERE scan_id=?", (scan_id,))
        conn.executemany(
            "INSERT INTO scan_results"
            "(scan_id, row_idx, upc, asin, verdict, score, review_status, data_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    scan_id, i,
                    str(r.get("UPC") or "").strip(),
                    str(r.get("ASIN") or "").strip().upper(),
                    r.get("Verdict") or "",
                    float(r.get("Confidence") or 0) if r.get("Confidence") not in (None, "") else None,
                    r.get("review_status") or "",
                    json.dumps(r, default=str),
                )
                for i, r in enumerate(results)
            ],
        )
    return len(results)


def load_scan_results(scan_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT data_json, review_status FROM scan_results "
            "WHERE scan_id=? ORDER BY row_idx",
            (scan_id,),
        ).fetchall()
    out: list[dict] = []
    for r in rows:
        try:
            payload = json.loads(r["data_json"])
        except json.JSONDecodeError:
            continue
        payload["review_status"] = r["review_status"] or payload.get("review_status", "")
        out.append(payload)
    return out


def update_scan_result_row(
    scan_id: int, row_idx: int, verdict: str, review_status: str, data: dict,
) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "UPDATE scan_results SET verdict=?, review_status=?, data_json=? "
            "WHERE scan_id=? AND row_idx=?",
            (verdict, review_status, json.dumps(data, default=str),
             scan_id, row_idx),
        )


def recompute_scan_stats(scan_id: int) -> dict:
    """Derive verified/review/not_approved/reviewed counts from scan_results."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT verdict, review_status FROM scan_results WHERE scan_id=?",
            (scan_id,),
        ).fetchall()
    verified = review = not_approved = reviewed = 0
    for r in rows:
        v = (r["verdict"] or "").lower()
        if v == "verified":
            verified += 1
        elif v == "review":
            review += 1
        elif v in ("not approved", "not verified"):
            not_approved += 1
        if r["review_status"]:
            reviewed += 1
    update_scan(
        scan_id,
        verified_count=verified,
        review_count=review,
        not_approved_count=not_approved,
        reviewed_count=reviewed,
    )
    return {
        "verified": verified, "review": review,
        "not_approved": not_approved, "reviewed": reviewed,
    }


# --------------------------------------------------------------------------- #
# Maintenance
# --------------------------------------------------------------------------- #

def reset_tables(tables: Iterable[str]) -> None:
    """Developer-only. Never called from production paths."""
    with _LOCK, _connect() as conn:
        for t in tables:
            conn.execute(f"DELETE FROM {t}")


# Make sure the DB exists at import time. Keeps the rest of the code simple.
init_db()
