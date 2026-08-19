"""
Brand Analytics endpoints.

Library CRUD:
  GET    /brand-analytics/library
  POST   /brand-analytics/library
  DELETE /brand-analytics/library/{id}

Discovery:
  POST   /brand-analytics/discover

Runs:
  POST   /brand-analytics/runs
  GET    /brand-analytics/runs
  GET    /brand-analytics/runs/{id}
  POST   /brand-analytics/runs/{id}/control
  DELETE /brand-analytics/runs/{id}
  PATCH  /brand-analytics/runs/{id}/items/{asin}
  POST   /brand-analytics/runs/{id}/ai_fill
  GET    /brand-analytics/runs/{id}/export
  GET    /brand-analytics/runs/{id}/categories
  POST   /brand-analytics/runs/{id}/filter-categories
"""
from __future__ import annotations

import io
import json
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import StreamingResponse
from openpyxl import Workbook
from openpyxl.styles import Font
from pydantic import BaseModel

from services import database
from services.ai_recheck import make_client
from services.safety import clamp_int, safe_spreadsheet_row
from services.analytics.brand_discovery import discover_brands
from services.analytics.brand_runner import (
    start_brand_run,
    start_ai_fill,
    request_pause,
    request_stop,
    clear_control,
    request_stop_ai_fill,
    _clean_ai_fill_fields,
)

router = APIRouter()

_HDR_FONT = Font(bold=True)


# --------------------------------------------------------------------------- #
# DB helpers
# --------------------------------------------------------------------------- #

def _connect():
    return database._connect()


def _get_run(run_id: int) -> dict:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM brand_analytics_runs WHERE id=?", (run_id,)
        ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Run not found")
    d = dict(row)
    d["search_terms"] = json.loads(d.get("search_terms") or "[]")
    return d


# --------------------------------------------------------------------------- #
# Library endpoints
# --------------------------------------------------------------------------- #

class LibraryEntryBody(BaseModel):
    entity_type: str
    name: str
    parent_manufacturer: str | None = None
    sub_brands: list[str] = []
    aliases: list[str] = []
    discovered_by: str = "user"


@router.get("/brand-analytics/library")
async def get_library() -> dict:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT * FROM brand_library ORDER BY entity_type, name"
        ).fetchall()
    entries = []
    for r in rows:
        d = dict(r)
        d["sub_brands"] = json.loads(d.get("sub_brands") or "[]")
        d["aliases"] = json.loads(d.get("aliases") or "[]")
        entries.append(d)
    return {"entries": entries}


@router.post("/brand-analytics/library")
async def upsert_library(body: LibraryEntryBody) -> dict:
    entity_type = body.entity_type.strip().lower()
    if entity_type not in ("brand", "manufacturer"):
        entity_type = "brand"
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    sub_brands = json.dumps([s.strip() for s in body.sub_brands if s and s.strip()])
    aliases    = json.dumps([a.strip() for a in body.aliases if a and a.strip()])
    parent     = (body.parent_manufacturer or "").strip() or None

    with database._LOCK, _connect() as conn:
        conn.execute(
            """
            INSERT INTO brand_library
              (entity_type, name, parent_manufacturer, sub_brands, aliases, discovered_by)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(name) DO UPDATE SET
              entity_type=excluded.entity_type,
              parent_manufacturer=excluded.parent_manufacturer,
              sub_brands=excluded.sub_brands,
              aliases=excluded.aliases,
              discovered_by=excluded.discovered_by,
              updated_at=CURRENT_TIMESTAMP
            """,
            (entity_type, name, parent, sub_brands, aliases, body.discovered_by or "user"),
        )
        row = conn.execute("SELECT * FROM brand_library WHERE name=?", (name,)).fetchone()

    d = dict(row)
    d["sub_brands"] = json.loads(d.get("sub_brands") or "[]")
    d["aliases"] = json.loads(d.get("aliases") or "[]")
    return {"ok": True, "entry": d}


@router.delete("/brand-analytics/library/{entry_id}")
async def delete_library_entry(entry_id: int) -> dict:
    with database._LOCK, _connect() as conn:
        cur = conn.execute("DELETE FROM brand_library WHERE id=?", (entry_id,))
    return {"deleted": cur.rowcount > 0}


# --------------------------------------------------------------------------- #
# Discovery endpoint
# --------------------------------------------------------------------------- #

class DiscoverBody(BaseModel):
    name: str
    entity_type: str = "brand"


@router.post("/brand-analytics/discover")
async def discover(body: DiscoverBody) -> dict:
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")

    # Check library cache first
    with _connect() as conn:
        cached = conn.execute(
            "SELECT * FROM brand_library WHERE LOWER(name)=LOWER(?)", (name,)
        ).fetchone()

    if cached:
        d = dict(cached)
        d["sub_brands"] = json.loads(d.get("sub_brands") or "[]")
        d["aliases"] = json.loads(d.get("aliases") or "[]")
        return {"cached": True, "result": d}

    client = make_client()
    result = discover_brands(name, body.entity_type, client)

    # Auto-save to library
    if result and not result.get("error"):
        sub_brands_json = json.dumps(result.get("sub_brands") or [])
        aliases_json    = json.dumps(result.get("aliases") or [])
        entity_type     = result.get("entity_type") or body.entity_type
        with database._LOCK, _connect() as conn:
            conn.execute(
                """
                INSERT INTO brand_library
                  (entity_type, name, sub_brands, aliases, discovered_by)
                VALUES (?, ?, ?, ?, 'ai')
                ON CONFLICT(name) DO UPDATE SET
                  sub_brands=excluded.sub_brands,
                  aliases=excluded.aliases,
                  discovered_by='ai',
                  updated_at=CURRENT_TIMESTAMP
                """,
                (entity_type, name, sub_brands_json, aliases_json),
            )

    return {"cached": False, "result": result}


# --------------------------------------------------------------------------- #
# Runs endpoints
# --------------------------------------------------------------------------- #

class CreateRunBody(BaseModel):
    name: str
    search_type: str = "brand"
    search_terms: list[str]
    min_rank: int = 0
    max_rank: int = 0
    pages_per_brand: int = 10
    library_id: int | None = None
    force_refresh: bool = False
    vetting_mode: str = "cpg"
    category_filter: str = ""


@router.post("/brand-analytics/runs")
async def create_run(body: CreateRunBody) -> dict:
    name = (body.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name is required")
    terms = [t.strip() for t in body.search_terms if t and t.strip()]
    if not terms:
        raise HTTPException(status_code=400, detail="search_terms is required")

    search_type = (body.search_type or "brand").strip().lower()
    terms_json  = json.dumps(terms)

    # Cache freshness check (unless force_refresh)
    if not body.force_refresh:
        with _connect() as conn:
            existing = conn.execute(
                """
                SELECT id, last_asin_updated_at, status
                FROM brand_analytics_runs
                WHERE search_terms=? AND status='Complete'
                ORDER BY id DESC LIMIT 1
                """,
                (terms_json,),
            ).fetchone()
        if existing:
            updated_at = existing["last_asin_updated_at"]
            if updated_at:
                try:
                    dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
                    now = datetime.now(timezone.utc)
                    age_days = (now - dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else now - dt).days
                except Exception:
                    age_days = 999
                if age_days < 7:
                    return {
                        "cached": True,
                        "age_days": age_days,
                        "run_id": existing["id"],
                        "message": f"Results from {age_days} day(s) ago are still fresh.",
                    }
                elif age_days <= 150:
                    return {
                        "cached": True,
                        "age_days": age_days,
                        "run_id": existing["id"],
                        "message": f"Results are {age_days} days old. Use cached or refresh.",
                        "stale": True,
                    }
                # > 150 days: fall through and create a new run

    vetting_mode = (body.vetting_mode or "cpg").strip().lower()
    if vetting_mode not in ("cpg", "medical"):
        vetting_mode = "cpg"
    pages_per_brand = int(body.pages_per_brand) if body.pages_per_brand >= 0 else 0
    category_filter = (body.category_filter or "").strip().lower()

    with database._LOCK, _connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO brand_analytics_runs
              (name, search_type, search_terms, library_id,
               min_rank, max_rank, pages_per_brand, vetting_mode, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Pending')
            """,
            (
                name, search_type, terms_json,
                body.library_id,
                int(body.min_rank), int(body.max_rank), pages_per_brand,
                vetting_mode,
            ),
        )
        run_id = cur.lastrowid

    start_brand_run(
        run_id,
        terms,
        int(body.min_rank),
        int(body.max_rank),
        pages_per_brand,
        vetting_mode=vetting_mode,
        category_filter=category_filter,
    )
    return {"cached": False, "run_id": run_id}


@router.get("/brand-analytics/runs")
async def list_runs() -> dict:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT r.*,
                   (SELECT COUNT(*) FROM brand_analytics_items WHERE run_id=r.id) AS item_count
            FROM brand_analytics_runs r
            ORDER BY r.id DESC
            """,
        ).fetchall()
    runs = []
    for row in rows:
        d = dict(row)
        d["search_terms"] = json.loads(d.get("search_terms") or "[]")
        runs.append(d)
    return {"runs": runs}


@router.get("/brand-analytics/runs/{run_id}")
async def get_run(
    run_id: int,
    limit: int = 50,
    offset: int = 0,
    search: str = "",
    sort_key: str = "bsr",
    sort_dir: str = "asc",
) -> dict:
    run = _get_run(run_id)
    limit = clamp_int(limit, 1, 500, 50)
    offset = max(0, int(offset or 0))

    # Item count
    with _connect() as conn:
        total_row = conn.execute(
            "SELECT COUNT(*) AS n FROM brand_analytics_items WHERE run_id=?",
            (run_id,),
        ).fetchone()
        total_items = total_row["n"] if total_row else 0

        # Stats
        stats_row = conn.execute(
            """
            SELECT
                COUNT(*) AS total,
                COUNT(CASE WHEN upc IS NOT NULL OR ai_upc IS NOT NULL THEN 1 END) AS with_upc,
                COUNT(CASE WHEN mpn IS NOT NULL OR ai_mpn IS NOT NULL THEN 1 END) AS with_mpn,
                COUNT(CASE WHEN upc IS NULL AND ean IS NULL
                               AND ai_upc IS NULL AND ai_ean IS NULL THEN 1 END) AS missing_upc,
                COUNT(CASE WHEN mpn IS NULL AND ai_mpn IS NULL THEN 1 END) AS missing_mpn
            FROM brand_analytics_items WHERE run_id=?
            """,
            (run_id,),
        ).fetchone()

        # Items query
        allowed_sort = {"bsr", "title", "brand_searched", "asin"}
        sk = sort_key if sort_key in allowed_sort else "bsr"
        sd = "ASC" if sort_dir.lower() != "desc" else "DESC"
        null_order = "NULLS LAST" if sd == "ASC" else "NULLS FIRST"

        where_clause = "WHERE run_id=?"
        params: list[Any] = [run_id]
        if search:
            where_clause += " AND (LOWER(title) LIKE ? OR LOWER(asin) LIKE ? OR LOWER(brand_searched) LIKE ?)"
            s = f"%{search.lower()}%"
            params += [s, s, s]

        items_rows = conn.execute(
            f"""
            SELECT id, run_id, brand_searched, asin, title, bsr, bsr_category,
                   mpn, upc, ean, gtin, pack_qty, uom_qty, image_url,
                   ai_mpn, ai_upc, ai_ean, ai_gtin, ai_fill_status
            FROM brand_analytics_items
            {where_clause}
            ORDER BY {sk} {sd} {null_order}
            LIMIT ? OFFSET ?
            """,
            params + [limit, offset],
        ).fetchall()

        filtered_count_row = conn.execute(
            f"SELECT COUNT(*) AS n FROM brand_analytics_items {where_clause}",
            params,
        ).fetchone()

    items = [dict(r) for r in items_rows]
    return {
        "run": run,
        "items": items,
        "total_items": total_items,
        "filtered_count": filtered_count_row["n"] if filtered_count_row else total_items,
        "stats": dict(stats_row) if stats_row else {},
    }


class ControlBody(BaseModel):
    action: str  # pause | stop | resume


@router.post("/brand-analytics/runs/{run_id}/control")
async def control_run(run_id: int, body: ControlBody) -> dict:
    run = _get_run(run_id)
    action = (body.action or "").lower()

    if action == "pause":
        request_pause(run_id)
        return {"ok": True, "action": "pause"}
    elif action == "stop":
        request_stop(run_id)
        return {"ok": True, "action": "stop"}
    elif action == "resume":
        clear_control(run_id)
        terms = run["search_terms"]
        start_brand_run(
            run_id, terms,
            run.get("min_rank") or 0,
            run.get("max_rank") or 0,
            run.get("pages_per_brand") or 10,
            vetting_mode=run.get("vetting_mode") or "cpg",
        )
        return {"ok": True, "action": "resume"}

    raise HTTPException(status_code=400, detail="action must be pause|stop|resume")


@router.delete("/brand-analytics/runs/{run_id}")
async def delete_run(run_id: int) -> dict:
    _get_run(run_id)  # 404 if not found
    request_stop(run_id)
    with database._LOCK, _connect() as conn:
        conn.execute("DELETE FROM brand_analytics_items WHERE run_id=?", (run_id,))
        conn.execute("DELETE FROM brand_analytics_runs WHERE id=?", (run_id,))
    return {"deleted": True}


class PatchItemBody(BaseModel):
    mpn: str | None = None
    upc: str | None = None
    ean: str | None = None
    gtin: str | None = None


@router.patch("/brand-analytics/runs/{run_id}/items/{asin}")
async def patch_item(run_id: int, asin: str, body: PatchItemBody) -> dict:
    # Only update fields the caller explicitly sent (not Pydantic defaults).
    provided = body.model_fields_set  # e.g. {"mpn"} when only mpn was in the JSON body

    fields: list[str] = []
    params: list[Any] = []

    for col in ("mpn", "upc", "ean", "gtin"):
        if col in provided:
            fields.append(f"{col}=?")
            val = getattr(body, col)
            params.append((val or "").strip() or None)

    if not fields:
        return {"ok": True}

    fields.append("updated_at=CURRENT_TIMESTAMP")
    params += [run_id, asin.upper()]

    asin_upper = asin.upper()

    with database._LOCK, _connect() as conn:
        cur = conn.execute(
            f"UPDATE brand_analytics_items SET {', '.join(fields)} "
            "WHERE run_id=? AND asin=?",
            params,
        )
        if cur.rowcount == 0:
            raise HTTPException(status_code=404, detail="Item not found")

        # Persist the correction globally so future brand runs for this ASIN
        # always get the user-corrected value instead of whatever Amazon returns.
        ov_fields: list[str] = []
        ov_params: list[Any] = []
        for col in ("mpn", "upc", "ean", "gtin"):
            if col in provided:
                ov_fields.append(f"{col}=?")
                val = getattr(body, col)
                ov_params.append((val or "").strip() or None)

        if ov_fields:
            ov_fields.append("updated_at=CURRENT_TIMESTAMP")
            # Ensure the row exists before updating (INSERT OR IGNORE)
            conn.execute(
                "INSERT OR IGNORE INTO asin_identifier_overrides (asin) VALUES (?)",
                (asin_upper,),
            )
            conn.execute(
                f"UPDATE asin_identifier_overrides SET {', '.join(ov_fields)} WHERE asin=?",
                ov_params + [asin_upper],
            )

    return {"ok": True}


@router.get("/brand-analytics/runs/{run_id}/ai_fill/estimate")
async def ai_fill_estimate(run_id: int, fields: str = "") -> dict:
    _get_run(run_id)  # 404 guard
    client = make_client()

    sel = _clean_ai_fill_fields([f for f in fields.split(",") if f.strip()] if fields else None)
    ai_col = {"mpn": "ai_mpn", "upc": "ai_upc", "ean": "ai_ean", "gtin": "ai_gtin"}
    cond = " OR ".join(f"({f} IS NULL AND {ai_col[f]} IS NULL)" for f in sel)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT COUNT(*) AS n FROM brand_analytics_items WHERE run_id=? AND ({cond})",
            (run_id,),
        ).fetchone()
    item_count = row["n"] if row else 0

    # Detect model
    from services.ai_recheck import _is_anthropic
    if client is None:
        model = "unavailable"
        cost_usd_est = 0.0
    elif _is_anthropic(client):
        model = "claude-haiku-4-5-20251001"
        # ~500 input tokens + ~50 output tokens per item
        cost_usd_est = item_count * (500 * 0.80 + 50 * 4.00) / 1_000_000
    else:
        model = "gpt-4o-mini"
        cost_usd_est = item_count * (500 * 0.15 + 50 * 0.60) / 1_000_000

    return {
        "item_count": item_count,
        "model": model,
        "cost_usd_est": round(cost_usd_est, 4),
        "available": client is not None,
    }


@router.post("/brand-analytics/runs/{run_id}/ai_fill")
async def trigger_ai_fill(run_id: int, body: dict = Body(default={})) -> dict:
    _get_run(run_id)  # 404 guard
    client = make_client()
    if client is None:
        raise HTTPException(status_code=503, detail="No AI client available (set ANTHROPIC_API_KEY or OPENAI_API_KEY)")
    fields = _clean_ai_fill_fields(body.get("fields"))
    start_ai_fill(run_id, client, fields)
    return {"ok": True, "message": "AI Fill started", "fields": fields}


@router.post("/brand-analytics/runs/{run_id}/ai_fill/stop")
async def stop_ai_fill(run_id: int) -> dict:
    _get_run(run_id)  # 404 guard
    request_stop_ai_fill(run_id)
    # Mark as stopped immediately so the banner hides right away;
    # the background thread will see the control flag and exit cleanly.
    with database._LOCK, _connect() as conn:
        conn.execute(
            "UPDATE brand_analytics_runs SET ai_fill_status='stopped', "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (run_id,),
        )
    return {"ok": True, "message": "AI Fill stop requested"}


@router.get("/brand-analytics/runs/{run_id}/brand-names")
async def get_run_brand_names(run_id: int) -> dict:
    """
    Return distinct Amazon brand field values found in this run's results,
    along with their ASIN counts — ordered by count descending.

    Use this to discover exactly how Amazon has the brand stored in its
    registry (e.g. user searched "Hartmann" but Amazon stores it as
    "Hartmann H" or "Paul Hartmann AG").  The user can then add those
    exact names as additional search terms for a re-run.
    """
    _get_run(run_id)  # 404 guard
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(amz_brand, '(unknown)') AS brand_name,
                   COUNT(*) AS count
            FROM brand_analytics_items
            WHERE run_id=?
            GROUP BY amz_brand
            ORDER BY count DESC
            """,
            (run_id,),
        ).fetchall()
    return {"brand_names": [dict(r) for r in rows]}


@router.get("/brand-analytics/runs/{run_id}/categories")
async def get_run_categories(run_id: int) -> dict:
    """Return distinct BSR categories present in this run with counts."""
    _get_run(run_id)  # 404 guard
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT COALESCE(NULLIF(bsr_category, ''), '(Blanks)') AS category,
                   COUNT(*) AS count
            FROM brand_analytics_items
            WHERE run_id=?
            GROUP BY bsr_category
            ORDER BY count DESC
            """,
            (run_id,),
        ).fetchall()
    return {"categories": [dict(r) for r in rows]}


class FilterCategoriesBody(BaseModel):
    remove_categories: list[str]


@router.post("/brand-analytics/runs/{run_id}/filter-categories")
async def filter_run_categories(run_id: int, body: FilterCategoriesBody) -> dict:
    """Delete items whose bsr_category is in the remove_categories list."""
    _get_run(run_id)  # 404 guard
    if not body.remove_categories:
        return {"deleted": 0, "remaining": 0}

    # Normalise: treat '(Blanks)' sentinel as NULL or empty string
    remove_set  = [c for c in body.remove_categories if c != "(Blanks)"]
    remove_blank = "(Blanks)" in body.remove_categories

    with database._LOCK, _connect() as conn:
        deleted = 0
        if remove_set:
            placeholders = ",".join("?" * len(remove_set))
            cur = conn.execute(
                f"DELETE FROM brand_analytics_items WHERE run_id=? AND bsr_category IN ({placeholders})",
                [run_id] + remove_set,
            )
            deleted += cur.rowcount
        if remove_blank:
            # Delete both NULL and empty-string bsr_category rows
            cur = conn.execute(
                "DELETE FROM brand_analytics_items WHERE run_id=? AND (bsr_category IS NULL OR bsr_category = '')",
                (run_id,),
            )
            deleted += cur.rowcount

        remaining_row = conn.execute(
            "SELECT COUNT(*) AS n FROM brand_analytics_items WHERE run_id=?", (run_id,)
        ).fetchone()
        remaining = remaining_row["n"] if remaining_row else 0

    return {"deleted": deleted, "remaining": remaining}


@router.get("/brand-analytics/runs/{run_id}/export")
async def export_run(run_id: int) -> StreamingResponse:
    run = _get_run(run_id)

    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT brand_searched, asin, title, bsr, bsr_category,
                   pack_qty, uom_qty,
                   COALESCE(upc,  ai_upc)  AS upc,
                   COALESCE(ean,  ai_ean)  AS ean,
                   COALESCE(gtin, ai_gtin) AS gtin,
                   COALESCE(mpn,  ai_mpn)  AS mpn
            FROM brand_analytics_items
            WHERE run_id=?
            ORDER BY bsr ASC NULLS LAST
            """,
            (run_id,),
        ).fetchall()

    wb = Workbook(write_only=True)
    ws = wb.create_sheet("Brand Analytics")

    headers = ["Brand", "ASIN", "Title", "BSR", "BSR Category",
               "Pack Qty", "UOM Qty",
               "UPC", "EAN", "GTIN", "MPN"]
    ws.append([c for c in headers])

    for row in rows:
        ws.append(safe_spreadsheet_row([
            row["brand_searched"],
            row["asin"],
            row["title"],
            row["bsr"],
            row["bsr_category"],
            row["pack_qty"],
            row["uom_qty"],
            row["upc"],
            row["ean"],
            row["gtin"],
            row["mpn"],
        ]))

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in run["name"])
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{safe_name}.xlsx"'},
    )
