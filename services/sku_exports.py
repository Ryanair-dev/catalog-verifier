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
           total_kits: int = 0, files: list[str] | None = None) -> int:
    """Insert an export-log row; returns its DB id."""
    init()
    with _LOCK, _connect() as conn:
        cur = conn.execute(
            """INSERT INTO sku_exports
               (export_id, export_date, created_at, company, batch_type,
                total_mains, total_fba, total_fbm, total_kits, files)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (export_id, export_date, str(time.time()), company, batch_type,
             int(total_mains), int(total_fba), int(total_fbm), int(total_kits),
             json.dumps(files or [])),
        )
        return cur.lastrowid
