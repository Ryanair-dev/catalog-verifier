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
    conn.execute("PRAGMA synchronous = NORMAL;")   # safe with WAL; faster fsync
    conn.execute("PRAGMA cache_size = -32768;")     # 32 MB page cache
    conn.execute("PRAGMA temp_store = MEMORY;")     # temp tables/indices in RAM
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
        CREATE TABLE IF NOT EXISTS scan_candidates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scan_id INTEGER NOT NULL,
            row_idx INTEGER NOT NULL,
            asin TEXT NOT NULL,
            confidence REAL DEFAULT 0,
            verdict TEXT DEFAULT 'review',
            review_status TEXT DEFAULT '',
            match_method TEXT,
            data_json TEXT NOT NULL,
            UNIQUE(scan_id, row_idx, asin),
            FOREIGN KEY (scan_id) REFERENCES scans(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_scan_candidates_scan    ON scan_candidates(scan_id);
        CREATE INDEX IF NOT EXISTS idx_scan_candidates_row     ON scan_candidates(scan_id, row_idx);
        CREATE INDEX IF NOT EXISTS idx_scan_candidates_asin    ON scan_candidates(scan_id, asin);
        CREATE INDEX IF NOT EXISTS idx_scan_results_scan      ON scan_results(scan_id);
        CREATE INDEX IF NOT EXISTS idx_scans_created_at       ON scans(created_at DESC);
        CREATE INDEX IF NOT EXISTS idx_scan_catalog_rows_scan ON scan_catalog_rows(scan_id);
        CREATE INDEX IF NOT EXISTS idx_scan_amazon_rows_scan  ON scan_amazon_rows(scan_id);
        CREATE INDEX IF NOT EXISTS idx_scan_results_upc_asin  ON scan_results(upc, asin);

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
        CREATE INDEX IF NOT EXISTS idx_analytics_catalog_run  ON analytics_catalog_rows(run_id);
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

        # additive migrations — match-from-Keepa mode on scans
        if not _column_exists(conn, "scans", "match_from_keepa"):
            conn.execute(
                "ALTER TABLE scans ADD COLUMN match_from_keepa INTEGER DEFAULT 0"
            )
        if not _column_exists(conn, "scans", "match_methods"):
            conn.execute(
                "ALTER TABLE scans ADD COLUMN match_methods TEXT DEFAULT NULL"
            )

        # additive migration — vetting mode (cpg / medical) per run
        if not _column_exists(conn, "analytics_runs", "vetting_mode"):
            conn.execute(
                "ALTER TABLE analytics_runs ADD COLUMN vetting_mode TEXT DEFAULT 'cpg'"
            )

        # additive migration — brand column/text saved at wizard time for rescore pre-population
        if not _column_exists(conn, "analytics_runs", "brand_col"):
            conn.execute("ALTER TABLE analytics_runs ADD COLUMN brand_col TEXT DEFAULT ''")
        if not _column_exists(conn, "analytics_runs", "brand_mode"):
            conn.execute("ALTER TABLE analytics_runs ADD COLUMN brand_mode TEXT DEFAULT 'col'")

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
        if not _column_exists(conn, "analytics_runs", "ai_decisions_applied"):
            conn.execute("ALTER TABLE analytics_runs ADD COLUMN ai_decisions_applied INTEGER DEFAULT 0")

        # additive migration — eligibility (CAN_SELL/NEEDS_APPROVAL/RESTRICTED) + storage fee,
        # populated for Approved/Review candidates by services/analytics/eligibility_check.py
        if not _column_exists(conn, "analytics_candidates", "eligibility_status"):
            conn.execute("ALTER TABLE analytics_candidates ADD COLUMN eligibility_status TEXT")
        if not _column_exists(conn, "analytics_candidates", "storage_fee"):
            conn.execute("ALTER TABLE analytics_candidates ADD COLUMN storage_fee REAL")
        if not _column_exists(conn, "analytics_candidates", "storage_fee_peak"):
            conn.execute("ALTER TABLE analytics_candidates ADD COLUMN storage_fee_peak REAL")
        if not _column_exists(conn, "analytics_runs", "elig_check_status"):
            conn.execute("ALTER TABLE analytics_runs ADD COLUMN elig_check_status TEXT")
        if not _column_exists(conn, "analytics_runs", "elig_check_done"):
            conn.execute("ALTER TABLE analytics_runs ADD COLUMN elig_check_done INTEGER DEFAULT 0")
        if not _column_exists(conn, "analytics_runs", "elig_check_total"):
            conn.execute("ALTER TABLE analytics_runs ADD COLUMN elig_check_total INTEGER DEFAULT 0")

        # additive migration — global ASIN cache (SP-API normalized data, all fetched ASINs)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS global_asin_cache (
                asin TEXT PRIMARY KEY,
                data_json TEXT NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # Pair Library ------------------------------------------------------
        # Manually-confirmed pairs imported via the Pair Manager.  One row per
        # ASIN; its identifiers live in pair_library_ids, UNIQUE PER ASIN — so
        # an ASIN carries its own UPC(s)/MPN(s) (primary + aliases) AND the same
        # UPC may sit under many ASINs (one product, many Amazon listings).
        # Authority is per-ASIN: once an ASIN is imported, only its imported
        # UPCs are valid for it (enforced at read time in load_verified_set).
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pair_library (
                asin         TEXT PRIMARY KEY,
                brand        TEXT DEFAULT '',
                manufacturer TEXT DEFAULT '',
                amz_pack     TEXT DEFAULT '',
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pair_library_ids (
                asin       TEXT NOT NULL,
                id_type    TEXT NOT NULL,
                identifier TEXT NOT NULL,
                is_primary INTEGER DEFAULT 0,
                added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (asin, id_type, identifier)
            )
        """)
        # Migration: earlier builds used PRIMARY KEY (id_type, identifier),
        # which forced one ASIN per UPC.  Rebuild to per-ASIN uniqueness so a
        # UPC can be shared across many ASINs.  (IF NOT EXISTS above is a no-op
        # when an old table is present; this rebuilds it, preserving rows.)
        _plib_pk = {r["name"] for r in
                    conn.execute("PRAGMA table_info(pair_library_ids)").fetchall()
                    if r["pk"] > 0}
        if _plib_pk == {"id_type", "identifier"}:
            conn.execute("ALTER TABLE pair_library_ids RENAME TO _plib_ids_old")
            conn.execute("""
                CREATE TABLE pair_library_ids (
                    asin       TEXT NOT NULL,
                    id_type    TEXT NOT NULL,
                    identifier TEXT NOT NULL,
                    is_primary INTEGER DEFAULT 0,
                    added_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (asin, id_type, identifier)
                )
            """)
            conn.execute(
                "INSERT OR IGNORE INTO pair_library_ids"
                "(asin, id_type, identifier, is_primary, added_at) "
                "SELECT asin, id_type, identifier, is_primary, added_at "
                "FROM _plib_ids_old"
            )
            conn.execute("DROP TABLE _plib_ids_old")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_plib_ids_asin ON pair_library_ids(asin)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_plib_ids_ident ON pair_library_ids(id_type, identifier)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_plib_brand ON pair_library(brand)"
        )
        # additive: amz_pack column on existing pair_library tables
        if not _column_exists(conn, "pair_library", "amz_pack"):
            conn.execute("ALTER TABLE pair_library ADD COLUMN amz_pack TEXT DEFAULT ''")

        # Brand Analytics tables -------------------------------------------
        conn.execute("""
            CREATE TABLE IF NOT EXISTS brand_library (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                entity_type         TEXT NOT NULL,
                name                TEXT NOT NULL UNIQUE,
                parent_manufacturer TEXT,
                sub_brands          TEXT DEFAULT '[]',
                aliases             TEXT DEFAULT '[]',
                discovered_by       TEXT DEFAULT 'user',
                created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at          TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS brand_analytics_runs (
                id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                name                TEXT NOT NULL,
                search_type         TEXT NOT NULL,
                search_terms        TEXT NOT NULL,
                library_id          INTEGER,
                status              TEXT DEFAULT 'Pending',
                progress_phase      TEXT,
                progress_done       INTEGER DEFAULT 0,
                progress_total      INTEGER DEFAULT 0,
                min_rank            INTEGER DEFAULT 0,
                max_rank            INTEGER DEFAULT 0,
                pages_per_brand     INTEGER DEFAULT 3,
                last_asin_updated_at TEXT,
                ai_fill_status      TEXT,
                ai_fill_done        INTEGER DEFAULT 0,
                ai_fill_total       INTEGER DEFAULT 0,
                created_at          TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at          TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS brand_analytics_items (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id          INTEGER NOT NULL,
                brand_searched  TEXT NOT NULL,
                asin            TEXT NOT NULL,
                title           TEXT,
                bsr             INTEGER,
                bsr_category    TEXT,
                mpn             TEXT,
                upc             TEXT,
                ean             TEXT,
                gtin            TEXT,
                image_url       TEXT,
                ai_mpn          TEXT,
                ai_upc          TEXT,
                ai_ean          TEXT,
                ai_gtin         TEXT,
                ai_fill_status  TEXT,
                data_json       TEXT DEFAULT '{}',
                updated_at      TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(run_id, asin)
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_brand_items_run
            ON brand_analytics_items(run_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_brand_runs_created
            ON brand_analytics_runs(created_at DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_brand_items_asin
            ON brand_analytics_items(asin)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_analytics_cand_run_verdict
            ON analytics_candidates(run_id, verdict)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_analytics_candidates_verdict_conf
            ON analytics_candidates(run_id, verdict, confidence DESC)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_analytics_cand_sales_rank
            ON analytics_candidates(sales_rank)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_analytics_candidates_run_sales_rank
            ON analytics_candidates(run_id, sales_rank)
        """)

        # Additive migrations for brand_analytics_items
        for col, defn in [
            ("pack_qty",  "TEXT"),
            ("uom_qty",   "TEXT"),
            ("amz_brand", "TEXT"),   # actual Amazon brand field value (not search query label)
            ("storage_fee",       "REAL"),   # FBA storage $/unit/mo (off-peak), from dims
            ("storage_fee_peak",  "REAL"),   # Q4 peak $/unit/mo
            ("buybox_price",      "REAL"),   # SP-API Product Pricing buy-box price
            ("eligibility_status", "TEXT"),  # CAN_SELL / NEEDS_APPROVAL / RESTRICTED
        ]:
            try:
                conn.execute(f"ALTER TABLE brand_analytics_items ADD COLUMN {col} {defn}")
            except Exception:
                pass  # column already exists

        # Additive migrations for brand_analytics_runs
        try:
            conn.execute("ALTER TABLE brand_analytics_runs ADD COLUMN vetting_mode TEXT DEFAULT 'cpg'")
        except Exception:
            pass
        for col, defn in [
            ("source",             "TEXT"),      # 'keepa' | 'spapi' — how ASINs were discovered
            ("keepa_tokens_spent", "INTEGER"),   # Keepa tokens used this run
            ("keepa_tokens_left",  "INTEGER"),   # Keepa bucket balance after the run
            ("elig_check_status",  "TEXT"),      # eligibility/storage enrichment progress (reused)
            ("elig_check_done",    "INTEGER DEFAULT 0"),
            ("elig_check_total",   "INTEGER DEFAULT 0"),
        ]:
            try:
                conn.execute(f"ALTER TABLE brand_analytics_runs ADD COLUMN {col} {defn}")
            except Exception:
                pass

        # Additive migration — duplicate catalog rows removed at run creation
        if not _column_exists(conn, "analytics_runs", "duplicate_rows_removed"):
            conn.execute(
                "ALTER TABLE analytics_runs ADD COLUMN duplicate_rows_removed INTEGER DEFAULT 0"
            )

        # Additive migration — vendor passthrough columns carried into export
        if not _column_exists(conn, "analytics_runs", "passthrough_cols"):
            conn.execute(
                "ALTER TABLE analytics_runs ADD COLUMN passthrough_cols TEXT DEFAULT ''"
            )

        # Global ASIN identifier overrides — persists user corrections across all runs
        conn.execute("""
            CREATE TABLE IF NOT EXISTS asin_identifier_overrides (
                asin       TEXT PRIMARY KEY,
                mpn        TEXT,
                upc        TEXT,
                ean        TEXT,
                gtin       TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)

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
    """Return all blacklisted (upc, asin) pairs as a set for O(1) bulk lookup.

    Pair Library precedence: a manually-imported (UPC, ASIN) pair is ground
    truth, so a system-saved blacklist entry for that exact pair is EXCLUDED
    here — a blacklist can never disable a library-confirmed match.  (The
    blacklist row stays in the table and remains visible/unlockable in the
    Pair Manager; runs simply ignore it.)
    """
    asin_upcs = _library_asin_upcs()
    with _connect() as conn:
        rows = conn.execute("SELECT upc, asin FROM blacklisted_pairs").fetchall()
    out: set[tuple[str, str]] = set()
    for r in rows:
        upc, asin = r["upc"], r["asin"].upper()
        allowed = asin_upcs.get(asin)
        if allowed is not None and upc in allowed:
            continue  # this exact pair is in the library → never blacklist it
        out.add((upc, asin))
    return out


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
            # Preserve the Pair Library provenance marker — export round-trips
            # re-save verified rows and must not erase where the pair came from.
            "  review_status=CASE WHEN verified_items.review_status='Pair Library Import' "
            "                     THEN verified_items.review_status "
            "                     ELSE excluded.review_status END, "
            "  verified_at=CURRENT_TIMESTAMP",
            (str(upc).strip(), str(asin).strip().upper(),
             json.dumps(data, default=str), review_status),
        )


def load_verified_set() -> set[tuple[str, str]]:
    """Return all verified (upc, asin) pairs as a set for O(1) bulk lookup.

    Pair Library precedence (ASIN-keyed): once an ASIN is in the manually-
    imported library, ONLY its imported UPC(s) are valid for it.  So a
    system-saved verified pair (in-app Approve, export round-trip, a
    slipped-through bad match …) for a library ASIN whose UPC is NOT one the
    user imported for that ASIN is EXCLUDED — that's how a wrong pair gets
    fixed.  A UPC may still belong to many ASINs (one product, many listings):
    the rule keys on the ASIN, not the UPC, so other ASINs sharing that UPC are
    untouched.  All library pairs (every barcode form) are always included.
    """
    asin_upcs = _library_asin_upcs()
    with _connect() as conn:
        rows = conn.execute("SELECT upc, asin FROM verified_items").fetchall()
    out: set[tuple[str, str]] = set()
    for r in rows:
        upc, asin = r["upc"], r["asin"].upper()
        allowed = asin_upcs.get(asin)
        if allowed is not None and upc not in allowed:
            continue  # this ASIN is confirmed elsewhere; this UPC isn't its real one
        out.add((upc, asin))
    # The library's own pairs always verify (belt-and-braces with mirror rows).
    for asin, forms in asin_upcs.items():
        for form in forms:
            out.add((form, asin))
    return out


def _library_asin_upcs() -> dict[str, set[str]]:
    """{ASIN: {every equivalent barcode form of its imported upc/ean values}}.
    The set of UPCs a manually-confirmed ASIN is allowed to carry — used to
    override slipped-through system pairs for that ASIN at read time.  Expanded
    across barcode forms because verified/blacklist rows are keyed by whatever
    raw string each flow carried (raw 11-digit, zero-padded 12, EAN-13 …)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT asin, identifier FROM pair_library_ids "
            "WHERE id_type IN ('upc', 'ean')"
        ).fetchall()
    out: dict[str, set[str]] = {}
    for r in rows:
        out.setdefault(r["asin"].upper(), set()).update(
            _upc_mirror_forms(r["identifier"]))
    return out


# --------------------------------------------------------------------------- #
# Pair Library — manually confirmed identifier → ASIN mappings
# --------------------------------------------------------------------------- #
#
# One row per ASIN in pair_library; identifiers live in pair_library_ids,
# UNIQUE PER ASIN.  Invariants:
#   * an ASIN carries its own identifiers of a type: first = primary, rest = aliases
#   * the SAME UPC may sit under many ASINs (one product, many Amazon listings)
#   * upc/ean values are mirrored into verified_items so runs auto-verify them;
#     an exact blacklist entry for the pair is cleared
#   * authority is ASIN-keyed and enforced at READ time (load_verified_set):
#     for an imported ASIN, only its imported UPCs count — we never edit other
#     ASINs' rows at import, since a shared UPC legitimately has many ASINs.


def _upc_mirror_forms(v: str) -> list[str]:
    """
    Equivalent barcode forms (11↔12↔13↔14-digit) of a normalized identifier.
    verified_items is keyed by the RAW string each flow carries (verify scans
    use the unmodified catalog cell; analytics zero-pads 11→12), so the mirror
    must cover every form or library pairs silently fail to auto-approve —
    and conflicting rows stored under another form would survive the cleanup.
    """
    out = [v]
    if v.isdigit():
        if len(v) == 12:
            out.append("0" + v)            # EAN-13 form
            if v.startswith("0"):
                out.append(v[1:])           # raw 11-digit vendor form
        elif len(v) == 13:
            out.append("0" + v)            # GTIN-14 form
            if v.startswith("0"):
                out.append(v[1:])           # UPC-12 form
        elif len(v) == 14 and v.startswith("00"):
            out.append(v[2:])               # UPC-12 form
    return list(dict.fromkeys(out))


def upsert_library_pair(
    asin: str,
    ids: dict[str, list[str]],
    brand: str = "",
    manufacturer: str = "",
    amz_pack: str = "",
) -> dict:
    """
    Insert/update one Pair Library entry.  ``ids`` maps id_type → ordered list
    of identifier values ({"upc": [...], "ean": [...], "mpn": [...]}); per ASIN
    the first value of each type becomes the primary, the rest aliases.

    Semantics (manual imports are ground truth):
      * identifiers are unique PER ASIN — the same UPC may sit under many ASINs
        (one product, many Amazon listings), so we never move it off others
      * re-importing an ASIN with a new value makes it primary and demotes the
        previous ones to aliases (aliases accumulate)
      * brand/manufacturer overwrite only when non-empty in the import
      * (upc|ean, asin) mirrored into verified_items (every barcode form);
        an exact blacklist entry for the pair is cleared
      * the "only this ASIN's imported UPCs are valid for it" override is
        applied at read time in load_verified_set (ASIN-keyed) — not here

    Returns counts: created, updated, ids_added, blacklist_cleared.
    """
    asin = str(asin or "").strip().upper()
    out = {"created": 0, "updated": 0, "ids_added": 0, "blacklist_cleared": 0}
    if not asin:
        return out

    _payload = json.dumps({"source": "pair-library", "brand": brand or ""})
    with _LOCK, _connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM pair_library WHERE asin=?", (asin,)
        ).fetchone()
        if exists:
            sets, params = ["updated_at=CURRENT_TIMESTAMP"], []
            if brand:
                sets.append("brand=?");        params.append(brand)
            if manufacturer:
                sets.append("manufacturer=?"); params.append(manufacturer)
            if amz_pack:
                sets.append("amz_pack=?");     params.append(amz_pack)
            params.append(asin)
            conn.execute(
                f"UPDATE pair_library SET {', '.join(sets)} WHERE asin=?", params
            )
            out["updated"] = 1
        else:
            conn.execute(
                "INSERT INTO pair_library(asin, brand, manufacturer, amz_pack) VALUES (?, ?, ?, ?)",
                (asin, brand or "", manufacturer or "", amz_pack or ""),
            )
            out["created"] = 1

        for id_type, values in (ids or {}).items():
            vals = [v for v in (values or []) if v]
            if not vals:
                continue
            # New primary incoming for this type on THIS ASIN — demote existing.
            conn.execute(
                "UPDATE pair_library_ids SET is_primary=0 WHERE asin=? AND id_type=?",
                (asin, id_type),
            )
            for j, val in enumerate(vals):
                prev = conn.execute(
                    "SELECT 1 FROM pair_library_ids "
                    "WHERE asin=? AND id_type=? AND identifier=?",
                    (asin, id_type, val),
                ).fetchone()
                if prev is None:
                    out["ids_added"] += 1
                conn.execute(
                    "INSERT INTO pair_library_ids(asin, id_type, identifier, is_primary, added_at) "
                    "VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP) "
                    "ON CONFLICT(asin, id_type, identifier) DO UPDATE SET "
                    "  is_primary=excluded.is_primary, added_at=CURRENT_TIMESTAMP",
                    (asin, id_type, val, 1 if j == 0 else 0),
                )

                if id_type in ("upc", "ean"):
                    # Mirror into verified_items so runs auto-approve this pair.
                    # NOTE: we do NOT delete same-UPC rows on other ASINs — a
                    # shared UPC legitimately maps to many ASINs.  Every barcode
                    # form is mirrored because verified_items is keyed by the
                    # raw string each flow carries.
                    for form in _upc_mirror_forms(val):
                        conn.execute(
                            "INSERT INTO verified_items(upc, asin, data_json, review_status, verified_at) "
                            "VALUES (?, ?, ?, 'Pair Library Import', CURRENT_TIMESTAMP) "
                            "ON CONFLICT(upc, asin) DO UPDATE SET "
                            "  review_status='Pair Library Import', verified_at=CURRENT_TIMESTAMP",
                            (form, asin, _payload),
                        )
                        cur = conn.execute(
                            "DELETE FROM blacklisted_pairs WHERE upc=? AND asin=?",
                            (form, asin),
                        )
                        out["blacklist_cleared"] += cur.rowcount
    return out


def delete_library_pair(asin: str, id_type: str = "", identifier: str = "") -> dict:
    """
    Remove an incorrect Pair Library entry.

    * With ``id_type``+``identifier`` → drop just that one identifier from the ASIN.
    * Otherwise → drop the whole ASIN (all its identifiers).

    Reverses the verified_items mirror too: any 'Pair Library Import' verified row
    for this ASIN whose barcode form is no longer backed by a remaining upc/ean
    identifier is deleted, so runs stop auto-approving the bad pair. If the ASIN has
    no identifiers left, its pair_library row is removed. (Blacklist is untouched —
    a shared UPC may still be valid for other ASINs.)
    """
    asin = str(asin or "").strip().upper()
    out = {"deleted_ids": 0, "deleted_asin": 0, "verified_cleared": 0}
    if not asin:
        return out
    with _LOCK, _connect() as conn:
        if id_type and identifier:
            cur = conn.execute(
                "DELETE FROM pair_library_ids WHERE asin=? AND id_type=? AND identifier=?",
                (asin, str(id_type).strip().lower(), str(identifier).strip()),
            )
        else:
            cur = conn.execute("DELETE FROM pair_library_ids WHERE asin=?", (asin,))
        out["deleted_ids"] = cur.rowcount

        # Barcode forms still legitimately mirrored for this ASIN after the delete.
        keep_forms: set[str] = set()
        for row in conn.execute(
            "SELECT identifier FROM pair_library_ids "
            "WHERE asin=? AND id_type IN ('upc','ean')", (asin,)
        ).fetchall():
            keep_forms.update(_upc_mirror_forms(row["identifier"]))

        for vr in conn.execute(
            "SELECT upc FROM verified_items "
            "WHERE asin=? AND review_status='Pair Library Import'", (asin,)
        ).fetchall():
            if vr["upc"] not in keep_forms:
                c = conn.execute(
                    "DELETE FROM verified_items WHERE upc=? AND asin=?", (vr["upc"], asin)
                )
                out["verified_cleared"] += c.rowcount

        left = conn.execute(
            "SELECT COUNT(*) AS n FROM pair_library_ids WHERE asin=?", (asin,)
        ).fetchone()["n"]
        if left == 0:
            c = conn.execute("DELETE FROM pair_library WHERE asin=?", (asin,))
            out["deleted_asin"] = c.rowcount
    return out


def pair_library_stats() -> dict:
    """Totals + per-brand counts for the Pair Manager UI."""
    with _connect() as conn:
        pairs = conn.execute("SELECT COUNT(*) AS n FROM pair_library").fetchone()["n"]
        idents = conn.execute("SELECT COUNT(*) AS n FROM pair_library_ids").fetchone()["n"]
        brands = conn.execute(
            "SELECT COALESCE(NULLIF(TRIM(brand), ''), '(No brand)') AS brand, "
            "       COUNT(*) AS n "
            "FROM pair_library GROUP BY 1 ORDER BY n DESC, brand"
        ).fetchall()
    return {
        "pairs": pairs,
        "identifiers": idents,
        "brands": [{"brand": r["brand"], "count": r["n"]} for r in brands],
    }


def pair_library_rows(brand: str = "") -> list[dict]:
    """
    Library entries (one dict per ASIN) with grouped identifiers.
    ``brand``: case-insensitive exact filter; empty returns everything.
    Per type: primary value + comma-joined aliases.
    """
    with _connect() as conn:
        if brand:
            lib = conn.execute(
                "SELECT * FROM pair_library WHERE brand=? COLLATE NOCASE "
                "ORDER BY asin",
                (brand,),
            ).fetchall()
        else:
            lib = conn.execute(
                "SELECT * FROM pair_library ORDER BY brand, asin"
            ).fetchall()
        ids = conn.execute(
            "SELECT id_type, identifier, asin, is_primary FROM pair_library_ids "
            "ORDER BY is_primary DESC, added_at DESC, identifier"
        ).fetchall()

    grouped: dict[str, dict[str, list[str]]] = {}
    for r in ids:  # primary-first ordering preserved
        grouped.setdefault(r["asin"], {}).setdefault(r["id_type"], []).append(r["identifier"])

    out: list[dict] = []
    for r in lib:
        a = r["asin"]
        g = grouped.get(a, {})

        def _split(t: str) -> tuple[str, str]:
            vals = g.get(t, [])
            return (vals[0] if vals else "", ", ".join(vals[1:]))

        upc, upc_al = _split("upc")
        ean, ean_al = _split("ean")
        mpn, mpn_al = _split("mpn")
        out.append({
            "asin": a,
            "brand": r["brand"] or "",
            "manufacturer": r["manufacturer"] or "",
            "amz_pack": (r["amz_pack"] if "amz_pack" in r.keys() else "") or "",
            "updated_at": r["updated_at"],
            "upc": upc, "upc_aliases": upc_al,
            "ean": ean, "ean_aliases": ean_al,
            "mpn": mpn, "mpn_aliases": mpn_al,
        })
    return out


def load_pair_library_map() -> dict[tuple[str, str], set[str]]:
    """{(id_type, identifier): {asins}} — an identifier (esp. a UPC) can map to
    MANY ASINs (one product, many Amazon listings), so each key holds a set."""
    out: dict[tuple[str, str], set[str]] = {}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id_type, identifier, asin FROM pair_library_ids"
        ).fetchall()
    for r in rows:
        out.setdefault((r["id_type"], r["identifier"]), set()).add(r["asin"].upper())
    return out


def search_pair_library(query: str) -> list[dict]:
    """Search the Pair Library by identifier, ASIN, or brand (partial match)."""
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q.upper()}%"
    with _connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT p.asin FROM pair_library p "
            "LEFT JOIN pair_library_ids i ON i.asin = p.asin "
            "WHERE UPPER(p.asin) LIKE ? OR UPPER(p.brand) LIKE ? "
            "   OR UPPER(i.identifier) LIKE ? "
            "ORDER BY p.asin LIMIT 200",
            (like, like, like),
        ).fetchall()
    wanted = {r["asin"] for r in rows}
    if not wanted:
        return []
    return [r for r in pair_library_rows() if r["asin"] in wanted]


# --------------------------------------------------------------------------- #
# Global ASIN cache (SP-API normalized data, cross-run)
# --------------------------------------------------------------------------- #

def save_asin_to_cache(asin: str, normalized: dict) -> None:
    """Save a normalized SP-API item to the global ASIN cache (without _raw to save space)."""
    if not asin:
        return
    slim = {k: v for k, v in normalized.items() if k != "_raw"}
    with _LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO global_asin_cache(asin, data_json, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(asin) DO UPDATE SET "
            "  data_json=excluded.data_json, updated_at=CURRENT_TIMESTAMP",
            (str(asin).strip().upper(), json.dumps(slim, default=str)),
        )


def load_asin_cache(asins: list[str]) -> dict[str, dict]:
    """Return cached normalized data for the given ASINs, keyed by ASIN."""
    if not asins:
        return {}
    placeholders = ",".join("?" * len(asins))
    upper = [a.strip().upper() for a in asins]
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT asin, data_json FROM global_asin_cache WHERE asin IN ({placeholders})",
            upper,
        ).fetchall()
    result = {}
    for r in rows:
        try:
            result[r["asin"]] = json.loads(r["data_json"])
        except (ValueError, TypeError):
            pass
    return result


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
    try:
        raw_mm = d.get("match_methods")
        d["match_methods"] = json.loads(raw_mm) if raw_mm else []
    except (json.JSONDecodeError, TypeError):
        d["match_methods"] = []
    return d


def create_scan(
    name: str,
    mapping: dict,
    marketplace: str = "US",
    condition: str = "New",
    catalog_filename: str | None = None,
    catalog_count: int = 0,
    ai_mode: bool = False,
    match_from_keepa: bool = False,
    match_methods: list | None = None,
) -> int:
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "INSERT INTO scans(name, marketplace, condition, mapping_json, "
            "    catalog_filename, catalog_count, status, ai_mode, "
            "    match_from_keepa, match_methods) "
            "VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (name, marketplace, condition, json.dumps(mapping, default=str),
             catalog_filename, int(catalog_count), 1 if ai_mode else 0,
             1 if match_from_keepa else 0,
             json.dumps(match_methods or []) if match_methods else None),
        )
        return cur.lastrowid


_SCANS_COLS = (
    "id, name, marketplace, condition, status, mapping_json, "
    "catalog_filename, catalog_count, amazon_filename, "
    "amazon_source, amazon_count, ai_mode, verified_count, "
    "review_count, not_approved_count, reviewed_count, "
    "match_from_keepa, match_methods, "
    "exported_at, created_at, updated_at"
)


def list_scans() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT {_SCANS_COLS} FROM scans ORDER BY datetime(created_at) DESC"
        ).fetchall()
    return [_scan_row_to_dict(r) for r in rows]


def get_scan(scan_id: int) -> dict | None:
    with _connect() as conn:
        row = conn.execute(
            f"SELECT {_SCANS_COLS} FROM scans WHERE id=?", (scan_id,),
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
        "ai_mode", "match_from_keepa", "match_methods",
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


def save_scan_candidates(scan_id: int, candidates: list[dict]) -> int:
    """Upsert all candidates for a scan (used by match-from-Keepa mode)."""
    with _LOCK, _connect() as conn:
        conn.execute("DELETE FROM scan_candidates WHERE scan_id=?", (scan_id,))
        conn.executemany(
            "INSERT OR REPLACE INTO scan_candidates"
            "(scan_id, row_idx, asin, confidence, verdict, review_status, match_method, data_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    scan_id,
                    c["row_idx"],
                    str(c.get("asin") or "").strip().upper(),
                    float(c.get("confidence") or 0),
                    c.get("verdict") or "review",
                    c.get("review_status") or "",
                    c.get("match_method") or "",
                    json.dumps(c, default=str),
                )
                for c in candidates
            ],
        )
    return len(candidates)


def load_scan_candidates(scan_id: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, row_idx, asin, confidence, verdict, review_status, match_method, data_json "
            "FROM scan_candidates WHERE scan_id=? "
            "ORDER BY row_idx, confidence DESC",
            (scan_id,),
        ).fetchall()
    out = []
    for r in rows:
        try:
            payload = json.loads(r["data_json"])
        except json.JSONDecodeError:
            payload = {}
        payload["_cand_id"]      = r["id"]
        payload["row_idx"]       = r["row_idx"]
        payload["asin"]          = r["asin"]
        payload["confidence"]    = r["confidence"]
        payload["verdict"]       = r["verdict"]
        payload["review_status"] = r["review_status"] or payload.get("review_status", "")
        payload["match_method"]  = r["match_method"]
        out.append(payload)
    return out


def get_scan_candidate(cand_id: int) -> dict | None:
    with _connect() as conn:
        r = conn.execute(
            "SELECT id, scan_id, row_idx, asin, confidence, verdict, review_status, match_method, data_json "
            "FROM scan_candidates WHERE id=?",
            (cand_id,),
        ).fetchone()
    if not r:
        return None
    try:
        payload = json.loads(r["data_json"])
    except json.JSONDecodeError:
        payload = {}
    payload["_cand_id"]      = r["id"]
    payload["scan_id"]       = r["scan_id"]
    payload["row_idx"]       = r["row_idx"]
    payload["asin"]          = r["asin"]
    payload["confidence"]    = r["confidence"]
    payload["verdict"]       = r["verdict"]
    payload["review_status"] = r["review_status"]
    payload["match_method"]  = r["match_method"]
    return payload


def update_scan_candidate(cand_id: int, verdict: str, review_status: str, data: dict) -> None:
    with _LOCK, _connect() as conn:
        conn.execute(
            "UPDATE scan_candidates SET verdict=?, review_status=?, data_json=? WHERE id=?",
            (verdict, review_status, json.dumps(data, default=str), cand_id),
        )


def get_asin_approved_for_scan(scan_id: int, asin: str) -> int | None:
    """Return the row_idx that has already approved this ASIN, or None."""
    with _connect() as conn:
        r = conn.execute(
            "SELECT row_idx FROM scan_candidates "
            "WHERE scan_id=? AND asin=? AND review_status='Approved'",
            (scan_id, asin.upper()),
        ).fetchone()
    return r["row_idx"] if r else None


def recompute_scan_stats(scan_id: int) -> dict:
    """Derive verified/review/not_approved/reviewed counts from scan_results."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT verdict, review_status FROM scan_results WHERE scan_id=?",
            (scan_id,),
        ).fetchall()
    verified = review = not_approved = reviewed = 0
    for r in rows:
        v = (r["verdict"] or "").strip().lower().replace("_", " ")
        if v in ("approved", "verified"):
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

_RESETTABLE_TABLES: frozenset[str] = frozenset({
    "scans", "scan_catalog_rows", "scan_amazon_rows", "scan_results", "scan_candidates",
    "analytics_runs", "analytics_catalog_rows", "analytics_candidates",
    "brand_analytics_runs", "brand_analytics_items", "brand_library",
    "keepa_imports", "amazon_imports", "blacklisted_pairs", "verified_items",
    "attribute_cache", "abbreviation_library", "global_asin_cache",
    "asin_identifier_overrides",
})


def reset_tables(tables: Iterable[str]) -> None:
    """Developer-only. Never called from production paths.
    Only tables in _RESETTABLE_TABLES are accepted to prevent SQL injection."""
    safe = [t for t in tables if t in _RESETTABLE_TABLES]
    with _LOCK, _connect() as conn:
        for t in safe:
            conn.execute(f"DELETE FROM {t}")


# Make sure the DB exists at import time. Keeps the rest of the code simple.
init_db()


def reset_orphaned_running_states() -> None:
    """On server startup, any run/check left in 'Running' state from a previous
    process is now an orphan — the thread that drove it is dead.  Reset them to
    'Stopped' so the UI doesn't show a永 spinner and the stop button works."""
    with _LOCK, _connect() as conn:
        conn.execute(
            "UPDATE analytics_runs SET ai_check_status='Stopped' "
            "WHERE ai_check_status='Running'"
        )
        # Also clear stuck brand-analytics run statuses (Searching → Stopped)
        conn.execute(
            "UPDATE brand_analytics_runs SET status='Stopped' "
            "WHERE status IN ('Searching','Pending')"
        )
        # AI Fill runs in a daemon thread that dies with the process; a restart
        # (or a mid-loop crash) leaves ai_fill_status stuck on 'running' with the
        # progress frozen. Reset it so the UI un-freezes and re-enables AI Fill.
        conn.execute(
            "UPDATE brand_analytics_runs SET ai_fill_status='stopped' "
            "WHERE ai_fill_status='running'"
        )
