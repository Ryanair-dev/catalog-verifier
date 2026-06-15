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
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Iterable

from services import database
from services.analytics.matcher import calculate_confidence
from services.analytics.parser import SourceRow
from services.analytics.product_categorizer import categorize, category_distance, UNKNOWN, MEDICAL
from services.analytics.brand_extractor import extract_brands
from services.spapi import get_catalog_api, sp_api_configured
from services.spapi.catalog import CatalogAPI

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Tunables — match AmazonAsinResearch1 defaults
# --------------------------------------------------------------------------- #

UPC_BATCH_SIZE = 20            # SP-API hard cap on identifiers per call
DEFAULT_TITLE_MAX_PAGES = 5    # what the wizard exposes (Analytics panel)
TITLE_PAGE_CAP = 10
PAGE_SLEEP = 0.6               # seconds between paged keyword calls (safety)
AI_CLEAN_WORKERS = 3           # parallel GPT-4o-mini calls for title cleaning (Tier 1: 500 RPM)

# Verdict thresholds (mirrors AmazonAsinResearch1 config defaults).
MIN_CONFIDENCE = 30.0
AUTO_APPROVE = 90.0
REVIEW_FLOOR = 35.0


# --------------------------------------------------------------------------- #
# Media-format hard-reject
# --------------------------------------------------------------------------- #

# Matches physical/digital media format indicators that appear as standalone words
# in an Amazon product title.  A medical or CPG vendor would never supply these.
# Using word-boundary regex so "DVD" in a title like "STRETCHING EXERCISES FOR
# SENIORS DVD" fires, but "DVDS001" (a model number) does not.
_MEDIA_FORMAT_RE = re.compile(
    r"\b(dvd|blu[\s\-]?ray|audiobook|audio\s+book|e[\s\-]?book|vhs|cd[\s\-]?rom)\b",
    re.IGNORECASE,
)


def _is_media_format(amazon_title: str) -> bool:
    """Return True when the Amazon title explicitly identifies the item as a
    physical/digital media product (DVD, Blu-ray, audiobook, etc.)."""
    return bool(_MEDIA_FORMAT_RE.search(amazon_title or ""))


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

    Returns (main_rank, main_category, all_ranks) where:
      main_rank      — highest rank number across all entries (= broadest/top-level
                       category, matching the main BSR Amazon displays on the product
                       page). Subcategory ranks are always smaller numbers; the largest
                       number is the most general category rank.
      main_category  — category name for that main rank
      all_ranks      — list of {"rank": int, "category": str} for every entry
    """
    all_ranks: list[dict] = []
    main_rank: int | None = None
    main_category = ""

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
                if main_rank is None or r > main_rank:
                    main_rank = r
                    main_category = cat

    return main_rank, main_category, all_ranks


def normalize_amazon_item(raw: dict) -> dict:
    """
    Flatten a single SP-API Catalog Items v2022-04-01 item into the dict
    shape `matcher.calculate_confidence` expects.
    """
    asin = raw.get("asin", "")

    summaries = raw.get("summaries") or []
    summary = summaries[0] if summaries else {}

    identifiers = raw.get("identifiers") or []
    upcs, eans, gtins = [], [], []
    for ident_group in identifiers:
        for ident in ident_group.get("identifiers", []):
            t = (ident.get("identifierType") or "").upper()
            v = (ident.get("identifier") or "").strip()
            if t == "UPC" and v:
                upcs.append(v)
            elif t == "EAN" and v:
                eans.append(v)
            elif t in ("GTIN", "GTIN14", "GTIN-14") and v:
                gtins.append(v)

    # Derive GTIN-14 from UPC-A or EAN-13 when SP-API doesn't return one directly.
    # GTIN-14 standard: UPC-A (12 digits) → "00" + UPC; EAN-13 (13 digits) → "0" + EAN.
    gtin_val = gtins[0] if gtins else ""
    if not gtin_val:
        if upcs and len(upcs[0]) == 12 and upcs[0].isdigit():
            gtin_val = "00" + upcs[0]
        elif eans and len(eans[0]) == 13 and eans[0].isdigit():
            gtin_val = "0" + eans[0]

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
        "gtin": gtin_val,
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
    mode: str = "cpg",
    brand_col: str = "",
    brand_mode: str = "col",
    passthrough_cols: str = "",
) -> int:
    with database._LOCK, _with_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO analytics_runs
              (name, marketplace, search_methods, pages_per_title,
               ai_clean_titles, total_catalog_items, max_rank, min_rank,
               vetting_mode, brand_col, brand_mode, passthrough_cols,
               status, progress_phase, progress_done, progress_total)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'Pending', 'Queued', 0, ?)
            """,
            (
                name, marketplace, json.dumps(search_methods),
                int(pages_per_title), 1 if ai_clean_titles else 0,
                int(total_catalog_items), int(max_rank), int(min_rank),
                mode or "cpg", brand_col or "", brand_mode or "col",
                passthrough_cols or "",
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
    blacklist_set: frozenset | None = None,
    verified_set: frozenset | None = None,
    mode: str = "cpg",
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

    upc = str(source.get("upc") or "").strip()

    # Save all fetched ASIN data to the global cache (strips _raw to save space).
    database.save_asin_to_cache(asin, normalized)

    scores = calculate_confidence(
        source, normalized,
        upc_search_hit="UPC" in sources,
        extracted=extracted,
        mpn_search_hit="ItemID" in sources,
        mode=mode,
    )
    conf = scores["confidence_score"]

    # Category check: reject Amazon items whose BSR category is clearly in the
    # wrong domain.  We use ONLY the Amazon sales_rank_category (the BSR top-level
    # category) — it is authoritative and unambiguous.  Title-inferred categories
    # are too noisy (e.g. "washcloths" → Food, "hot pack" → Garden).
    # If Amazon provides no BSR category the check is skipped (no false-positives).
    # Medical mode only — CPG runs span too many categories to restrict.
    # Category distance only vetoes AMBIGUOUS matches.  A confirmed UPC or brand
    # match means it's the right product even when Amazon shelves it outside
    # Health (Futuro pantyhose → Clothing, Command hooks → Home Improvement,
    # Scotch tape → Office).  Otherwise correct 100%-UPC matches were being
    # hard-rejected as "Category mismatch".
    _amz_bsr_cat = categorize("", normalized.get("sales_rank_category") or "")
    category_mismatch = False
    if (
        mode == "medical" and _amz_bsr_cat != UNKNOWN
        and not scores.get("upc_match") and not scores.get("brand_confirmed")
    ):
        _dist = category_distance(MEDICAL, _amz_bsr_cat)
        category_mismatch = _dist >= 5.0

    if category_mismatch:
        scores["category_mismatch"] = True

    # Media-format hard-reject (both CPG and medical): if the Amazon title
    # explicitly identifies the listing as a DVD, Blu-ray, audiobook, etc.
    # a medical/CPG vendor would never supply it.  This catches exercise DVDs
    # that Amazon shelves under "Health & Personal Care" or "Sports & Outdoors"
    # — BSR categories that otherwise pass the category distance check.
    media_mismatch = _is_media_format(normalized.get("title") or "")
    if media_mismatch:
        scores["media_format_mismatch"] = True

    hard_reject = (
        scores.get("size_mismatch") or scores.get("gender_mismatch")
        or scores.get("color_mismatch") or category_mismatch or media_mismatch
        or scores.get("count_mismatch") or scores.get("scent_mismatch")
        or scores.get("shade_mismatch") or scores.get("apparel_size_mismatch")
    )
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

    # Apply rank window: ASINs outside [min_rank, max_rank] are excluded
    # entirely from storage so they never appear in any tab.
    # max_rank: null/unknown rank passes through (new listings may not have BSR yet).
    # min_rank: null/unknown rank is ALSO excluded — setting a floor means the user
    #           only wants confirmed-ranked products (e.g. min_rank=1 = no unranked).
    _rank_val = int(sales_rank) if sales_rank is not None else None
    _bsr_excluded = (
        (max_rank > 0 and _rank_val is not None and _rank_val > max_rank) or
        (min_rank > 0 and (_rank_val is None or _rank_val < min_rank))
    )
    if _bsr_excluded:
        # Clean up any pre-existing row (e.g. from a prior tier that found the
        # ASIN before BSR was populated) so it stays invisible.
        with database._LOCK, _with_conn() as conn:
            conn.execute(
                "DELETE FROM analytics_candidates "
                "WHERE run_id=? AND row_idx=? AND asin=?",
                (run_id, row_idx, asin),
            )
        return "bsr_filtered"

    # Pair manager overrides — applied after all scoring/rank logic.
    # Blacklisted pairs are always forced not_approved (never match again until unlinked).
    # Previously verified pairs are auto-approved when scoring is still reasonable.
    if upc and blacklist_set is not None and (upc, asin) in blacklist_set:
        verdict = "not_approved"
    elif upc and verified_set is not None and (upc, asin) in verified_set \
            and not hard_reject and conf >= REVIEW_FLOOR:
        verdict = "verified"

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
            # lower-scoring tier can't downgrade a high-confidence "verified".
            # hard_reject is already computed above (includes category_mismatch).
            if hard_reject:
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
            # Pair manager overrides on update path too.
            if upc and blacklist_set is not None and (upc, asin) in blacklist_set:
                verdict = "not_approved"
            elif upc and verified_set is not None and (upc, asin) in verified_set \
                    and not hard_reject and new_conf >= REVIEW_FLOOR:
                verdict = "verified"
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


def _ean13_check_digit(d12: str) -> str:
    """
    Compute the EAN-13 check digit for the first 12 digits.
    Odd positions (0-indexed even) ×1, even positions (0-indexed odd) ×3.
    """
    total = sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(d12))
    return str((10 - (total % 10)) % 10)


def _is_valid_upca(v: str) -> bool:
    """True when the 12-digit string is a self-consistent UPC-A (check digit OK).
    A genuine UPC-A passes; an EAN-13 with its trailing check digit dropped
    (e.g. "030521026849") fails — which is how we tell the two apart."""
    if len(v) != 12 or not v.isdigit():
        return False
    s = sum(int(c) * (3 if i % 2 == 0 else 1) for i, c in enumerate(v[:11]))
    return str((10 - (s % 10)) % 10) == v[11]


def _all_id_forms(v: str) -> list[str]:
    """
    Return every plausible barcode form of a single identifier string so the
    lookup can match regardless of whether SP-API returns UPC-12, EAN-13, or
    GTIN-14.

    Rules (all assuming the input is numeric):
      12-digit  →  also produce EAN-13 ("0" + v) and GTIN-14 ("00" + v);
                   ALSO produce EAN-13 by appending the computed check digit —
                   covers vendor catalogs that store an EAN-13 with its trailing
                   check digit dropped (e.g. "030521026849" is really EAN
                   "0305210268494" with the final "4" cut off).
      13-digit  →  also produce GTIN-14 ("0" + v); if starts with "0", also UPC-12 (v[1:]);
                   ALSO produce the 12-digit truncation (v[:12]) so it matches a
                   vendor value that dropped the check digit.
      14-digit  →  also produce EAN-13 (v[1:]) and, if starts with "00", UPC-12 (v[2:])
    """
    if not v or not v.isdigit():
        return [v] if v else []
    forms: list[str] = [v]
    n = len(v)
    if n == 12:
        forms.append("0" + v)                      # → EAN-13 (UPC-A prefixed with 0)
        forms.append("00" + v)                     # → GTIN-14
        if not _is_valid_upca(v):
            # Not a self-consistent UPC-A → almost certainly an EAN-13 whose
            # trailing check digit was dropped.  Recover the full EAN-13.
            forms.append(v + _ean13_check_digit(v))
    elif n == 13:
        forms.append("0" + v)   # → GTIN-14
        if v.startswith("0"):
            forms.append(v[1:])  # → UPC-12 (EAN-13 derived from UPC-A)
        forms.append(v[:12])     # → 12-digit truncation (vendor dropped the check digit)
    elif n == 14:
        forms.append(v[1:])      # → EAN-13
        if v.startswith("00"):
            forms.append(v[2:])  # → UPC-12
    # Dedupe while preserving order.
    return list(dict.fromkeys(forms))


def _tier1_upc(
    api: CatalogAPI,
    rows: list[SourceRow],
    run_id: int = 0,
) -> dict[int, list[dict]]:
    """
    Batch UPC/EAN search via SP-API. Returns {row_idx: [normalized items]}.

    Four-pass strategy:
      Pass 1 — search as UPC (id_type="UPC") for all 12-digit identifiers.
      Pass 2 — search the same 12-digit identifiers as EAN-13 (prepend "0").
               Amazon indexes many CPG products as EAN-13 even when the physical
               label shows a 12-digit UPC-A barcode.
      Pass 3 — search any 13-digit EANs from the vendor catalog directly as
               id_type="EAN".  Vendor exports sometimes supply the EAN-13 (not
               the UPC-A) in the UPC column — Pass 1 would search them as
               id_type="UPC" (wrong type) so they need their own EAN pass.
      Pass 4 — keyword search for UPCs that got ZERO results from passes 1-3.
               Two causes of misses: (a) old ASINs (pre-2010, B000/B001/B002
               prefix) whose UPC fields were never registered in SP-API's catalog,
               and (b) SP-API returning only the "featured" ASIN when multiple
               ASINs share the same UPC (size/pack variants).  A keyword search
               on the UPC string hits Amazon's full-text index which finds both.

    The lookup table (`id_to_rows`) is built with ALL barcode forms of each
    catalog identifier (12-digit, 13-digit, 14-digit) so that a match succeeds
    regardless of which form SP-API happens to return in its identifiers array.
    """
    # Build id_to_rows with ALL barcode forms → row index mapping.
    id_to_rows: dict[str, list[int]] = {}
    for r in rows:
        if not r.upc:
            continue
        norm = _normalize_upc(r.upc.strip())
        for form in _all_id_forms(norm):
            id_to_rows.setdefault(form, []).append(r.row_idx)

    out: dict[int, list[dict]] = {}
    if not id_to_rows:
        return out

    def _run_batches(id_list: list[str], id_type: str, phase_offset: int = 0) -> None:
        """Send batches and populate `out`."""
        batches = list(_chunks(id_list, UPC_BATCH_SIZE))
        for i, batch in enumerate(batches):
            if run_id and _check_control(run_id):
                break
            if run_id:
                _update_progress(run_id, done=phase_offset + i, total=phase_offset + len(batches))
            try:
                data = api.search_by_identifiers(batch, id_type=id_type)
            except PermissionError:
                raise  # 403 — SP-API auth/roles problem; abort with a clear error
            except Exception as exc:
                log.warning("%s batch failed (%s): %s", id_type, len(batch), exc)
                continue
            for raw_item in (data.get("items") or []):
                normalized = normalize_amazon_item(raw_item)
                # Collect all barcode forms this item carries across UPC, EAN, GTIN.
                item_ids: set[str] = set()
                for v in filter(None, [
                    normalized.get("upc"),
                    normalized.get("ean"),
                    normalized.get("gtin"),
                ]):
                    for form in _all_id_forms(v):
                        item_ids.add(form)
                for uid in item_ids & id_to_rows.keys():
                    for ri in id_to_rows[uid]:
                        out.setdefault(ri, []).append(normalized)
        # Fix off-by-one: loop ends at done=N-1; mark all batches complete.
        if run_id and batches:
            _update_progress(
                run_id,
                done=phase_offset + len(batches),
                total=phase_offset + len(batches),
            )

    # Deduplicate while preserving order (catalog may have same UPC on multiple rows).
    all_ids = list(dict.fromkeys(id_to_rows.keys()))
    upcs_12 = [u for u in all_ids if u.isdigit() and len(u) == 12]
    eans_13 = [u for u in all_ids if u.isdigit() and len(u) == 13]

    # Pass 1: search all 12-digit forms as UPC
    if upcs_12:
        _run_batches(upcs_12, "UPC", phase_offset=0)

    # Pass 2: search 12-digit forms as EAN-13 (prepend "0")
    if upcs_12:
        upc1_batches = (len(upcs_12) + UPC_BATCH_SIZE - 1) // UPC_BATCH_SIZE
        log.info("UPC pass 2 (EAN-13): searching %d UPCs as EAN", len(upcs_12))
        _run_batches(["0" + u for u in upcs_12], "EAN", phase_offset=upc1_batches)

    # Pass 3: search 13-digit EANs directly as EAN
    # These come from vendor catalogs that export EAN-13 in the UPC column.
    # Pass 1 would search them as id_type="UPC" (wrong) so they need their own pass.
    if eans_13:
        upc12_batches = (len(upcs_12) + UPC_BATCH_SIZE - 1) // UPC_BATCH_SIZE
        ean_dup_batches = upc12_batches  # Pass 2 already incremented offset
        log.info("UPC pass 3 (EAN-13 direct): searching %d EANs from catalog", len(eans_13))
        _run_batches(eans_13, "EAN", phase_offset=upc12_batches + ean_dup_batches)

    # Pass 4: keyword fallback for rows that got zero results from passes 1–3.
    # Two known causes of misses:
    #   (a) Old ASINs (B000–B002 era) whose UPC was never populated in SP-API's
    #       structured identifier fields — identifier search finds nothing.
    #   (b) SP-API returns only the "featured" ASIN when multiple ASINs share a
    #       UPC (pack/size variants) — sibling variants are silently dropped.
    # A keyword search on the raw UPC string uses Amazon's full-text index and
    # recovers both cases.  We search the original (pre-pad) form AND the
    # zero-padded 12-digit form so both `50000765089` and `050000765089` are tried.
    missed_rows = [r for r in rows if r.upc and r.row_idx not in out]
    if missed_rows:
        # Build a deduplicated list of (upc_string, row_idx) pairs to search.
        seen_upc: set[str] = set()
        kw_pairs: list[tuple[str, int]] = []
        for r in missed_rows:
            raw_upc = r.upc.strip()
            norm_upc = _normalize_upc(raw_upc)
            for u in dict.fromkeys([raw_upc, norm_upc]):   # original then padded
                if u and u not in seen_upc:
                    seen_upc.add(u)
                    kw_pairs.append((u, r.row_idx))

        log.info("UPC pass 4 (keyword fallback): %d rows got no results from passes 1-3, "
                 "searching %d UPC strings as keywords", len(missed_rows), len(kw_pairs))

        # Map each UPC string back to all rows that share it (same UPC on multiple rows).
        upc_to_rows: dict[str, list[int]] = {}
        for u, ri in kw_pairs:
            upc_to_rows.setdefault(u, []).append(ri)

        _kw_total = len(upc_to_rows)
        _kw_done = 0
        if run_id and _kw_total:
            _update_progress(
                run_id,
                phase=f"Tier 1 / UPC keyword fallback ({_kw_total} items)",
                done=0,
                total=_kw_total,
            )

        for upc_kw, row_idxs in upc_to_rows.items():
            if run_id and _check_control(run_id):
                break
            try:
                data = api.search_by_keywords(keywords=upc_kw, page_size=20)
            except PermissionError:
                raise  # 403 — SP-API auth/roles problem; abort with a clear error
            except Exception as exc:
                log.warning("UPC keyword fallback failed for %r: %s", upc_kw, exc)
                _kw_done += 1
                if run_id and (_kw_done % 10 == 0 or _kw_done == _kw_total):
                    _update_progress(run_id, done=_kw_done, total=_kw_total)
                continue
            for raw_item in (data.get("items") or []):
                normalized = normalize_amazon_item(raw_item)
                # Verify the result actually carries a matching barcode so we
                # don't accidentally match unrelated products that contain the
                # UPC digits coincidentally in their title/description.
                item_ids: set[str] = set()
                for v in filter(None, [
                    normalized.get("upc"),
                    normalized.get("ean"),
                    normalized.get("gtin"),
                ]):
                    for form in _all_id_forms(v):
                        item_ids.add(form)
                # Also accept a partial match: the UPC keyword must appear in
                # at least one of the item's barcode forms.
                upc_forms = set(_all_id_forms(_normalize_upc(upc_kw)))
                if not (item_ids & upc_forms) and not (item_ids & {upc_kw}):
                    # No barcode match — skip to avoid false positives.
                    continue
                for ri in row_idxs:
                    if normalized not in out.get(ri, []):
                        out.setdefault(ri, []).append(normalized)
            _kw_done += 1
            if run_id and (_kw_done % 10 == 0 or _kw_done == _kw_total):
                _update_progress(run_id, done=_kw_done, total=_kw_total)
            time.sleep(PAGE_SLEEP)

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
        if run_id:
            _update_progress(run_id, done=i + 1, total=total_terms)
        try:
            data = api.search_by_keywords(term, page_size=20, max_retries=3)
        except PermissionError:
            raise  # 403 — SP-API auth/roles problem; abort with a clear error
        except Exception as exc:
            log.warning("ItemID search failed for %r: %s", term, exc)
            continue
        items = data.get("items") or []
        normalized = [normalize_amazon_item(it) for it in items]
        for ri in row_idxs:
            out.setdefault(ri, []).extend(normalized)
    return out


# Quantity / pack / size noise that breaks Amazon keyword search.  Strips
# slash-pack codes ("6/6.5oz", "24/1ct", "12/6pk/22.5oz", "pk/144"), standalone
# sizes ("6.5oz", "1000g"), and "pack of N" / "N count" / "N pk" etc.  Shade
# codes ("#46") and meaningful product numbers ("5 Hour", "WD40") are preserved.
_QTY_NOISE_RE = re.compile(
    r'\b\d+\s*/\s*\d*\.?\d*\s*(?:fl\s*)?(?:oz|ounces?|ml|milliliters?|liters?|l|g|grams?|kg|lbs?|gal|ct|count|pk|pack|ea|each)?'
    r'|\b(?:pk|pack|ct|count|cs|case|ea|dz|dozen)\s*/\s*\d+\b'
    r'|\b\d+(?:\.\d+)?\s*(?:fl\s*)?(?:oz|ounces?|ml|milliliters?|liters?|gallons?|gal|grams?|kg|lbs?|pounds?)\b'
    r'|\bpack\s+of\s+\d+\b|\bbox\s+of\s+\d+\b|\bset\s+of\s+\d+\b|\bcount\s+of\s+\d+\b'
    r'|\b\d+\s*[-\s]?(?:ct|count|pk|pack|pcs|pieces?|rolls?|pairs?|tubes?|vials?|sachets?)\b',
    re.IGNORECASE,
)


def _clean_search_query(text: str) -> str:
    """Strip pack/size noise from a vendor title so Amazon's keyword search
    matches on the real product words (e.g. 'VASELINE SPRAY 6/6.5oz ALOE' →
    'VASELINE SPRAY ALOE').  Keeps every meaningful product word."""
    c = _QTY_NOISE_RE.sub(" ", text or "")
    c = re.sub(r"\s*/\s*", " ", c)      # drop orphaned slashes left by pack codes
    return re.sub(r"\s+", " ", c).strip()


def _tier3_title(
    api: CatalogAPI,
    rows: list[SourceRow],
    max_pages: int,
    run_id: int = 0,
    extracted_brands: dict[int, dict] | None = None,
) -> dict[int, list[dict]]:
    """
    One paginated keyword search per unique title.

    Query = brand + cleaned vendor title.  The title is cleaned of pack/size
    noise ("6/6.5oz", "pk/144") which otherwise wrecks Amazon's keyword
    relevance, but every distinguishing product word is kept (so e.g. "aloe"
    survives — searching extracted brand+product_type alone would drop it and
    bury the right listing).  Extracted brand is preferred for the brand token;
    extracted product_type/model are appended as a fallback when the vendor
    has no usable title.
    """
    extracted_brands = extracted_brands or {}
    term_to_rows: dict[str, list[int]] = {}
    for r in rows:
        ext = extracted_brands.get(r.row_idx) or {}
        brand = ext.get("brand") or r.brand or ""
        cleaned = _clean_search_query(r.search_term)

        if cleaned:
            # brand + cleaned title (don't duplicate the brand if the title
            # already leads with it).
            if brand and brand.lower() not in cleaned.lower():
                term = f"{brand} {cleaned}".strip()
            else:
                term = cleaned
        elif brand and ext.get("product_type"):
            # No usable title — fall back to extracted fields.
            term = " ".join(
                p for p in [brand, ext.get("product_type"), ext.get("model")] if p
            )
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
        page_count = max(1, min(TITLE_PAGE_CAP, int(max_pages or DEFAULT_TITLE_MAX_PAGES)))
        for page in range(page_count):
            try:
                data = api.search_by_keywords(term, page_token=page_token, page_size=20)
            except PermissionError:
                raise  # 403 — SP-API auth/roles problem; abort with a clear error
            except Exception as exc:
                log.warning("Title search failed for %r: %s", term, exc)
                break
            items = data.get("items") or []
            collected.extend(normalize_amazon_item(it) for it in items)
            page_token = (data.get("pagination") or {}).get("nextToken")
            if not page_token:
                break
            if page < page_count - 1:
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
    min_rank: int = 0, brand_mode: str = "col",
) -> None:
    """Kick off a background re-score thread for an existing run.

    max_rank / min_rank: when >= 0, persist on the run row so future
    rescores default to these values.  Pass 0 to clear the cap.
    brand_mode: "text" means brand_col is a literal value applied to every row;
                "col" means brand_col is a column name looked up per row.
    """
    clear_control(run_id)
    # Persist rank caps and brand selection so future rescores pre-populate correctly.
    with database._LOCK, _with_conn() as conn:
        conn.execute(
            "UPDATE analytics_runs SET max_rank=?, min_rank=?, brand_col=?, brand_mode=? WHERE id=?",
            (int(max_rank), int(min_rank), brand_col or "", brand_mode or "col", run_id),
        )
    # Write "Rescoring" to the DB *before* launching the thread so that the
    # very first fetchAnalyticsRunDetail call (which happens right after the
    # endpoint returns) already sees an active status and starts polling.
    _update_progress(run_id, status="Rescoring",
                     phase="Re-scoring candidates…", done=0, total=0)
    thread = threading.Thread(
        target=_rescore_pipeline,
        args=(run_id, title_col, brand_col, max_rank, min_rank, brand_mode),
        daemon=True,
    )
    thread.start()


def _rescore_pipeline(
    run_id: int, title_col: str, brand_col: str = "", max_rank: int = 0,
    min_rank: int = 0, brand_mode: str = "col",
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
            run_row = conn.execute(
                "SELECT vetting_mode FROM analytics_runs WHERE id=?", (run_id,)
            ).fetchone()
            rescore_mode = (run_row["vetting_mode"] if run_row else None) or "cpg"

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

        # Load pair-manager sets once for the whole rescore.
        bl_set = frozenset(database.load_blacklist_set())
        vf_set = frozenset(database.load_verified_set())

        # Build row_idx → catalog data map, also resolve new title per row.
        # Single pass — parse data_json once per row to build all three maps
        catalog_map: dict[int, dict] = {}
        title_by_row: dict[int, str] = {}
        brand_by_row: dict[int, str] = {}
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
            if brand_mode == "text" and brand_col:
                # Literal brand override — same value for every row.
                brand_by_row[ri] = brand_col
            elif brand_col and brand_col in raw:
                brand_by_row[ri] = str(raw.get(brand_col) or "").strip()
            else:
                brand_by_row[ri] = d.get("brand", "")

        total = len(cand_rows)
        _update_progress(run_id, done=0, total=total)

        cand_batch:   list[tuple] = []
        delete_batch: list[tuple] = []  # (run_id, row_idx, asin) — BSR-excluded rows
        cat_batch:    list[tuple] = []  # (new_data_json, run_id, row_idx)
        flushed_cat_rows: set[int] = set()

        def _flush(final_done: int) -> None:
            if delete_batch:
                with database._LOCK, _with_conn() as conn:
                    conn.executemany(
                        "DELETE FROM analytics_candidates "
                        "WHERE run_id=? AND row_idx=? AND asin=?",
                        delete_batch,
                    )
                delete_batch.clear()
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

            ext = extracted_brands_rescore.get(row_idx) or {}
            # When the user explicitly set a text brand override, prevent the
            # AI-extracted brand from taking priority over it.
            if brand_mode == "text" and brand_col and ext.get("brand"):
                ext = {k: v for k, v in ext.items() if k != "brand"}
            scores = calculate_confidence(
                source, amazon_data,
                upc_search_hit="UPC" in sources_list,
                extracted=ext or None,
                mpn_search_hit="ItemID" in sources_list,
                mode=rescore_mode,
            )
            conf = scores["confidence_score"]

            # Category check (same logic as _upsert_candidate): only veto
            # ambiguous matches — a confirmed UPC/brand match is the right
            # product even if Amazon shelves it outside Health.
            _amz_bsr_cat_r = categorize("", amazon_data.get("sales_rank_category") or "")
            _cat_mismatch_r = False
            if (
                rescore_mode == "medical" and _amz_bsr_cat_r != UNKNOWN
                and not scores.get("upc_match") and not scores.get("brand_confirmed")
            ):
                _dist_r = category_distance(MEDICAL, _amz_bsr_cat_r)
                _cat_mismatch_r = _dist_r >= 5.0

            if _cat_mismatch_r:
                scores["category_mismatch"] = True

            # Media-format hard-reject (same logic as _upsert_candidate).
            _media_mismatch_r = _is_media_format(amazon_data.get("title") or "")
            if _media_mismatch_r:
                scores["media_format_mismatch"] = True

            hard_reject = (
                scores.get("size_mismatch") or scores.get("gender_mismatch")
                or scores.get("color_mismatch") or _cat_mismatch_r or _media_mismatch_r
                or scores.get("count_mismatch") or scores.get("scent_mismatch")
                or scores.get("shade_mismatch") or scores.get("apparel_size_mismatch")
            )
            if hard_reject:
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

            # Apply rank window: items outside [min_rank, max_rank] are deleted.
            # min_rank also excludes null-ranked items (same logic as _upsert_candidate).
            _rscore_rank = int(cand_sales_rank) if cand_sales_rank is not None else None
            _rscore_bsr_excluded = (
                (max_rank > 0 and _rscore_rank is not None and _rscore_rank > max_rank) or
                (min_rank > 0 and (_rscore_rank is None or _rscore_rank < min_rank))
            )
            if _rscore_bsr_excluded:
                delete_batch.append((run_id, row_idx, str(c["asin"])))
                continue  # skip adding to cand_batch

            # Pair manager overrides — blacklisted always loses, verified auto-approves.
            cand_upc = str(source.get("upc") or "").strip()
            cand_asin = str(c["asin"]).strip().upper()
            if cand_upc and (cand_upc, cand_asin) in bl_set:
                verdict = "not_approved"
            elif cand_upc and (cand_upc, cand_asin) in vf_set \
                    and not hard_reject and conf >= 35:
                verdict = "verified"

            scores["verdict"] = verdict
            cand_data["scores"] = scores
            cand_data["verdict"] = verdict

            cand_batch.append((
                float(conf), verdict, json.dumps(cand_data, default=str),
                cand_sales_rank,   # write back to the sales_rank DB column
                run_id, row_idx, str(c["asin"]),
            ))

            # Update the catalog row's title (and brand when overridden) once per unique row_idx.
            if row_idx not in flushed_cat_rows:
                updated_cat = dict(cat)
                updated_cat["title"] = new_title
                if brand_mode == "text" and brand_col:
                    updated_cat["brand"] = brand_col
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
    mode: str = "cpg",
    brand_col: str = "",
    brand_mode: str = "col",
    passthrough_cols: str = "",
) -> int:
    """
    Create an analytics_runs row, persist source rows, and kick off a
    background thread that runs the 3-tier search + scoring. Returns
    the new run id so the caller can redirect/poll.

    max_rank / min_rank: enforce rank window at verdict time.
    mode: "cpg" or "medical" — controls MPN hit bonus and category distance threshold.
    brand_col / brand_mode: saved for rescore pre-population.
    passthrough_cols: JSON array of vendor column header names to carry into export.
    """
    pages_per_title = max(1, min(TITLE_PAGE_CAP, int(pages_per_title or DEFAULT_TITLE_MAX_PAGES)))
    run_id = _create_run(
        name=name, marketplace=marketplace, search_methods=search_methods,
        pages_per_title=pages_per_title, ai_clean_titles=ai_clean_titles,
        total_catalog_items=len(source_rows), max_rank=max_rank, min_rank=min_rank,
        mode=mode, brand_col=brand_col, brand_mode=brand_mode,
        passthrough_cols=passthrough_cols,
    )
    _save_catalog_rows(run_id, source_rows)

    # Run in a daemon thread so uvicorn shuts down cleanly. This is
    # fine for the local-first catalog-verifier; a larger deployment
    # would swap this for a proper queue.
    thread = threading.Thread(
        target=_run_pipeline,
        args=(run_id, marketplace, search_methods, pages_per_title,
              ai_clean_titles, source_rows, False, max_rank, min_rank, mode),
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
    run_mode = run_d.get("vetting_mode") or "cpg"

    clear_control(run_id)
    _update_progress(run_id, status="Searching", phase="Resuming…")

    thread = threading.Thread(
        target=_run_pipeline,
        args=(run_id,
              run_d.get("marketplace") or "US",
              search_methods,
              max(1, min(TITLE_PAGE_CAP, int(run_d.get("pages_per_title") or DEFAULT_TITLE_MAX_PAGES))),
              bool(run_d.get("ai_clean_titles")),
              source_rows,
              True,       # is_resume=True
              max_rank, min_rank, run_mode),
        daemon=True,
    )
    thread.start()
    return True


def _vet_and_store(
    run_id: int,
    rows_to_vet: list[SourceRow],
    candidates_by_row: dict[int, list[tuple[dict, str]]],
    extracted_brands: dict[int, dict],
    blacklist_set: frozenset,
    verified_set: frozenset,
    max_rank: int,
    min_rank: int,
    mode: str,
    phase_label: str,
) -> None:
    """
    Score and persist every (row × ASIN) pair for ``rows_to_vet`` using the
    candidates accumulated so far in ``candidates_by_row``.

    Called after EACH search tier (not once at the very end) so candidates show
    up in the UI progressively and survive a pause/stop — they're written as
    soon as their tier completes instead of being held in memory for the whole
    run.  Safe to re-run for the same row: ``_upsert_candidate`` merges by
    (run_id, row_idx, asin), unioning the ``sources`` list and keeping the best
    confidence, so a later tier simply enriches the row's existing candidates.
    """
    total = len(rows_to_vet)
    if not total:
        return
    _update_progress(run_id, phase=phase_label, done=0, total=total)
    done = 0
    for r in rows_to_vet:
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
                blacklist_set=blacklist_set, verified_set=verified_set,
                mode=mode,
            )
        done += 1
        if done % 25 == 0 or done == total:
            _update_progress(run_id, done=done)
    _recompute_run_counts(run_id)


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
    mode: str = "cpg",
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

        # --- Brand extraction (GPT-4o-mini, only when ai_clean_titles enabled) ---
        # Extracts brand / product_type / model / size / pack_info from vendor
        # titles. Used to build better Tier 3 search queries and improve scorer
        # brand matching.  Stored in DB so rescore can reuse without re-calling.
        extracted_brands: dict[int, dict] = {}
        if ai_clean_titles and os.getenv("OPENAI_API_KEY"):
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

        # Pair-manager sets — loaded once and reused for vetting after each tier.
        blacklist_set = frozenset(database.load_blacklist_set())
        verified_set  = frozenset(database.load_verified_set())

        # --- Tier 1: UPC -----------------------------------------------------
        if "UPC" in search_methods:
            _update_progress(run_id, phase="Tier 1 / UPC batch search")
            tier1 = _tier1_upc(api, source_rows, run_id=run_id)
            for ri, items in tier1.items():
                for it in items:
                    candidates_by_row.setdefault(ri, []).append((it, "UPC"))
            # Score + persist this tier's rows immediately so UPC matches appear
            # in the UI right away (and aren't lost if the user stops later).
            _vet_and_store(
                run_id, [r for r in source_rows if r.row_idx in tier1],
                candidates_by_row, extracted_brands, blacklist_set, verified_set,
                max_rank, min_rank, mode, "Scoring UPC matches",
            )
            if _handle_control():
                return

        # --- Tier 2: Item ID -------------------------------------------------
        if "ItemID" in search_methods:
            _update_progress(run_id, phase="Tier 2 / Item ID search")
            tier2 = _tier2_itemid(api, source_rows, run_id=run_id)
            for ri, items in tier2.items():
                for it in items:
                    candidates_by_row.setdefault(ri, []).append((it, "ItemID"))
            _vet_and_store(
                run_id, [r for r in source_rows if r.row_idx in tier2],
                candidates_by_row, extracted_brands, blacklist_set, verified_set,
                max_rank, min_rank, mode, "Scoring Item ID matches",
            )
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
            _vet_and_store(
                run_id, [r for r in source_rows if r.row_idx in tier3],
                candidates_by_row, extracted_brands, blacklist_set, verified_set,
                max_rank, min_rank, mode, "Scoring title matches",
            )
            if _handle_control():
                return

        # Each tier vetted the rows it touched (with the full set of candidates
        # accumulated so far), so every row that produced a candidate has been
        # scored with all its sources by the last tier that touched it.
        _recompute_run_counts(run_id)
        clear_control(run_id)
        _update_progress(run_id, status="Complete", phase="Done", done=total, total=total)

    except PermissionError as exc:  # SP-API 403 — auth / expired secret / roles
        log.error("Analytics run %s aborted — SP-API 403: %s", run_id, exc)
        _update_progress(
            run_id, status="Error",
            phase=f"SP-API {exc}"[:200],
        )
    except Exception as exc:  # noqa: BLE001
        log.exception("Analytics run %s failed", run_id)
        _update_progress(
            run_id, status="Error",
            phase=f"{type(exc).__name__}: {exc}"[:200],
        )
