"""
SQLite persistence layer.

All tables live in ``catalog_verifier.db`` next to ``main.py``. The module is
idempotent — importing it or calling :func:`init_db` creates tables if missing.

Tables
------
* ``keepa_imports``       — raw Keepa rows keyed by ASIN, reusable across sessions
* ``amazon_imports``      — raw Amazon export rows keyed by ASIN
* ``blacklisted_pairs``   — UPC/ASIN pairs rejected by user or engine
* ``verified_items``      — final approved rows (what eventually got exported)
* ``attribute_cache``     — normalised attributes per UPC/ASIN (with source)
* ``abbreviation_library``— categorised abbreviation/full-form mappings
* ``settings``            — key/value for tunable config (e.g. thresholds)
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

DB_PATH = Path(__file__).resolve().parent.parent / "catalog_verifier.db"
_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Connection helpers
# --------------------------------------------------------------------------- #

def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), check_same_thread=False, timeout=15.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    return conn


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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(category, abbr)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
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
    rows: list[tuple[str, str, str]] = []
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
            rows.append((category, abbr.strip(), (full or abbr).strip()))
    conn.executemany(
        "INSERT OR IGNORE INTO abbreviation_library (category, abbr, full_form) "
        "VALUES (?, ?, ?)",
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
# Imports
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


# --------------------------------------------------------------------------- #
# Verified items
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
    """Return a map of category → list of {abbr, full} entries."""
    out: dict[str, list[dict]] = {c: [] for c in VALID_CATEGORIES}
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, category, abbr, full_form FROM abbreviation_library "
            "ORDER BY category, abbr"
        ).fetchall()
    for r in rows:
        cat = r["category"] if r["category"] in VALID_CATEGORIES else "Product Attributes"
        out.setdefault(cat, []).append({
            "id": r["id"], "abbr": r["abbr"], "full": r["full_form"],
        })
    return out


def flat_library() -> list[dict]:
    """Flat list suitable for the rule-based extractor."""
    flat: list[dict] = []
    for entries in list_library().values():
        for e in entries:
            flat.append({"abbr": e["abbr"], "full": e["full"]})
    return flat


def add_library_entry(category: str, abbr: str, full: str) -> bool:
    if category not in VALID_CATEGORIES:
        category = "Product Attributes"
    abbr = (abbr or "").strip()
    full = (full or "").strip() or abbr
    if not abbr:
        return False
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO abbreviation_library(category, abbr, full_form) "
            "VALUES (?, ?, ?)",
            (category, abbr, full),
        )
        return cur.rowcount > 0


def delete_library_entry(entry_id: int) -> int:
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            "DELETE FROM abbreviation_library WHERE id=?", (entry_id,),
        )
        return cur.rowcount


def has_library_entry(abbr: str) -> bool:
    with _connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM abbreviation_library WHERE LOWER(abbr)=LOWER(?)",
            ((abbr or "").strip(),),
        ).fetchone()
    return bool(row)


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
