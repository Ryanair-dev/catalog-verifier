"""
Background worker for Analytics runs.

Pipeline (per run):
  Tier 1 — UPC batch search       (SP-API searchCatalogItems, identifiers)
  Tier 2 — Item ID keyword search (SP-API searchCatalogItems, keywords)
  Tier 3 — Title keyword search   (SP-API searchCatalogItems, keywords + paging)

Each candidate ASIN is scored with services.analytics.matcher against
the source row. If the same ASIN comes back from multiple tiers, we
dedupe and record which tiers contributed via the `sources` column.

Progress is reported by updating the run row in `analytics_runs` so the
UI's `loadAnalyticsRuns()` polling can show live phase / done / total.

This mirrors `AmazonAsinResearch1/app/services/amazon_client.py`'s
`batch_keyword_search` / `batch_identifier_search` flow, but skipped the
Celery / SQLAlchemy layer -- we run in a plain `threading.Thread` and
persist directly via `sqlite3`, matching the rest of catalog-verifier.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from datetime import datetime
from typing import Any, Iterable

from services import database
from services.analytics.matcher import calculate_confidence
from services.analytics.parser import SourceRow
from services.spapi import get_catalog_api, sp_api_configured
from services.spapi.catalog import CatalogAPI

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Tunables — match AmazonAsinResearch1 defaults
# --------------------------------------------------------------------------- #

UPC_BATCH_SIZE = 20            # SP-API hard cap on identifiers per call
DEFAULT_TITLE_MAX_PAGES = 5    # what the wizard exposes (Analytics panel)
PAGE_SLEEP = 0.6               # seconds between paged keyword calls (safety)

# Verdict thresholds (mirrors AmazonAsinResearch1 config defaults).
MIN_CONFIDENCE = 30.0
AUTO_APPROVE = 90.0
REVIEW_FLOOR = 35.0


# --------------------------------------------------------------------------- #
# Amazon item -> normalized dict the matcher understands
# --------------------------------------------------------------------------- #


def _first(items: list | None, key: str = "value") -> Any:
    """Pick `items[0][key]` defensively — SP-API items are dicts-of-lists."""
    if not items:
        return None
    first = items[0]
    if isinstance(first, dict):
        return first.get(key)
    return first


def normalize_amazon_item(raw: dict) -> dict:
    """
    Flatten a single SP-API Catalog Items v2022-04-01 item into the dict
    shape `matcher.calculate_confidence` expects.
    """
    asin = raw.get("asin", "")

    summaries = raw.get("summaries") or []
    summary = summaries[0] if summaries else {}

    identifiers = raw.get("identifiers") or []
    upcs, eans = [], []
    for ident_group in identifiers:
        for ident in ident_group.get("identifiers", []):
            t = (ident.get("identifierType") or "").upper()
            v = (ident.get("identifier") or "").strip()
            if t == "UPC" and v:
                upcs.append(v)
            elif t == "EAN" and v:
                eans.append(v)

    attrs = raw.get("attributes") or {}

    def _attr_str(key: str) -> str:
        v = _first(attrs.get(key))
        return str(v).strip() if v is not None else ""

    # Pull bullet points + description from attributes (structure varies).
    bullets: list[str] = []
    for bp in (attrs.get("bullet_point") or []):
        if isinstance(bp, dict) and bp.get("value"):
            bullets.append(str(bp["value"]).strip())
    description = _attr_str("product_description")

    return {
        "asin": asin,
        "title": summary.get("itemName") or _attr_str("item_name"),
        "brand": summary.get("brand") or _attr_str("brand"),
        "manufacturer": summary.get("manufacturer") or _attr_str("manufacturer"),
        "mpn": summary.get("partNumber") or summary.get("modelNumber") or _attr_str("part_number"),
        "upc": upcs[0] if upcs else "",
        "ean": eans[0] if eans else "",
        "description": description,
        "bullet_points": bullets,
        "item_package_quantity": _attr_str("item_package_quantity"),
        "number_of_items": _attr_str("number_of_items"),
        "attributes": {
            "color": _attr_str("color"),
            "size": _attr_str("size"),
            "material": _attr_str("material"),
        },
        # Preserve the original for later UI drill-down.
        "_raw": raw,
    }


# --------------------------------------------------------------------------- #
# DB helpers (thin wrappers over services.database._connect)
# --------------------------------------------------------------------------- #


def _with_conn():
    return database._connect()


def _create_run(
    name: str,
    marketplace: str,
    search_methods: list[str],
    pages_per_title: int,
    ai_clean_titles: bool,
    total_catalog_items: int,
) -> int:
    with database._LOCK, _with_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO analytics_runs
              (name, marketplace, search_methods, pages_per_title,
               ai_clean_titles, total_catalog_items, status,
               progress_phase, progress_done, progress_total)
            VALUES (?, ?, ?, ?, ?, ?, 'Pending', 'Queued', 0, ?)
            """,
            (
                name, marketplace, json.dumps(search_methods),
                int(pages_per_title), 1 if ai_clean_titles else 0,
                int(total_catalog_items), int(total_catalog_items),
            ),
        )
        return cur.lastrowid


def _save_catalog_rows(run_id: int, rows: Iterable[SourceRow]) -> None:
    with database._LOCK, _with_conn() as conn:
        conn.execute(
            "DELETE FROM analytics_catalog_rows WHERE run_id=?", (run_id,),
        )
        conn.executemany(
            "INSERT INTO analytics_catalog_rows(run_id, row_idx, data_json) "
            "VALUES (?, ?, ?)",
            [(run_id, r.row_idx, json.dumps(r.as_dict(), default=str))
             for r in rows],
        )


def _update_progress(
    run_id: int,
    phase: str | None = None,
    done: int | None = None,
    total: int | None = None,
    status: str | None = None,
) -> None:
    fields: list[str] = []
    params: list[Any] = []
    if phase is not None:
        fields.append("progress_phase=?"); params.append(phase)
    if done is not None:
        fields.append("progress_done=?");  params.append(int(done))
    if total is not None:
        fields.append("progress_total=?"); params.append(int(total))
    if status is not None:
        fields.append("status=?");         params.append(status)
    if not fields:
        return
    fields.append("updated_at=CURRENT_TIMESTAMP")
    params.append(run_id)
    with database._LOCK, _with_conn() as conn:
        conn.execute(
            f"UPDATE analytics_runs SET {', '.join(fields)} WHERE id=?",
            params,
        )


def _upsert_candidate(
    run_id: int,
    row_idx: int,
    normalized: dict,
    source: dict,
    sources: list[str],
) -> str:
    """
    Score the candidate, write/merge it into analytics_candidates, and
    return the verdict string. If the ASIN already exists for this row
    from a prior tier, we keep the higher confidence and union the
    `sources` list.
    """
    asin = (normalized.get("asin") or "").strip().upper()
    if not asin:
        return "skip"

    scores = calculate_confidence(source, normalized)
    conf = scores["confidence_score"]
    if conf >= AUTO_APPROVE or scores["upc_match"]:
        verdict = "verified"
    elif conf >= REVIEW_FLOOR:
        verdict = "review"
    elif conf >= MIN_CONFIDENCE:
        verdict = "review"
    else:
        verdict = "not_approved"

    # amz_pack used by the UI for at-a-glance pack quantity differences.
    amz_pack_raw = normalized.get("item_package_quantity") or normalized.get("number_of_items")
    try:
        amz_pack = int(float(amz_pack_raw)) if amz_pack_raw else None
    except (TypeError, ValueError):
        amz_pack = None

    data_json = json.dumps({
        "asin": asin,
        "scores": scores,
        "amazon": normalized,
        "verdict": verdict,
    }, default=str)

    with database._LOCK, _with_conn() as conn:
        existing = conn.execute(
            "SELECT sources, confidence FROM analytics_candidates "
            "WHERE run_id=? AND row_idx=? AND asin=?",
            (run_id, row_idx, asin),
        ).fetchone()

        if existing is None:
            conn.execute(
                "INSERT INTO analytics_candidates"
                "(run_id, row_idx, asin, sources, confidence, verdict, amz_pack, data_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, row_idx, asin, json.dumps(sources),
                 conf, verdict, amz_pack, data_json),
            )
        else:
            try:
                prev_sources = json.loads(existing["sources"] or "[]")
            except (TypeError, ValueError):
                prev_sources = []
            merged = list(dict.fromkeys([*prev_sources, *sources]))  # dedupe, keep order
            new_conf = max(conf, float(existing["confidence"] or 0))
            conn.execute(
                "UPDATE analytics_candidates SET sources=?, confidence=?, "
                "  verdict=?, amz_pack=?, data_json=? "
                "WHERE run_id=? AND row_idx=? AND asin=?",
                (json.dumps(merged), new_conf, verdict, amz_pack, data_json,
                 run_id, row_idx, asin),
            )

    return verdict


def _recompute_run_counts(run_id: int) -> None:
    with database._LOCK, _with_conn() as conn:
        rows = conn.execute(
            "SELECT verdict, COUNT(*) AS n FROM analytics_candidates "
            "WHERE run_id=? GROUP BY verdict",
            (run_id,),
        ).fetchall()
        verified = review = not_approved = total = 0
        for r in rows:
            v = (r["verdict"] or "").lower()
            n = int(r["n"])
            total += n
            if v == "verified":       verified += n
            elif v == "review":       review += n
            elif v == "not_approved": not_approved += n
        conn.execute(
            "UPDATE analytics_runs SET total_candidates_found=?, "
            "  verified_count=?, review_count=?, not_approved_count=?, "
            "  updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (total, verified, review, not_approved, run_id),
        )


# --------------------------------------------------------------------------- #
# SP-API helpers — Tier 1/2/3 searches
# --------------------------------------------------------------------------- #


def _chunks(seq: list, size: int) -> Iterable[list]:
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def _tier1_upc(
    api: CatalogAPI,
    rows: list[SourceRow],
) -> dict[int, list[dict]]:
    """Batch up to 20 UPCs per SP-API call. Returns {row_idx: [normalized items]}."""
    upc_to_rows: dict[str, list[int]] = {}
    for r in rows:
        if r.upc:
            upc_to_rows.setdefault(r.upc, []).append(r.row_idx)

    out: dict[int, list[dict]] = {}
    upcs = list(upc_to_rows.keys())
    if not upcs:
        return out

    for batch in _chunks(upcs, UPC_BATCH_SIZE):
        try:
            data = api.search_by_identifiers(batch, id_type="UPC")
        except Exception as exc:
            log.warning("UPC batch failed (%s): %s", len(batch), exc)
            continue
        items = data.get("items") or []
        # SP-API returns items with no hint about which UPC they came from,
        # so we look inside each item's identifiers for the match.
        for raw in items:
            normalized = normalize_amazon_item(raw)
            hit_upcs = {normalized.get("upc"), normalized.get("ean")}
            hit_upcs.discard(None); hit_upcs.discard("")
            for upc in hit_upcs & set(upc_to_rows.keys()):
                for ri in upc_to_rows[upc]:
                    out.setdefault(ri, []).append(normalized)
    return out


def _tier2_itemid(
    api: CatalogAPI,
    rows: list[SourceRow],
) -> dict[int, list[dict]]:
    """One keyword search per unique Item ID string."""
    term_to_rows: dict[str, list[int]] = {}
    for r in rows:
        if r.itemid:
            term_to_rows.setdefault(r.itemid, []).append(r.row_idx)

    out: dict[int, list[dict]] = {}
    for term, row_idxs in term_to_rows.items():
        try:
            data = api.search_by_keywords(term, page_size=20)
        except Exception as exc:
            log.warning("ItemID search failed for %r: %s", term, exc)
            continue
        items = data.get("items") or []
        normalized = [normalize_amazon_item(it) for it in items]
        for ri in row_idxs:
            out.setdefault(ri, []).extend(normalized)
    return out


def _tier3_title(
    api: CatalogAPI,
    rows: list[SourceRow],
    max_pages: int,
) -> dict[int, list[dict]]:
    """
    One paginated keyword search per unique title. Titles are used
    verbatim for now — the optional GPT clean-up step is a follow-up.
    """
    term_to_rows: dict[str, list[int]] = {}
    for r in rows:
        if r.title:
            # Add brand prefix if we have it — dramatically improves relevance.
            term = f"{r.brand} {r.title}".strip() if r.brand else r.title
            term_to_rows.setdefault(term, []).append(r.row_idx)

    out: dict[int, list[dict]] = {}
    for term, row_idxs in term_to_rows.items():
        page_token = None
        collected: list[dict] = []
        for page in range(max(1, int(max_pages))):
            try:
                data = api.search_by_keywords(term, page_token=page_token, page_size=20)
            except Exception as exc:
                log.warning("Title search failed for %r: %s", term, exc)
                break
            items = data.get("items") or []
            collected.extend(normalize_amazon_item(it) for it in items)
            page_token = (data.get("pagination") or {}).get("nextToken")
            if not page_token:
                break
            if page < max_pages - 1:
                time.sleep(PAGE_SLEEP)
        for ri in row_idxs:
            out.setdefault(ri, []).extend(collected)
    return out


# --------------------------------------------------------------------------- #
# Main entrypoint
# --------------------------------------------------------------------------- #


def start_analytics_run(
    *,
    name: str,
    marketplace: str,
    search_methods: list[str],
    pages_per_title: int,
    ai_clean_titles: bool,
    source_rows: list[SourceRow],
) -> int:
    """
    Create an analytics_runs row, persist source rows, and kick off a
    background thread that runs the 3-tier search + scoring. Returns
    the new run id so the caller can redirect/poll.
    """
    run_id = _create_run(
        name=name, marketplace=marketplace, search_methods=search_methods,
        pages_per_title=pages_per_title, ai_clean_titles=ai_clean_titles,
        total_catalog_items=len(source_rows),
    )
    _save_catalog_rows(run_id, source_rows)

    # Run in a daemon thread so uvicorn shuts down cleanly. This is
    # fine for the local-first catalog-verifier; a larger deployment
    # would swap this for a proper queue.
    thread = threading.Thread(
        target=_run_pipeline,
        args=(run_id, marketplace, search_methods, pages_per_title, source_rows),
        daemon=True,
    )
    thread.start()
    return run_id


def _run_pipeline(
    run_id: int,
    marketplace: str,
    search_methods: list[str],
    pages_per_title: int,
    source_rows: list[SourceRow],
) -> None:
    """Do the work. All errors are caught and recorded on the run row."""
    try:
        if not sp_api_configured():
            _update_progress(
                run_id, status="Error",
                phase="SP-API credentials not configured. Add them to .env and restart.",
            )
            return

        api = get_catalog_api(marketplace)
        total = len(source_rows)
        done = 0
        _update_progress(run_id, status="Searching", phase="Starting", done=0, total=total)

        candidates_by_row: dict[int, list[tuple[dict, str]]] = {i: [] for i in range(total)}
        idx_to_row = {r.row_idx: r for r in source_rows}

        # --- Tier 1: UPC -----------------------------------------------------
        if "UPC" in search_methods:
            _update_progress(run_id, phase="Tier 1 / UPC batch search")
            tier1 = _tier1_upc(api, source_rows)
            for ri, items in tier1.items():
                for it in items:
                    candidates_by_row.setdefault(ri, []).append((it, "UPC"))

        # --- Tier 2: Item ID -------------------------------------------------
        if "ItemID" in search_methods:
            _update_progress(run_id, phase="Tier 2 / Item ID search")
            tier2 = _tier2_itemid(api, source_rows)
            for ri, items in tier2.items():
                for it in items:
                    candidates_by_row.setdefault(ri, []).append((it, "ItemID"))

        # --- Tier 3: Title ---------------------------------------------------
        if "Title" in search_methods:
            _update_progress(run_id, phase="Tier 3 / Title search")
            tier3 = _tier3_title(api, source_rows, pages_per_title)
            for ri, items in tier3.items():
                for it in items:
                    candidates_by_row.setdefault(ri, []).append((it, "Title"))

        # --- Vetting: score every (row × ASIN) pair --------------------------
        _update_progress(run_id, phase="Vetting candidates", done=0, total=total)
        for r in source_rows:
            source_dict = r.as_source_dict()
            # Merge duplicate ASINs across tiers first.
            by_asin: dict[str, tuple[dict, list[str]]] = {}
            for item, source_label in candidates_by_row.get(r.row_idx, []):
                asin = (item.get("asin") or "").upper()
                if not asin:
                    continue
                if asin in by_asin:
                    by_asin[asin] = (by_asin[asin][0],
                                     list(dict.fromkeys(by_asin[asin][1] + [source_label])))
                else:
                    by_asin[asin] = (item, [source_label])

            for asin, (item, sources) in by_asin.items():
                _upsert_candidate(run_id, r.row_idx, item, source_dict, sources)

            done += 1
            if done % 10 == 0 or done == total:
                _update_progress(run_id, done=done)

        _recompute_run_counts(run_id)
        _update_progress(run_id, status="Complete", phase="Done", done=total, total=total)

    except Exception as exc:  # noqa: BLE001
        log.exception("Analytics run %s failed", run_id)
        _update_progress(
            run_id, status="Error",
            phase=f"{type(exc).__name__}: {exc}"[:200],
        )
