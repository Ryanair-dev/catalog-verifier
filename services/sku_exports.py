"""
Log of Create-SKUs exports — one row per export batch so each generated file set
is traceable (which ID, when, how many SKUs, which files). Stored in
catalog_verifier.db table ``sku_exports``.
"""
from __future__ import annotations

import json
import time

from services.database import _LOCK, _connect


def init() -> None:
    with _LOCK, _connect() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS sku_exports (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            export_id    TEXT,
            export_date  TEXT,
            created_at   TEXT,
            company      TEXT,
            batch_type   TEXT,
            total_mains  INTEGER DEFAULT 0,
            total_fba    INTEGER DEFAULT 0,
            total_fbm    INTEGER DEFAULT 0,
            total_kits   INTEGER DEFAULT 0,
            files        TEXT DEFAULT ''
        )""")
        # Additive: the actual generated file bytes (base64), so a past export can
        # be re-downloaded later without re-running generation. Exports logged
        # before this column existed simply have no blob (re-export unavailable
        # for those, filenames-only). A real Create SKUs export is small --
        # observed ~13-30KB per file uncompressed, so even thousands of exports
        # stored this way stays well under 1GB total.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(sku_exports)")}
        if "files_blob" not in cols:
            conn.execute("ALTER TABLE sku_exports ADD COLUMN files_blob TEXT")


def list_recent(limit: int = 20) -> list[dict]:
    """Most-recent exports first, with `files` parsed back to a list."""
    init()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM sku_exports ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["files"] = json.loads(d.get("files") or "[]")
        except Exception:
            d["files"] = []
        out.append(d)
    return out


def record(*, export_id: str, export_date: str, company: str = "", batch_type: str = "",
           total_mains: int = 0, total_fba: int = 0, total_fbm: int = 0,
           total_kits: int = 0, files: list[str] | None = None,
           files_blob: list[dict] | None = None) -> int:
    """Insert an export-log row; returns its DB id.

    `files` is just the filenames (shown in the history list). `files_blob` is
    the full `[{"name": ..., "b64": ...}, ...]` payload, stored so the exact
    same files can be re-downloaded later via get_files_blob() without
    re-running SKU generation.
    """
    init()
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            """INSERT INTO sku_exports
               (export_id, export_date, created_at, company, batch_type,
                total_mains, total_fba, total_fbm, total_kits, files, files_blob)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (export_id, export_date, str(time.time()), company, batch_type,
             int(total_mains), int(total_fba), int(total_fbm), int(total_kits),
             json.dumps(files or []),
             json.dumps(files_blob) if files_blob else None),
        )
        return cur.lastrowid


def get_files_blob(export_id: str) -> list[dict] | None:
    """The stored [{name, b64}] for a past export, or None if not found / logged
    before files_blob existed (re-export unavailable for that older entry)."""
    init()
    with _connect() as conn:
        row = conn.execute(
            "SELECT files_blob FROM sku_exports WHERE export_id=? ORDER BY id DESC LIMIT 1",
            (export_id,),
        ).fetchone()
    if not row or not row["files_blob"]:
        return None
    try:
        return json.loads(row["files_blob"])
    except (TypeError, ValueError):
        return None
