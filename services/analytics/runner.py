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
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Iterable

from services import database
from services.analytics.matcher import calculate_confidence
from services.analytics.parser import SourceRow
from services.analytics.brand_extractor import extract_brands
from services.spapi import get_catalog_api, sp_api_configured
from services.spapi.catalog import CatalogAPI

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Tunables — match AmazonAsinResearch1 defaults
# --------------------------------------------------------------------------- #

UPC_BATCH_SIZE = 20            # SP-API hard cap on identifiers per call
DEFAULT_TITLE_MAX_PAGES = 5    # what the wizard exposes (Analytics panel)
PAGE_SLEEP = 0.6               # seconds between paged keyword calls (safety)
AI_CLEAN_WORKERS = 3           # parallel GPT-4o-mini calls for title cleaning (Tier 1: 500 RPM)

# Verdict thresholds (mirrors AmazonAsinResearch1 config defaults).
MIN_CONFIDENCE = 30.0
AUTO_APPROVE = 90.0
REVIEW_FLOOR = 35.0


# --------------------------------------------------------------------------- #
# Run control — pause / stop flags
# --------------------------------------------------------------------------- #

# Maps run_id → "pause" | "stop"
_RUN_CONTROL: dict[int, str] = {}
_RUN_CONTROL_LOCK = threading.Lock()


def request_pause(run_id: int) -> None:
    with _RUN_CONTROL_LOCK:
        _RUN_CONTROL[run_id] = "pause"


def request_stop(run_id: int) -> None:
    with _RUN_CONTROL_LOCK:
        _RUN_CONTROL[run_id] = "stop"


def clear_control(run_id: int) -> None:
    with _RUN_CONTROL_LOCK:
        _RUN_CONTROL.pop(run_id, None)


def _check_control(run_id: int) -> str | None:
    """Return 'pause', 'stop', or None. Clears the flag if stop."""
    with _RUN_CONTROL_LOCK:
        return _RUN_CONTROL.get(run_id)


# --------------------------------------------------------------------------- #
# AI title cleaning — GPT-4o-mini
# --------------------------------------------------------------------------- #

_AI_CLEAN_SYSTEM = (
    "You are a product title cleaner. Given a vendor product title, return a "
    "clean, concise, search-friendly version suitable for an Amazon keyword "
    "search. Strip internal codes, excess punctuation, and all-caps formatting. "
    "Return only the cleaned title — nothing else."
)


def _clean_one_title(title: str) -> str:
    """Call GPT-4o-mini to clean a single vendor title. Returns original on error."""
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return title
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": _AI_CLEAN_SYSTEM},
                {"role": "user",   "content": title},
            ],
            max_tokens=60,
            temperature=0.0,
        )
        cleaned = (resp.choices[0].message.content or "").strip()
        return cleaned if cleaned else title
    except Exception as exc:
        log.warning("AI title clean failed for %r: %s", title[:60], exc)
        return title


def clean_titles_parallel(
    rows: list[SourceRow],
    run_id: int,
    workers: int = AI_CLEAN_WORKERS,
) -> dict[int, str]:
    """
    Return {row_idx: cleaned_title} for every row that has a title.
    Runs up to `workers` GPT-4o-mini calls in parallel.
    Stops early if a pause/stop is requested.
    """
    result: dict[int, str] = {}
    rows_with_titles = [(r.row_idx, r.title) for r in rows if r.title]
    total = len(rows_with_titles)
    done = 0

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_clean_one_title, title): (idx, title)
            for idx, title in rows_with_titles
        }
        for fut in as_completed(futures):
            idx, orig = futures[fut]
            try:
                result[idx] = fut.result()
            except Exception:
                result[idx] = orig
            done += 1
            if done % 50 == 0 or done == total:
                _update_progress(
                    run_id,
                    phase=f"Cleaning titles with AI ({done}/{total})",
                )
            if _check_control(run_id):
                pool.shutdown(wait=False, cancel_futures=True)
                break

    return result


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


def _extract_sales_rank(raw: dict) -> tuple[int | None, str, list[dict]]:
    """
    Parse salesRanks from a raw SP-API item.

    Returns (best_rank, best_category, all_ranks) where:
      best_rank      — lowest (best) rank number across all rank entries, or None
      best_category  — category name for that best rank
      all_ranks      — list of {"rank": int, "category": str} for every entry
    """
    all_ranks: list[dict] = []
    best_rank: int | None = None
    best_category = ""

    for sr in (raw.get("salesRanks") or []):
        for group_key in ("classificationRanks", "displayGroupRanks"):
            for entry in (sr.get(group_key) or []):
                try:
                    r = int(entry.get("rank") or 0)
                except (TypeError, ValueError):
                    continue
                if r <= 0:
                    continue
                cat = str(entry.get("title") or "")
                all_ranks.append({"rank": r, "category": cat})
                if best_rank is None or r < best_rank:
                    best_rank = r
                    best_category = cat

    return best_rank, best_category, all_ranks


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

    best_rank, best_rank_category, all_ranks = _extract_sales_rank(raw)

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
        "sales_rank": best_rank,
        "sales_rank_category": best_rank_category,
        "sales_ranks": all_ranks,
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
    max_rank: int = 0,
    min_rank: int = 0,
) -> int:
    with database._LOCK, _with_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO analytics_runs
              (name, marketplace, search_methods, pages_per_title,
               ai_clean_titles, total_catalog_items, max_rank, min_rank, status,
               progress_phase, progress_done, progress_total)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'Pending', 'Queued', 0, ?)
            """,
            (
                name, marketplace, json.dumps(search_methods),
                int(pages_per_title), 1 if ai_clean_titles else 0,
                int(total_catalog_items), int(max_rank), int(min_rank),
                int(total_catalog_items),
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
    max_rank: int = 0,
    min_rank: int = 0,
    extracted: dict | None = None,
) -> str:
    """
    Score the candidate, write/merge it into analytics_candidates, and
    return the verdict string. If the ASIN already exists for this row
    from a prior tier, we keep the higher confidence and union the
    `sources` list.

    max_rank: when > 0, ASINs whose sales_rank exceeds this are forced to not_approved.
    min_rank: when > 0, ASINs whose sales_rank is below this are forced to not_approved.
    Unknown/null rank is never penalised by either cap.
    extracted: brand fields from GPT-4o-mini extraction — used for more precise scoring.
    """
    asin = (normalized.get("asin") or "").strip().upper()
    if not asin:
        return "skip"

    scores = calculate_confidence(
        source, normalized,
        upc_search_hit="UPC" in sources,
        extracted=extracted,
    )
    conf = scores["confidence_score"]
    hard_reject = scores.get("size_mismatch") or scores.get("gender_mismatch") or scores.get("color_mismatch")
    if hard_reject:
        verdict = "not_approved"
    elif conf >= AUTO_APPROVE:
        verdict = "verified"
    elif conf >= REVIEW_FLOOR or scores.get("pack_mismatch"):
        verdict = "review"
    elif conf >= MIN_CONFIDENCE:
        verdict = "review"
    else:
        verdict = "not_approved"

    # amz_pack: use the scorer's effective_pack when available (accounts for
    # vendor titles that already embed a count, e.g. "36 count crayons").
    # Fall back to the raw SP-API attribute for non-UPC-matched candidates.
    effective_pack = scores.get("effective_pack")
    if effective_pack is not None:
        amz_pack = effective_pack if effective_pack > 1 else None
    else:
        amz_pack_raw = normalized.get("item_package_quantity") or normalized.get("number_of_items")
        try:
            raw_int = int(float(amz_pack_raw)) if amz_pack_raw else None
            amz_pack = raw_int if raw_int and raw_int > 1 else None
        except (TypeError, ValueError):
            amz_pack = None

    sales_rank = normalized.get("sales_rank")  # int or None

    # Apply rank window: ASINs outside [min_rank, max_rank] → not_approved.
    # Both caps: unknown/null rank is always allowed through (we never penalise
    # products whose BSR Amazon hasn't populated).
    if max_rank > 0 and sales_rank is not None and int(sales_rank) > max_rank:
        verdict = "not_approved"
    if min_rank > 0 and sales_rank is not None and int(sales_rank) < min_rank:
        verdict = "not_approved"

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
                "(run_id, row_idx, asin, sources, confidence, verdict, amz_pack, sales_rank, data_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, row_idx, asin, json.dumps(sources),
                 conf, verdict, amz_pack, sales_rank, data_json),
            )
        else:
            try:
                prev_sources = json.loads(existing["sources"] or "[]")
            except (TypeError, ValueError):
                prev_sources = []
            merged = list(dict.fromkeys([*prev_sources, *sources]))  # dedupe, keep order
            new_conf = max(conf, float(existing["confidence"] or 0))
            # Recompute verdict from the best confidence we've seen so a
            # lower-scoring tier can't downgrade a UPC-confirmed "verified".
            upc_confirmed = scores["upc_match"] and not scores.get("pack_mismatch") and not scores.get("upc_suspect")
            if upc_confirmed:
                verdict = "verified"
            elif hard_reject:
                verdict = "not_approved"
            elif new_conf >= AUTO_APPROVE:
                verdict = "verified"
            elif new_conf >= REVIEW_FLOOR or scores.get("pack_mismatch"):
                verdict = "review"
            elif new_conf >= MIN_CONFIDENCE:
                verdict = "review"
            else:
                verdict = "not_approved"
            if max_rank > 0 and sales_rank is not None and int(sales_rank) > max_rank:
                verdict = "not_approved"
            if min_rank > 0 and sales_rank is not None and int(sales_rank) < min_rank:
                verdict = "not_approved"
            conn.execute(
                "UPDATE analytics_candidates SET sources=?, confidence=?, "
                "  verdict=?, amz_pack=?, sales_rank=?, data_json=? "
                "WHERE run_id=? AND row_idx=? AND asin=?",
                (json.dumps(merged), new_conf, verdict, amz_pack, sales_rank, data_json,
                 run_id, row_idx, asin),
            )

    return verdict


def _save_extracted_brands(run_id: int, extracted: dict[int, dict]) -> None:
    """Persist extracted brand fields into analytics_catalog_rows.extracted_json."""
    if not extracted:
        return
    with database._LOCK, _with_conn() as conn:
        conn.executemany(
            "UPDATE analytics_catalog_rows SET extracted_json=? "
            "WHERE run_id=? AND row_idx=?",
            [(json.dumps(v, ensure_ascii=False), run_id, k) for k, v in extracted.items()],
        )


def _load_extracted_brands(run_id: int) -> dict[int, dict]:
    """Load extracted brand fields from DB for an existing run."""
    with _with_conn() as conn:
        rows = conn.execute(
            "SELECT row_idx, extracted_json FROM analytics_catalog_rows "
            "WHERE run_id=? AND extracted_json IS NOT NULL",
            (run_id,),
        ).fetchall()
    result: dict[int, dict] = {}
    for r in rows:
        try:
            result[r["row_idx"]] = json.loads(r["extracted_json"] or "{}")
        except Exception:
            pass
    return result


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


def _normalize_upc(upc: str) -> str:
    """Zero-pad 11-digit UPCs to 12 digits. UPC-A is always 12 digits;
    vendor exports commonly strip the leading zero."""
    if len(upc) == 11 and upc.isdigit():
        return "0" + upc
    return upc


def _tier1_upc(
    api: CatalogAPI,
    rows: list[SourceRow],
    run_id: int = 0,
) -> dict[int, list[dict]]:
    """
    Batch UPC/EAN search via SP-API. Returns {row_idx: [normalized items]}.

    Two-pass strategy:
      Pass 1 — search as UPC-12 (id_type="UPC").
      Pass 2 — for any UPC that got no hit, prepend 0 to make EAN-13 and
               search again as id_type="EAN". Amazon indexes many CPG products
               as EAN-13 even when the label shows a 12-digit UPC-A barcode.
    """
    upc_to_rows: dict[str, list[int]] = {}
    for r in rows:
        if r.upc:
            norm = _normalize_upc(r.upc)
            upc_to_rows.setdefault(norm, []).append(r.row_idx)

    out: dict[int, list[dict]] = {}
    if not upc_to_rows:
        return out

    def _run_batches(id_list: list[str], id_type: str, phase_offset: int = 0) -> set[str]:
        """Send batches, populate `out`, return set of UPCs that got ≥1 hit."""
        hit_set: set[str] = set()
        batches = list(_chunks(id_list, UPC_BATCH_SIZE))
        for i, batch in enumerate(batches):
            if run_id and _check_control(run_id):
                break
            if run_id:
                _update_progress(run_id, done=phase_offset + i, total=phase_offset + len(batches))
            try:
                data = api.search_by_identifiers(batch, id_type=id_type)
            except Exception as exc:
                log.warning("%s batch failed (%s): %s", id_type, len(batch), exc)
                continue
            for raw in (data.get("items") or []):
                normalized = normalize_amazon_item(raw)
                # Collect all identifiers this item carries (both UPC and EAN forms).
                item_ids: set[str] = set()
                for v in (normalized.get("upc"), normalized.get("ean")):
                    if v:
                        item_ids.add(v)
                        # Also try stripping a leading 0 to match stored 12-digit UPC.
                        if v.startswith("0") and len(v) == 13:
                            item_ids.add(v[1:])
                for uid in item_ids & set(upc_to_rows.keys()):
                    hit_set.add(uid)
                    for ri in upc_to_rows[uid]:
                        out.setdefault(ri, []).append(normalized)
        return hit_set

    # Pass 1: search by UPC-12
    upcs = list(upc_to_rows.keys())
    hit_upcs = _run_batches(upcs, "UPC", phase_offset=0)

    # Pass 2: search ALL 12-digit UPCs again as EAN-13 (prepend 0).
    # Amazon indexes many CPG products under EAN-13 even when the physical
    # label shows a 12-digit UPC-A. Running a full EAN pass finds additional
    # listings that the UPC pass missed, and can also return extra ASINs for
    # UPCs that already had UPC hits (multipack / variant listings).
    ean_candidates = [u for u in upcs if u.isdigit() and len(u) == 12]
    if ean_candidates:
        log.info("UPC pass 2 (EAN-13): searching all %d UPCs as EAN", len(ean_candidates))
        eans = ["0" + u for u in ean_candidates]
        upc1_batches = (len(upcs) + UPC_BATCH_SIZE - 1) // UPC_BATCH_SIZE
        _run_batches(eans, "EAN", phase_offset=upc1_batches)

    return out


def _tier2_itemid(
    api: CatalogAPI,
    rows: list[SourceRow],
    run_id: int = 0,
) -> dict[int, list[dict]]:
    """One keyword search per unique Item ID string."""
    term_to_rows: dict[str, list[int]] = {}
    for r in rows:
        if r.itemid:
            term_to_rows.setdefault(r.itemid, []).append(r.row_idx)

    out: dict[int, list[dict]] = {}
    terms = list(term_to_rows.items())
    total_terms = len(terms)
    for i, (term, row_idxs) in enumerate(terms):
        if run_id and _check_control(run_id):
            break
        if run_id and (i % 5 == 0 or i == total_terms - 1):
            _update_progress(run_id, done=i, total=total_terms)
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
    run_id: int = 0,
    extracted_brands: dict[int, dict] | None = None,
) -> dict[int, list[dict]]:
    """
    One paginated keyword search per unique title.
    When extracted_brands is provided, builds queries from extracted
    brand + product_type (+ model) instead of the raw vendor title —
    this gives Amazon's search engine cleaner, more targeted input.
    """
    extracted_brands = extracted_brands or {}
    term_to_rows: dict[str, list[int]] = {}
    for r in rows:
        ext = extracted_brands.get(r.row_idx) or {}
        brand = ext.get("brand") or r.brand or ""
        product_type = ext.get("product_type") or ""
        model = ext.get("model") or ""

        if brand and product_type:
            # Extracted fields available — build a clean, targeted query
            parts = [p for p in [brand, product_type, model] if p]
            term = " ".join(parts)
        elif r.search_term:
            # Fallback: existing approach (brand prefix + raw title/search_term)
            term = f"{brand} {r.search_term}".strip() if brand else r.search_term
        else:
            continue

        term_to_rows.setdefault(term, []).append(r.row_idx)

    out: dict[int, list[dict]] = {}
    terms = list(term_to_rows.items())
    total_terms = len(terms)
    for i, (term, row_idxs) in enumerate(terms):
        if run_id and _check_control(run_id):
            break
        if run_id and (i % 5 == 0 or i == total_terms - 1):
            _update_progress(run_id, done=i, total=total_terms)
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
# --------------------------------------------------------------------------- #
# Re-score pipeline (background)
# --------------------------------------------------------------------------- #

_RESCORE_BATCH = 200  # candidates scored + written per DB transaction


def start_rescore(
    run_id: int, title_col: str, brand_col: str = "", max_rank: int = 0,
    min_rank: int = 0,
) -> None:
    """Kick off a background re-score thread for an existing run.

    max_rank / min_rank: when >= 0, persist on the run row so future
    rescores default to these values.  Pass 0 to clear the cap.
    """
    clear_control(run_id)
    # Persist updated rank caps on the run row so they survive a server restart.
    with database._LOCK, _with_conn() as conn:
        conn.execute(
            "UPDATE analytics_runs SET max_rank=?, min_rank=? WHERE id=?",
            (int(max_rank), int(min_rank), run_id),
        )
    # Write "Rescoring" to the DB *before* launching the thread so that the
    # very first fetchAnalyticsRunDetail call (which happens right after the
    # endpoint returns) already sees an active status and starts polling.
    _update_progress(run_id, status="Rescoring",
                     phase="Re-scoring candidates…", done=0, total=0)
    thread = threading.Thread(
        target=_rescore_pipeline,
        args=(run_id, title_col, brand_col, max_rank, min_rank),
        daemon=True,
    )
    thread.start()


def _rescore_pipeline(
    run_id: int, title_col: str, brand_col: str = "", max_rank: int = 0,
    min_rank: int = 0,
) -> None:
    """
    Re-score every candidate in `run_id` using `title_col` from each row's
    raw dict as the source title for the confidence scorer.

    Also updates analytics_catalog_rows.data_json so the Vendor Title column
    in the UI reflects the newly chosen title after rescoring.

    max_rank / min_rank: enforce rank window — candidates outside the range
    are forced to not_approved regardless of confidence.
    """
    try:
        with database._connect() as conn:
            conn.row_factory = sqlite3.Row
            cat_rows = conn.execute(
                "SELECT row_idx, data_json FROM analytics_catalog_rows "
                "WHERE run_id=? ORDER BY row_idx",
                (run_id,),
            ).fetchall()
            cand_rows = conn.execute(
                "SELECT row_idx, asin, sources, data_json "
                "FROM analytics_candidates WHERE run_id=? ORDER BY row_idx",
                (run_id,),
            ).fetchall()

        # Load previously extracted brand fields (stored during the original run).
        extracted_brands_rescore = _load_extracted_brands(run_id)

        # Build row_idx → catalog data map, also resolve new title per row.
        catalog_map: dict[int, dict] = {}
        title_by_row: dict[int, str] = {}
        for r in cat_rows:
            try:
                d = json.loads(r["data_json"] or "{}")
            except (ValueError, TypeError):
                d = {}
            ri = int(r["row_idx"])
            catalog_map[ri] = d
            raw = d.get("raw", {})
            if title_col and title_col in raw:
                title_by_row[ri] = str(raw.get(title_col) or "").strip()
            else:
                title_by_row[ri] = d.get("title", "")

        brand_by_row: dict[int, str] = {}
        for r in cat_rows:
            try:
                d2 = json.loads(r["data_json"] or "{}")
            except (ValueError, TypeError):
                d2 = {}
            ri2 = int(r["row_idx"])
            raw2 = d2.get("raw", {})
            if brand_col and brand_col in raw2:
                brand_by_row[ri2] = str(raw2.get(brand_col) or "").strip()
            else:
                brand_by_row[ri2] = d2.get("brand", "")

        total = len(cand_rows)
        _update_progress(run_id, done=0, total=total)

        cand_batch: list[tuple] = []
        cat_batch:  list[tuple] = []   # (new_data_json, run_id, row_idx)
        flushed_cat_rows: set[int] = set()

        def _flush(final_done: int) -> None:
            if cand_batch:
                with database._LOCK, _with_conn() as conn:
                    conn.executemany(
                        "UPDATE analytics_candidates "
                        "SET confidence=?, verdict=?, data_json=?, sales_rank=? "
                        "WHERE run_id=? AND row_idx=? AND asin=?",
                        cand_batch,
                    )
                cand_batch.clear()
            if cat_batch:
                with database._LOCK, _with_conn() as conn:
                    conn.executemany(
                        "UPDATE analytics_catalog_rows SET data_json=? "
                        "WHERE run_id=? AND row_idx=?",
                        cat_batch,
                    )
                cat_batch.clear()
            _update_progress(run_id, done=final_done, total=total)

        for i, c in enumerate(cand_rows):
            if _check_control(run_id):
                _flush(i)
                _update_progress(run_id, status="Stopped",
                                 phase="Stopped by user", done=i, total=total)
                return

            row_idx = int(c["row_idx"])
            cat = catalog_map.get(row_idx, {})
            new_title = title_by_row.get(row_idx, cat.get("title", ""))

            try:
                cand_data = json.loads(c["data_json"] or "{}")
            except (ValueError, TypeError):
                cand_data = {}

            amazon_data = cand_data.get("amazon", {})
            if not amazon_data:
                continue

            row_brand = brand_by_row.get(row_idx, cat.get("brand", ""))
            source = {
                "upc":          cat.get("upc", ""),
                "mpn":          cat.get("itemid", ""),
                "itemid":       cat.get("itemid", ""),
                "title":        new_title,
                "brand":        row_brand,
                "manufacturer": row_brand,
            }

            try:
                sources_list = json.loads(c["sources"] or "[]")
            except (ValueError, TypeError):
                sources_list = []

            scores = calculate_confidence(
                source, amazon_data,
                upc_search_hit="UPC" in sources_list,
                extracted=extracted_brands_rescore.get(row_idx),
            )
            conf = scores["confidence_score"]
            hard_reject = scores.get("size_mismatch") or scores.get("gender_mismatch") or scores.get("color_mismatch")
            upc_confirmed = scores["upc_match"] and not scores.get("pack_mismatch") and not scores.get("upc_suspect")
            if upc_confirmed:
                verdict = "verified"
            elif hard_reject:
                verdict = "not_approved"
            elif conf >= 90:
                verdict = "verified"
            elif conf >= 35 or scores.get("pack_mismatch"):
                verdict = "review"
            else:
                verdict = "not_approved"
            # Re-extract sales_rank from the stored _raw SP-API payload when
            # the normalized dict is missing it (runs created before rank
            # extraction was added).  This backfills both the data_json and
            # the DB column so max_rank logic and the BSR display work correctly.
            cand_sales_rank = amazon_data.get("sales_rank")
            if cand_sales_rank is None:
                raw_sp = amazon_data.get("_raw") or {}
                if raw_sp:
                    cand_sales_rank, _, _ = _extract_sales_rank(raw_sp)
                    if cand_sales_rank is not None:
                        amazon_data["sales_rank"] = cand_sales_rank
                        cand_data["amazon"] = amazon_data

            # Apply rank window — overrides even a high-confidence verdict.
            # Both caps: unknown/null rank is always allowed through.
            if max_rank > 0 and cand_sales_rank is not None and int(cand_sales_rank) > max_rank:
                verdict = "not_approved"
            if min_rank > 0 and cand_sales_rank is not None and int(cand_sales_rank) < min_rank:
                verdict = "not_approved"

            scores["verdict"] = verdict
            cand_data["scores"] = scores
            cand_data["verdict"] = verdict

            cand_batch.append((
                float(conf), verdict, json.dumps(cand_data, default=str),
                cand_sales_rank,   # write back to the sales_rank DB column
                run_id, row_idx, str(c["asin"]),
            ))

            # Update the catalog row's title once per unique row_idx.
            if row_idx not in flushed_cat_rows:
                updated_cat = dict(cat)
                updated_cat["title"] = new_title
                cat_batch.append((
                    json.dumps(updated_cat, default=str),
                    run_id, row_idx,
                ))
                flushed_cat_rows.add(row_idx)

            if len(cand_batch) >= _RESCORE_BATCH:
                _flush(i + 1)

        _flush(total)

        # Recompute aggregate counts and restore "Complete" status.
        with database._LOCK, database._connect() as conn:
            conn.row_factory = sqlite3.Row
            verdict_rows = conn.execute(
                "SELECT verdict, COUNT(*) AS n FROM analytics_candidates "
                "WHERE run_id=? GROUP BY verdict",
                (run_id,),
            ).fetchall()
            verified = review = not_approved = total_c = 0
            for r in verdict_rows:
                v = (r["verdict"] or "").lower()
                n = int(r["n"])
                total_c += n
                if v == "verified":    verified += n
                elif v == "review":    review += n
                elif v == "not_approved": not_approved += n
            conn.execute(
                "UPDATE analytics_runs SET "
                "  total_candidates_found=?, verified_count=?, review_count=?, "
                "  not_approved_count=?, status='Complete', "
                "  progress_phase='Re-scoring complete', "
                "  progress_done=?, progress_total=?, "
                "  updated_at=CURRENT_TIMESTAMP "
                "WHERE id=?",
                (total_c, verified, review, not_approved, total, total, run_id),
            )
    except Exception:
        log.exception("Re-score pipeline failed for run %d", run_id)
        _update_progress(run_id, status="Error", phase="Re-score failed")


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
    max_rank: int = 0,
    min_rank: int = 0,
) -> int:
    """
    Create an analytics_runs row, persist source rows, and kick off a
    background thread that runs the 3-tier search + scoring. Returns
    the new run id so the caller can redirect/poll.

    max_rank / min_rank: enforce rank window at verdict time.
    """
    run_id = _create_run(
        name=name, marketplace=marketplace, search_methods=search_methods,
        pages_per_title=pages_per_title, ai_clean_titles=ai_clean_titles,
        total_catalog_items=len(source_rows), max_rank=max_rank, min_rank=min_rank,
    )
    _save_catalog_rows(run_id, source_rows)

    # Run in a daemon thread so uvicorn shuts down cleanly. This is
    # fine for the local-first catalog-verifier; a larger deployment
    # would swap this for a proper queue.
    thread = threading.Thread(
        target=_run_pipeline,
        args=(run_id, marketplace, search_methods, pages_per_title,
              ai_clean_titles, source_rows, False, max_rank, min_rank),
        daemon=True,
    )
    thread.start()
    return run_id


def resume_run(run_id: int) -> bool:
    """
    Resume a Paused run. Reloads the source rows from the DB and restarts
    the pipeline, skipping rows that already have candidates.
    Returns False if the run isn't in a resumable state.
    """
    with database._connect() as conn:
        conn.row_factory = sqlite3.Row
        run = conn.execute(
            "SELECT * FROM analytics_runs WHERE id=?", (run_id,)
        ).fetchone()
        if not run or run["status"] not in ("Paused", "Stopped"):
            return False
        run_d = dict(run)

        cat_rows = conn.execute(
            "SELECT row_idx, data_json FROM analytics_catalog_rows "
            "WHERE run_id=? ORDER BY row_idx",
            (run_id,),
        ).fetchall()

    # Rebuild SourceRow list from persisted data_json.
    from services.analytics.parser import SourceRow
    source_rows: list[SourceRow] = []
    for r in cat_rows:
        try:
            d = json.loads(r["data_json"] or "{}")
        except (ValueError, TypeError):
            d = {}
        sr = SourceRow(
            row_idx=r["row_idx"],
            upc=d.get("upc") or "",
            itemid=d.get("itemid") or "",
            title=d.get("title") or "",
            search_title=d.get("search_title") or "",
            brand=d.get("brand") or "",
        )
        source_rows.append(sr)

    try:
        search_methods = json.loads(run_d.get("search_methods") or "[]")
    except (ValueError, TypeError):
        search_methods = []

    max_rank = int(run_d.get("max_rank") or 0)
    min_rank = int(run_d.get("min_rank") or 0)

    clear_control(run_id)
    _update_progress(run_id, status="Searching", phase="Resuming…")

    thread = threading.Thread(
        target=_run_pipeline,
        args=(run_id,
              run_d.get("marketplace") or "US",
              search_methods,
              int(run_d.get("pages_per_title") or DEFAULT_TITLE_MAX_PAGES),
              bool(run_d.get("ai_clean_titles")),
              source_rows,
              True,       # is_resume=True
              max_rank, min_rank),
        daemon=True,
    )
    thread.start()
    return True


def _run_pipeline(
    run_id: int,
    marketplace: str,
    search_methods: list[str],
    pages_per_title: int,
    ai_clean_titles: bool,
    source_rows: list[SourceRow],
    is_resume: bool = False,
    max_rank: int = 0,
    min_rank: int = 0,
) -> None:
    """Do the work. All errors are caught and recorded on the run row."""
    try:
        if not sp_api_configured():
            _update_progress(
                run_id, status="Error",
                phase="SP-API credentials not configured. Add them to .env and restart.",
            )
            return

        # On resume, skip rows that already have at least one candidate.
        if is_resume:
            with database._connect() as conn:
                done_idxs = {
                    r["row_idx"] for r in conn.execute(
                        "SELECT DISTINCT row_idx FROM analytics_candidates WHERE run_id=?",
                        (run_id,),
                    ).fetchall()
                }
            source_rows = [r for r in source_rows if r.row_idx not in done_idxs]
            log.info("Resume run %s: %d rows remaining", run_id, len(source_rows))

        api = get_catalog_api(marketplace)
        total = len(source_rows)
        done = 0
        _update_progress(run_id, status="Searching", phase="Starting", done=0, total=total)

        candidates_by_row: dict[int, list[tuple[dict, str]]] = {}

        # ------------------------------------------------------------------ #
        # Helper: check for pause/stop after each major step
        # ------------------------------------------------------------------ #
        def _handle_control() -> bool:
            """Return True if pipeline should stop (pause or stop)."""
            ctrl = _check_control(run_id)
            if ctrl == "stop":
                _update_progress(run_id, status="Stopped", phase="Stopped by user")
                _recompute_run_counts(run_id)
                return True
            if ctrl == "pause":
                _update_progress(run_id, status="Paused", phase="Paused by user")
                _recompute_run_counts(run_id)
                return True
            return False

        # --- Brand extraction (GPT-4o-mini, always when OpenAI key present) ---
        # Extracts brand / product_type / model / size / pack_info from vendor
        # titles. Used to build better Tier 3 search queries and improve scorer
        # brand matching.  Stored in DB so rescore can reuse without re-calling.
        extracted_brands: dict[int, dict] = {}
        if os.getenv("OPENAI_API_KEY"):
            _update_progress(run_id, phase="Extracting product fields with AI…")

            def _extraction_progress(done: int, total_ext: int) -> None:
                _update_progress(
                    run_id,
                    phase=f"Extracting product fields ({done}/{total_ext})",
                    done=done,
                    total=total_ext,
                )

            extracted_brands = extract_brands(source_rows, run_id, progress_cb=_extraction_progress)
            _save_extracted_brands(run_id, extracted_brands)
            if _handle_control():
                return

        # --- AI title cleaning (before Tier 3, if enabled) ------------------
        cleaned_titles: dict[int, str] = {}
        if ai_clean_titles and "Title" in search_methods and not extracted_brands:
            # Only run simple title cleaning when full brand extraction didn't run
            _update_progress(run_id, phase="Cleaning titles with AI (0/{})".format(total))
            cleaned_titles = clean_titles_parallel(source_rows, run_id)
            if _handle_control():
                return

        # Build a version of source_rows with cleaned titles for Tier 3.
        def _rows_for_title_search() -> list[SourceRow]:
            if not cleaned_titles:
                return source_rows
            out = []
            for r in source_rows:
                if r.row_idx in cleaned_titles:
                    from dataclasses import replace
                    out.append(replace(r, title=cleaned_titles[r.row_idx]))
                else:
                    out.append(r)
            return out

        # --- Tier 1: UPC -----------------------------------------------------
        if "UPC" in search_methods:
            _update_progress(run_id, phase="Tier 1 / UPC batch search")
            tier1 = _tier1_upc(api, source_rows, run_id=run_id)
            for ri, items in tier1.items():
                for it in items:
                    candidates_by_row.setdefault(ri, []).append((it, "UPC"))
            if _handle_control():
                return

        # --- Tier 2: Item ID -------------------------------------------------
        if "ItemID" in search_methods:
            _update_progress(run_id, phase="Tier 2 / Item ID search")
            tier2 = _tier2_itemid(api, source_rows, run_id=run_id)
            for ri, items in tier2.items():
                for it in items:
                    candidates_by_row.setdefault(ri, []).append((it, "ItemID"))
            if _handle_control():
                return

        # --- Tier 3: Title ---------------------------------------------------
        if "Title" in search_methods:
            _update_progress(run_id, phase="Tier 3 / Title search")
            tier3 = _tier3_title(
                api,
                _rows_for_title_search(),
                pages_per_title,
                run_id=run_id,
                extracted_brands=extracted_brands,
            )
            for ri, items in tier3.items():
                for it in items:
                    candidates_by_row.setdefault(ri, []).append((it, "Title"))
            if _handle_control():
                return

        # --- Vetting: score every (row × ASIN) pair --------------------------
        _update_progress(run_id, phase="Vetting candidates", done=0, total=total)
        for r in source_rows:
            source_dict = r.as_source_dict()
            ext = extracted_brands.get(r.row_idx)
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

            for asin, (item, srcs) in by_asin.items():
                _upsert_candidate(
                    run_id, r.row_idx, item, source_dict, srcs,
                    max_rank, min_rank, extracted=ext,
                )

            done += 1
            if done % 10 == 0 or done == total:
                _update_progress(run_id, done=done)

            if done % 50 == 0 and _handle_control():
                return

        _recompute_run_counts(run_id)
        clear_control(run_id)
        _update_progress(run_id, status="Complete", phase="Done", done=total, total=total)

    except Exception as exc:  # noqa: BLE001
        log.exception("Analytics run %s failed", run_id)
        _update_progress(
            run_id, status="Error",
            phase=f"{type(exc).__name__}: {exc}"[:200],
        )
