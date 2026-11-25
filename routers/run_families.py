"""
Families of one Analytics run, for the UI and as a download. Makes review faster.

Building the families is slow (every vendor row against every listing: ~30 s for a 10k-candidate
run), so GET /run-families/{run_id} is only called when someone opens the Families list, and the
build is cached in `run_families_cache` for CACHE_DAYS. The build never looks at verdicts, so the
cache holds only the families and each pair's relation; verdict / confidence / review status are
read fresh on every request. The cache is rebuilt early when the run's candidates or vendor rows
change, when vendor_families.FILE_VERSION changes, or on ?refresh=1.
"""
from __future__ import annotations

import gzip
import json
import os
import sqlite3
import tempfile
import threading
from collections import Counter
from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from services import database
from services.analytics.vendor_families import (
    EXPORT_MAX_CANDIDATES,
    FILE_VERSION,
    RELATIONS,
    build_families,
    load_run,
    row_label,
    write_workbook,
)

router = APIRouter()

CACHE_DAYS = 45
_CACHE_FORMAT = 1                        # bump when the cached payload's shape changes
_TABLE_READY = False
_BUILD_LOCKS: dict[int, threading.Lock] = {}
_BUILD_LOCKS_GUARD = threading.Lock()


def _load(run_id: int) -> tuple[dict, list[dict], list[dict]]:
    vendor_rows, candidates = load_run(run_id)
    if not vendor_rows:
        raise HTTPException(status_code=404, detail=f"Run {run_id} has no vendor rows")
    return build_families(vendor_rows, candidates), candidates, vendor_rows


def _plain(value: Any) -> Any:
    """Values safe for JSON (numbers and text stay, anything else becomes text)."""
    return value if isinstance(value, (int, float, str)) or value is None else str(value)


def _ai_summary(vendor_rows: list[dict], run_id: int) -> tuple[dict, dict]:
    """What the AI reader (brand_extractor.py, GPT-4o-mini) found for this run's vendor rows, and what it spent:
    ({"installed", "switch_on", "rows_total", "rows_with_result", "attributes_found", "sent", "cached",
      "cost_usd", "error", "message"}, {row number as shown: {"attributes": {...}}})."""
    totals = {"installed": True, "switch_on": False, "rows_total": len(vendor_rows), "rows_with_result": 0,
              "attributes_found": 0, "sent": 0, "cached": 0, "cost_usd": 0.0, "error": "", "message": ""}
    per_row: dict = {}
    try:
        from services import database
        from services.analytics import brand_extractor
        with database._connect() as conn:
            row = conn.execute("SELECT ai_clean_titles FROM analytics_runs WHERE id = ?", (run_id,)).fetchone()
        totals["switch_on"] = bool(row and row[0]) and brand_extractor.enabled()
        summary = brand_extractor.run_summary(run_id, vendor_rows)
        for key in ("rows_with_result", "attributes_found", "sent", "cached", "cost_usd", "error"):
            totals[key] = summary[key]
        per_row = {str(row_label(idx)): {"attributes": attrs} for idx, attrs in summary["per_row"].items()}
    except Exception as exc:  # noqa: BLE001 - brand_extractor.py missing or broken
        totals.update(installed=False, message=f"{type(exc).__name__}: {exc}")
    return totals, per_row


def _cache_conn() -> sqlite3.Connection:
    global _TABLE_READY
    conn = database._connect()
    if not _TABLE_READY:
        with database._LOCK:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS run_families_cache (
                    run_id INTEGER PRIMARY KEY,
                    version TEXT NOT NULL,          -- FILE_VERSION + cache format
                    fingerprint TEXT NOT NULL,      -- what the run looked like when it was built
                    built_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    payload BLOB NOT NULL           -- gzipped JSON, see _build_static()
                )""")
            conn.commit()
        _TABLE_READY = True
    return conn


def _cache_version() -> str:
    return f"{FILE_VERSION} / cache {_CACHE_FORMAT}"


def _fingerprint(conn: sqlite3.Connection, run_id: int) -> str:
    """Cheap signature of a run's inputs (indexed columns only, no data_json parsing): changes when
    candidates are added, removed or re-inserted, or when a vendor row or its AI extraction changes."""
    n, lo, hi = conn.execute(
        "SELECT COUNT(*), MIN(rowid), MAX(rowid) FROM analytics_candidates WHERE run_id = ?", (run_id,)).fetchone()
    rows, size = conn.execute(
        "SELECT COUNT(*), SUM(LENGTH(data_json)) + SUM(LENGTH(COALESCE(extracted_json, ''))) "
        "FROM analytics_catalog_rows WHERE run_id = ?", (run_id,)).fetchone()
    return f"{n}:{lo}:{hi}|{rows}:{size}"


def _build_static(run_id: int) -> dict:
    """The slow, verdict-free part: families plus each pair's relation, keyed "row_idx|asin"."""
    result, _, vendor_rows = _load(run_id)
    ai_totals, ai_rows = _ai_summary(vendor_rows, run_id)
    families = [{
        "id": f["id"],
        "name": f["name"],
        "brand": f["brand"],
        "words": f["words"],
        "varying": {box: [_plain(v) for v in values] for box, values in f["varying"].items()},
        "rows": [{
            "row": row_label(idx),
            "row_idx": idx,
            "title": result["rows"][idx]["title"],
            "variant": result["rows"][idx]["variant"],
            "upc": result["rows"][idx]["upc"],
            "ai": ai_rows.get(str(row_label(idx))),
        } for idx in f["rows"]],
    } for f in result["families"]]
    pairs: dict[str, dict] = {}
    for (idx, asin), p in result["pairs"].items():    # same fields as pair_table(), keyed for the live merge
        r, l = result["rows"][idx], result["listings"][asin]
        pairs[f"{idx}|{asin}"] = {
            "family": r["family"],
            "row": row_label(idx),
            "asin": asin,
            "amazon_title": l["title"],
            "amazon_brand": l["brand"],
            "listing_variant": l["variant"],
            "listing_family": l.get("family") or "",
            "relation": p["relation"],
            "why": p["detail"],
        }
    return {"families": families, "pairs": pairs, "ai": ai_totals, "ai_rows": ai_rows}


def _static(run_id: int, refresh: bool) -> tuple[dict, str]:
    """(static payload, built_at), from the cache when it is fresh, otherwise built and stored."""
    with _BUILD_LOCKS_GUARD:
        lock = _BUILD_LOCKS.setdefault(run_id, threading.Lock())
    with lock:                                   # two people opening the same run build it once
        conn = _cache_conn()
        try:
            fp = _fingerprint(conn, run_id)
            if not refresh:
                row = conn.execute(
                    "SELECT payload, built_at FROM run_families_cache WHERE run_id = ? AND version = ? "
                    "AND fingerprint = ? AND built_at >= datetime('now', ?)",
                    (run_id, _cache_version(), fp, f"-{CACHE_DAYS} days")).fetchone()
                if row:
                    return json.loads(gzip.decompress(row[0])), row[1]
        finally:
            conn.close()
        static = _build_static(run_id)
        blob = gzip.compress(json.dumps(static, separators=(",", ":")).encode("utf-8"))
        with database._LOCK:
            conn = _cache_conn()
            try:
                conn.execute("DELETE FROM run_families_cache WHERE built_at < datetime('now', ?)",
                             (f"-{CACHE_DAYS} days",))
                conn.execute("INSERT OR REPLACE INTO run_families_cache (run_id, version, fingerprint, payload) "
                             "VALUES (?, ?, ?, ?)", (run_id, _cache_version(), fp, blob))
                conn.commit()
                built_at = conn.execute("SELECT built_at FROM run_families_cache WHERE run_id = ?",
                                        (run_id,)).fetchone()[0]
            finally:
                conn.close()
        return static, built_at


def export_columns(run_id: int) -> dict[tuple, list]:
    """Family columns for the main run export (same shape as vendor_families.export_family_columns),
    read from this cache so repeat exports don't rebuild -- the rebuild took long enough that downloads
    through the Cloudflare tunnel (100 s limit) failed.  Blank columns on any problem or a very big run."""
    try:
        with database._connect() as conn:
            n = conn.execute("SELECT COUNT(*) FROM analytics_candidates WHERE run_id = ?", (run_id,)).fetchone()[0]
        if n > EXPORT_MAX_CANDIDATES:
            return {}
        static, _ = _static(run_id, False)
        row_info = {r["row_idx"]: (f["id"], f["name"], r["variant"])
                    for f in static["families"] for r in f["rows"]}
        out: dict[tuple, list] = {}
        for key, p in static["pairs"].items():
            idx_s, asin = key.split("|", 1)
            idx = int(idx_s)
            fam, name, variant = row_info.get(idx, ("", "", ""))
            out[(idx, asin)] = [fam, name, variant, p["listing_variant"], p["relation"], p["why"]]
        return out
    except Exception:  # noqa: BLE001 - the export itself must never fail because of this
        return {}


@router.get("/run-families/{run_id}")
def run_families(run_id: int, refresh: bool = False) -> dict:
    static, built_at = _static(run_id, refresh)
    with database._connect() as conn:            # live verdicts: plain columns, no data_json
        live = conn.execute(
            "SELECT row_idx, asin, verdict, confidence, review_status FROM analytics_candidates "
            "WHERE run_id = ? ORDER BY row_idx, asin", (run_id,)).fetchall()

    lines = []
    for c in live:
        p = static["pairs"].get(f"{c['row_idx']}|{c['asin']}")
        if not p:
            continue
        try:
            conf = float(c["confidence"])
        except (TypeError, ValueError):
            conf = None
        lines.append({**p, "verdict": c["verdict"] or "", "confidence": conf,
                      "review_status": c["review_status"] or ""})

    order = {f["id"]: n for n, f in enumerate(static["families"])}

    def row_key(value: Any) -> tuple:
        try:
            return (0, float(value), "")
        except (TypeError, ValueError):
            return (1, 0.0, str(value))
    lines.sort(key=lambda d: (order.get(d["family"], 999), row_key(d["row"]),
                              RELATIONS.index(d["relation"]), -(d["confidence"] or 0)))

    counts: dict[str, Counter] = {}
    for d in lines:
        counts.setdefault(d["family"], Counter())[d["relation"]] += 1
    families = [{**f, "counts": dict(counts.get(f["id"], {}))} for f in static["families"]]

    return {"run_id": run_id, "version": FILE_VERSION, "relations": list(RELATIONS),
            "families": families, "pairs": lines, "ai": static["ai"], "ai_rows": static["ai_rows"],
            "built_at": built_at, "cache_days": CACHE_DAYS}


@router.get("/run-families/{run_id}/export")
def run_families_export(run_id: int) -> Response:
    result, candidates, _ = _load(run_id)
    handle, path = tempfile.mkstemp(suffix=".xlsx")
    os.close(handle)
    try:
        write_workbook(result, candidates, path)
        with open(path, "rb") as fh:
            data = fh.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return Response(
        content=data,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="run_{run_id}_families.xlsx"'},
    )