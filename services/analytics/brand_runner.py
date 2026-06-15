"""
Background worker for Brand Analytics runs.

For each brand name in search_terms:
  1. Keyword-search Amazon (search_by_keywords) for up to pages_per_brand pages
  2. Post-filter: only keep items where amazon.brand fuzzy-matches one of the
     searched brands (rapidfuzz ≥ 70%)
  3. BSR filter: apply min_rank / max_rank
  4. Upsert into brand_analytics_items

Progress is written to brand_analytics_runs so the UI can poll for live status.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any

from rapidfuzz import fuzz

from services import database
from services.analytics.runner import normalize_amazon_item
from services.analytics.product_categorizer import (
    categorize, category_distance, UNKNOWN,
    MEDICAL, DENTAL, LABORATORY, PERSONAL_CARE, BABY,
    CPG, JANITORIAL, INDUSTRIAL, OFFICE, FOOD, PET,
    HOME, GARDEN, TOOLS, SPORTS, CLOTHING, AUTOMOTIVE,
    ELECTRONICS, ARTS, TOYS, ENTERTAINMENT,
)
from services.spapi import get_catalog_api, sp_api_configured

log = logging.getLogger(__name__)

PAGE_SLEEP = 0.6

# Common product-type suffixes appended to brand names to widen search coverage.
# Mirrors AmazonAsinResearch1's BRAND_PRODUCT_TYPE search term generation.
# Maps the wizard's category_filter string to the Category object used by product_categorizer
_CATEGORY_FILTER_MAP = {
    "medical":       MEDICAL,
    "dental":        DENTAL,
    "laboratory":    LABORATORY,
    "personal_care": PERSONAL_CARE,
    "baby":          BABY,
    "cpg":           CPG,
    "janitorial":    JANITORIAL,
    "industrial":    INDUSTRIAL,
    "office":        OFFICE,
    "food":          FOOD,
    "pet":           PET,
    "home":          HOME,
    "sports":        SPORTS,
}

# Distance threshold: items whose BSR category is farther than this are dropped
_CATEGORY_FILTER_THRESHOLD = 4.5

_MEDICAL_PRODUCT_TYPES = [
    "wound care", "dressings", "bandages", "gauze", "medical supplies",
    "surgical", "gloves", "syringes", "catheters", "ostomy",
    "compression", "orthopedic", "incontinence",
]
_CPG_PRODUCT_TYPES = [
    "personal care", "health", "household", "cleaning", "beauty",
    "oral care", "skin care", "hair care",
]

# Maps run_id → "pause" | "stop"
_BRAND_RUN_CONTROL: dict[int, str] = {}
_BRAND_RUN_CONTROL_LOCK = threading.Lock()

# Maps run_id → "stop" for AI Fill
_AI_FILL_CONTROL: dict[int, str] = {}
_AI_FILL_CONTROL_LOCK = threading.Lock()


def request_stop_ai_fill(run_id: int) -> None:
    with _AI_FILL_CONTROL_LOCK:
        _AI_FILL_CONTROL[run_id] = "stop"


def _clear_ai_fill_control(run_id: int) -> None:
    with _AI_FILL_CONTROL_LOCK:
        _AI_FILL_CONTROL.pop(run_id, None)


def _check_ai_fill_control(run_id: int) -> str | None:
    with _AI_FILL_CONTROL_LOCK:
        return _AI_FILL_CONTROL.get(run_id)


def request_pause(run_id: int) -> None:
    with _BRAND_RUN_CONTROL_LOCK:
        _BRAND_RUN_CONTROL[run_id] = "pause"


def request_stop(run_id: int) -> None:
    with _BRAND_RUN_CONTROL_LOCK:
        _BRAND_RUN_CONTROL[run_id] = "stop"


def clear_control(run_id: int) -> None:
    with _BRAND_RUN_CONTROL_LOCK:
        _BRAND_RUN_CONTROL.pop(run_id, None)


def _check_control(run_id: int) -> str | None:
    with _BRAND_RUN_CONTROL_LOCK:
        return _BRAND_RUN_CONTROL.get(run_id)


# --------------------------------------------------------------------------- #
# Progress helpers
# --------------------------------------------------------------------------- #

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
    with database._LOCK, database._connect() as conn:
        conn.execute(
            f"UPDATE brand_analytics_runs SET {', '.join(fields)} WHERE id=?",
            params,
        )


# --------------------------------------------------------------------------- #
# Search keyword normalisation
# --------------------------------------------------------------------------- #

def _keyword_for_search(brand_name: str) -> str:
    """
    Convert brand names like 'Cryotherapy by DonJoy' → 'Cryotherapy DonJoy'
    so Amazon keyword search isn't confused by the preposition.
    """
    return re.sub(r'\s+by\s+', ' ', brand_name, flags=re.IGNORECASE).strip()


# --------------------------------------------------------------------------- #
# Brand fuzzy match
# --------------------------------------------------------------------------- #

def _brand_matches(
    amazon_brand: str,
    search_brands: list[str],
    title: str = "",
    strict: bool = False,
) -> bool:
    """
    Return True if the Amazon item is likely from one of the searched brands.

    Decision tree
    ─────────────
    1. Brand field set AND fuzzy-matches a search term → True.
       Threshold is length-adaptive: short names (≤5 chars) require 90%
       to avoid false positives like "Bode" matching "Bose" at 75%.
    2. Brand field set but doesn't match:
       - strict=True  → False immediately.  Used during Phase A keyword search
         where "brand field set but wrong brand" means it's a different product
         (e.g. Amazon brand="Shure" when searching for "Microflex").  The keyword
         may appear in the title by coincidence (competitor name, movie title, etc.)
         and the title fallback would wrongly pass those.
       - strict=False → fall through to title check (original behaviour).  Used
         for lookups where Amazon may store the parent-company name instead of
         the sub-brand (e.g. "DJO Global" instead of "Aircast").
    3. Brand field empty → title check is the only signal (both modes).

    Title check: sub-brand name must appear as a whole-word substring OR via
    partial_ratio ≥ 92 (tightened to reduce false positives on short names).
    """
    amz = (amazon_brand or "").strip().lower()

    def _threshold(name: str) -> int:
        # Short brand names need a tighter threshold to avoid 1-char-off collisions
        return 90 if len(name.strip()) <= 5 else 80

    # Step 1 — structured brand field matches
    if amz:
        for sb in search_brands:
            if not sb:
                continue
            sb_lower = sb.strip().lower()
            if fuzz.token_set_ratio(amz, sb_lower) >= _threshold(sb_lower):
                return True
        # Brand field is set but doesn't match any search term.
        # In strict mode this is a hard reject — the product belongs to a
        # different brand and the keyword hit is coincidental.
        if strict:
            return False

    # Steps 2 / 3 — title confirmation (non-strict, or brand field was empty)
    if title:
        title_lower = title.lower()
        for sb in search_brands:
            if not sb:
                continue
            sb_lower = sb.strip().lower()
            # Whole-word substring check (avoids "bode" matching "nobody")
            if re.search(r'\b' + re.escape(sb_lower) + r'\b', title_lower):
                return True
            # Tight fuzzy for spacing variants ("Air Cast" vs "Aircast")
            if len(sb_lower) > 5 and fuzz.partial_ratio(sb_lower, title_lower) >= 92:
                return True

    return False


# --------------------------------------------------------------------------- #
# Pack qty / UOM extraction
# --------------------------------------------------------------------------- #

def _extract_pack_qty(normalized: dict) -> str | None:
    """Return multipack count if > 1, e.g. '2', '3'.

    NOTE: Only ``item_package_quantity`` is used from SP-API attributes.
    ``number_of_items`` counts individual items (e.g. 4 electrodes), NOT the
    number of packs — using it for pack count produces wrong results like
    returning 4 for "4 Count (Pack of 5)".
    """
    val = normalized.get("item_package_quantity", "")
    if val:
        try:
            n = int(float(val))
            if n > 1:
                return str(n)
        except (ValueError, TypeError):
            pass

    # Fallback: scan title — "pack of N" is the most unambiguous signal
    title = normalized.get("title", "") or ""
    for pat in (
        r'pack\s+of\s+(\d+)',       # "Pack of 5"
        r'(\d+)\s*[-\s]*pack\b',    # "5-Pack" / "5 Pack"
        r'set\s+of\s+(\d+)',        # "Set of 3"
        r'box\s+of\s+(\d+)',        # "Box of 12"
        r'(\d+)\s*-count\b',        # "5-Count"
        r'\((\d+)\)',               # plain "(5)" as last resort
    ):
        m = re.search(pat, title, re.IGNORECASE)
        if m:
            n = int(m.group(1))
            if n > 1:
                return str(n)
    return None


def _extract_uom_qty(normalized: dict) -> str | None:
    """Return unit-of-measure quantity, e.g. '32 Count', '500 Milliliter'."""
    raw   = normalized.get("_raw") or {}
    attrs = raw.get("attributes") or {}

    # SP-API unit_count + unit_count_type
    count_val = None
    for entry in (attrs.get("unit_count") or []):
        if isinstance(entry, dict):
            v = entry.get("value")
            if v is not None:
                try:
                    count_val = int(float(v)) if float(v) == int(float(v)) else float(v)
                except (ValueError, TypeError):
                    pass
            break
    count_type = None
    for entry in (attrs.get("unit_count_type") or []):
        if isinstance(entry, dict) and entry.get("value"):
            count_type = str(entry["value"]).strip()
            break

    if count_val is not None and count_type:
        return f"{count_val} {count_type}"
    if count_val is not None:
        return str(count_val)

    # Fallback: scan title for common UOM patterns.
    #
    # Strategy — count units take priority over size/dosage units:
    #   Pass 1  parenthesized count   "(30 Easy to Swallow Capsules)"
    #   Pass 2  adjacent count        "4 Count"  "120 Capsules"
    #   Pass 3  words-between count   "30 Easy to Swallow Capsules"
    #   Pass 4  adjacent size/volume  "500 ml"  "32 oz"
    #              (dosage units like mg/mcg are intentionally excluded —
    #               they are concentration specs, not selling-unit counts)
    title = normalized.get("title", "") or ""

    # Units that represent a SELLING COUNT (tablets, capsules, pieces, etc.)
    _COUNT_UNITS = (
        r'capsules?|tablets?|caplets?|softgels?|gummies?|pills?|'
        r'lozenges?|chews?|gelcaps?|softcaps?|'
        r'pieces?|pcs?|count|ct\.?'
    )
    # Units that represent a SIZE / VOLUME (for liquid/powder products)
    _SIZE_UNITS = (
        r'oz\.?|fl\.?\s*oz\.?|ml|g\b|kg|lb\.?|lbs?\.?|ounces?|liters?|litres?'
    )

    # ── Pass 1: parenthesized form with optional adjective words ─────────────
    # "(30 Easy to Swallow Capsules)" or "(120 Capsules)"
    m = re.search(
        rf'\((\d+)\s+(?:\w+\s+){{0,5}}({_COUNT_UNITS})\)',
        title, re.IGNORECASE,
    )
    if m:
        return f"{m.group(1)} {m.group(2).strip()}"

    # ── Pass 2: count unit immediately adjacent to number ────────────────────
    # "4 Count" / "120 Capsules" / "30ct"
    m = re.search(
        rf'(\d+(?:\.\d+)?)\s*({_COUNT_UNITS})',
        title, re.IGNORECASE,
    )
    if m:
        return f"{m.group(1)} {m.group(2).strip()}"

    # ── Pass 3: count unit with 1-5 adjective words between number and unit ──
    # "30 Easy to Swallow Capsules" not in parentheses
    _COUNT_SUPP = (
        r'capsules?|tablets?|caplets?|softgels?|gummies?|pills?|'
        r'lozenges?|chews?|gelcaps?|softcaps?'
    )
    m = re.search(
        rf'(?<!\d)(\d+)\s+(?:\w+\s+){{1,5}}({_COUNT_SUPP})\b',
        title, re.IGNORECASE,
    )
    if m:
        return f"{m.group(1)} {m.group(2).strip()}"

    # ── Pass 4: size/volume unit immediately adjacent (liquids, powders) ─────
    m = re.search(
        rf'(\d+(?:\.\d+)?)\s*({_SIZE_UNITS})',
        title, re.IGNORECASE,
    )
    if m:
        return f"{m.group(1)} {m.group(2).strip()}"

    return None


# --------------------------------------------------------------------------- #
# DB upsert
# --------------------------------------------------------------------------- #

def _upsert_item(run_id: int, brand_searched: str, normalized: dict) -> None:
    asin  = normalized.get("asin", "")
    title = normalized.get("title", "") or ""
    bsr   = normalized.get("sales_rank")
    bsr_cat = normalized.get("sales_rank_category", "") or ""
    mpn   = normalized.get("mpn", "") or ""
    upc   = normalized.get("upc", "") or ""
    ean   = normalized.get("ean", "") or ""
    gtin  = normalized.get("gtin", "") or ""   # GTIN-14 from SP-API (or derived from UPC/EAN)
    amz_brand = (normalized.get("brand") or normalized.get("manufacturer") or "").strip()
    pack_qty = _extract_pack_qty(normalized)
    uom_qty  = _extract_uom_qty(normalized)

    image = ""
    raw   = normalized.get("_raw") or {}
    # Try to pull image URL from raw SP-API payload
    for img_group in (raw.get("images") or []):
        imgs = img_group.get("images") or []
        for img in imgs:
            if img.get("variant") in ("MAIN", "PT01") or not image:
                image = img.get("link", "") or ""
                break
        if image:
            break

    slim = {k: v for k, v in normalized.items() if k != "_raw"}

    # Single locked connection: read override then upsert (avoids two lock acquisitions)
    with database._LOCK, database._connect() as conn:
        ov = conn.execute(
            "SELECT mpn, upc, ean, gtin FROM asin_identifier_overrides WHERE asin=?",
            (asin,),
        ).fetchone()
        if ov:
            if ov["mpn"]  is not None: mpn  = ov["mpn"]
            if ov["upc"]  is not None: upc  = ov["upc"]
            if ov["ean"]  is not None: ean  = ov["ean"]
            if ov["gtin"] is not None: gtin = ov["gtin"]

        conn.execute(
            """
            INSERT INTO brand_analytics_items
              (run_id, brand_searched, asin, title, bsr, bsr_category,
               mpn, upc, ean, gtin, amz_brand, pack_qty, uom_qty, image_url, data_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(run_id, asin) DO UPDATE SET
              title=excluded.title,
              bsr=excluded.bsr,
              bsr_category=excluded.bsr_category,
              mpn=excluded.mpn,
              upc=excluded.upc,
              ean=excluded.ean,
              gtin=excluded.gtin,
              amz_brand=excluded.amz_brand,
              pack_qty=excluded.pack_qty,
              uom_qty=excluded.uom_qty,
              image_url=excluded.image_url,
              data_json=excluded.data_json,
              updated_at=CURRENT_TIMESTAMP
            """,
            (
                run_id, brand_searched, asin, title,
                int(bsr) if bsr else None,
                bsr_cat, mpn or None, upc or None, ean or None, gtin or None,
                amz_brand or None,
                pack_qty, uom_qty,
                image or None,
                json.dumps(slim, default=str),
            ),
        )


# --------------------------------------------------------------------------- #
# Main pipeline
# --------------------------------------------------------------------------- #

def _build_search_queries(search_terms: list[str], vetting_mode: str) -> list[tuple[str, str]]:
    """
    Build all keyword queries for a brand run.

    Returns list of (query_string, label) tuples.

    Strategy (mirrors AmazonAsinResearch1 BRAND / BRAND_PRODUCT_TYPE terms):
      1. Each brand name alone → "Hartmann", "Dermaplast", …
      2. Each brand name + product-type suffix → "Hartmann wound care", …
         (only the FIRST/main term gets product-type expansion to avoid explosion)
    """
    product_types = (
        _MEDICAL_PRODUCT_TYPES if vetting_mode == "medical" else _CPG_PRODUCT_TYPES
    )
    queries: list[tuple[str, str]] = []
    seen: set[str] = set()

    def _add(q: str, label: str) -> None:
        key = q.lower().strip()
        if key and key not in seen:
            seen.add(key)
            queries.append((q.strip(), label))

    # Brand-name-only queries
    for term in search_terms:
        kw = _keyword_for_search(term)
        _add(kw, f'"{term}"')

    # Product-type expansion — only for the first/main term to keep call count sane
    if search_terms:
        main_kw = _keyword_for_search(search_terms[0])
        for pt in product_types:
            _add(f"{main_kw} {pt}", f'"{search_terms[0]} {pt}"')

    return queries


def _brand_pipeline(
    run_id: int,
    search_terms: list[str],
    min_rank: int,
    max_rank: int,
    pages_per_brand: int,
    vetting_mode: str = "cpg",
    category_filter: str = "",
) -> None:
    # 0 = unlimited (paginate until Amazon has no more results)
    unlimited = pages_per_brand == 0
    pages_per_brand = max(1, int(pages_per_brand or 3))
    try:
        catalog = get_catalog_api()
    except Exception as exc:
        _update_progress(run_id, status="Error", phase=f"SP-API init failed: {exc}")
        return

    # Search strategy — three phases:
    #
    # Phase A (keyword search per sub-brand):
    #   keywords=sub_brand_name → finds products where the brand name is in title/desc.
    #   Brand-match post-filter applied. Also collects the exact Amazon brand field
    #   values that appear in results so Phase B can use them verbatim.
    #
    # Phase B (pure brandNames scan — no keyword):
    #   SP-API brandNames filter is OR-based and is used WITHOUT a keyword, so Amazon
    #   returns ALL products where the brand field matches — no keyword bias.
    #   Uses: (a) user-supplied search terms, PLUS (b) any Amazon brand variants
    #   discovered during Phase A (e.g. "Hartmann H", "Paul Hartmann AG").
    #   No brand-match post-filter needed — Amazon already guarantees correctness.
    #
    # Phase C (extra variants discovered only during Phase B):
    #   Any brand name first seen in Phase B results that wasn't searched yet gets
    #   its own brandNames-only scan.  Usually small (1-2 extra names at most).

    # Resolve category filter once
    _cat_target = _CATEGORY_FILTER_MAP.get(category_filter.lower()) if category_filter else None
    if _cat_target:
        log.info("brand_runner run=%d: category filter → %s (threshold %.1f)",
                 run_id, _cat_target.name, _CATEGORY_FILTER_THRESHOLD)

    # Build all keyword queries (brand names + product-type expansions)
    all_queries = _build_search_queries(search_terms, vetting_mode)
    # Normalised search terms for fuzzy brand-matching and as seed for Phase B
    all_brand_names_lower: set[str] = {_keyword_for_search(t).lower() for t in search_terms}

    # total_pages: Phase A (one block per query) + Phase B block + Phase C (estimated 1 block)
    total_pages = 0 if unlimited else (len(all_queries) + 2) * pages_per_brand
    done_pages = 0

    log.info("brand_runner run=%d: %d keyword queries + brandNames scan, %s pages each",
             run_id, len(all_queries), "unlimited" if unlimited else pages_per_brand)
    _update_progress(run_id, status="Searching", phase="Starting", done=0, total=total_pages)

    asin_count = 0
    # Collect exact Amazon brand field values seen during Phase A so Phase B
    # can search them verbatim (catches variants like "Hartmann H" or "Paul Hartmann").
    discovered_amz_brands: dict[str, str] = {}  # lowercase → original-case

    # ── Phase A: multi-angle keyword search ───────────────────────────────── #
    for q_idx, (query, q_label) in enumerate(all_queries, start=1):
        ctrl = _check_control(run_id)
        if ctrl == "stop":
            clear_control(run_id)
            _update_progress(run_id, status="Stopped", phase="Stopped by user")
            return
        if ctrl == "pause":
            _update_progress(run_id, status="Paused", phase="Paused")
            while True:
                time.sleep(2)
                c = _check_control(run_id)
                if c == "stop":
                    clear_control(run_id)
                    _update_progress(run_id, status="Stopped", phase="Stopped by user")
                    return
                if c != "pause":
                    break
            _update_progress(run_id, status="Searching",
                             phase=f"Resumed — {q_label} ({q_idx}/{len(all_queries)})")

        next_token: str | None = None
        pages_fetched = 0
        n_results_total: int | None = None

        while unlimited or pages_fetched < pages_per_brand:
            ctrl = _check_control(run_id)
            if ctrl in ("stop", "pause"):
                break

            try:
                result = catalog.search_by_keywords(
                    keywords=query,
                    page_size=20,
                    page_token=next_token,
                )
            except Exception as exc:
                log.warning("brand search failed for %r page %d: %s", query, pages_fetched, exc)
                break

            if pages_fetched == 0:
                n_results_total = result.get("numberOfResults")
                if n_results_total is not None:
                    log.info("brand_runner: query=%r → numberOfResults=%d", query, n_results_total)

            items = result.get("items") or []
            for raw_item in items:
                normalized = normalize_amazon_item(raw_item)
                amz_brand = normalized.get("brand") or normalized.get("manufacturer") or ""
                amz_title = normalized.get("title") or ""

                # strict=True: if Amazon brand field is set but doesn't match, reject
                # immediately.  Prevents keyword coincidences (e.g. "Ringers" keyword
                # matching "Dead Ringers" DVD, "Edge" matching shaving gel) from
                # slipping through via the title fallback.
                if not _brand_matches(amz_brand, search_terms, title=amz_title, strict=True):
                    continue

                # Collect the exact Amazon brand name for Phase B seed
                if amz_brand:
                    discovered_amz_brands[amz_brand.strip().lower()] = amz_brand.strip()

                if _cat_target is not None:
                    _amz_cat = categorize("", normalized.get("sales_rank_category") or "")
                    if _amz_cat != UNKNOWN:
                        if category_distance(_cat_target, _amz_cat) > _CATEGORY_FILTER_THRESHOLD:
                            continue

                bsr = normalized.get("sales_rank")
                if bsr:
                    if max_rank > 0 and bsr > max_rank:
                        continue
                    if min_rank > 0 and bsr < min_rank:
                        continue

                _upsert_item(run_id, query, normalized)
                asin_count += 1

            pages_fetched += 1
            done_pages += 1

            n_hint = f" / ~{n_results_total} on Amazon" if n_results_total else ""
            _update_progress(
                run_id,
                phase=f"{q_label} — p{pages_fetched}{n_hint} · {asin_count} kept",
                done=done_pages,
                total=total_pages,
            )

            next_token = (result.get("pagination") or {}).get("nextToken")
            if not next_token:
                if not unlimited:
                    done_pages += pages_per_brand - pages_fetched
                    _update_progress(run_id, done=done_pages, total=total_pages)
                break

            time.sleep(PAGE_SLEEP)

    # ── Phase B: pure brandNames scan (no keyword) ────────────────────────── #
    # Build the complete brand name set: user-supplied terms + Amazon variants
    # discovered during Phase A.  No keyword — Amazon returns the complete brand
    # catalog for these names without keyword filtering.
    ctrl = _check_control(run_id)
    if ctrl not in ("stop", "pause"):
        # Merge user search terms (normalised) with discovered variants
        phase_b_names_lower: dict[str, str] = {
            _keyword_for_search(t).lower(): _keyword_for_search(t) for t in search_terms
        }
        phase_b_names_lower.update(discovered_amz_brands)
        phase_b_brand_list = list(phase_b_names_lower.values())

        log.info("brand_runner run=%d: Phase B brandNames scan with %d names: %s",
                 run_id, len(phase_b_brand_list), phase_b_brand_list)

        next_token = None
        pages_fetched = 0
        phase_b_new: dict[str, str] = {}  # brand names newly discovered in Phase B results

        # Phase B always exhausts the token chain regardless of pages_per_brand.
        # It is a targeted brand-registry lookup (no keyword), so every page is
        # guaranteed to be relevant — unlike Phase A keyword searches which can
        # drift into unrelated territory as pages increase.
        while True:
            ctrl = _check_control(run_id)
            if ctrl in ("stop", "pause"):
                break
            try:
                result = catalog.search_by_brand_names(
                    brand_names=phase_b_brand_list,
                    page_size=20,
                    page_token=next_token,
                )
            except Exception as exc:
                log.warning("Phase B brandNames scan failed page %d: %s", pages_fetched, exc)
                break

            items = result.get("items") or []
            for raw_item in items:
                normalized = normalize_amazon_item(raw_item)
                # Amazon already filtered by brand — no extra post-filter needed.
                # Collect any new brand name variants for Phase C.
                amz_b = (normalized.get("brand") or normalized.get("manufacturer") or "").strip()
                if amz_b:
                    k = amz_b.lower()
                    if k not in phase_b_names_lower:
                        phase_b_new[k] = amz_b

                if _cat_target is not None:
                    _amz_cat = categorize("", normalized.get("sales_rank_category") or "")
                    if _amz_cat != UNKNOWN:
                        if category_distance(_cat_target, _amz_cat) > _CATEGORY_FILTER_THRESHOLD:
                            continue

                bsr = normalized.get("sales_rank")
                if bsr:
                    if max_rank > 0 and bsr > max_rank:
                        continue
                    if min_rank > 0 and bsr < min_rank:
                        continue

                brand_label = amz_b or phase_b_brand_list[0]
                _upsert_item(run_id, brand_label, normalized)
                asin_count += 1

            pages_fetched += 1
            done_pages += 1
            _update_progress(
                run_id,
                phase=f"Brand catalog scan (no-keyword) — page {pages_fetched} · {asin_count} found",
                done=done_pages,
                total=total_pages,
            )

            next_token = (result.get("pagination") or {}).get("nextToken")
            if not next_token:
                break  # exhausted — no page-count adjustment needed
            time.sleep(PAGE_SLEEP)

        # ── Phase C: scan any brand-name variants first seen in Phase B ───── #
        # These are brand names Amazon returned that weren't in our Phase B list,
        # meaning they are related but distinct entries (e.g. a sub-brand stored
        # under a slightly different name in Amazon's brand registry).
        # Only scan them if they fuzzy-match our original search terms — otherwise
        # they are genuinely unrelated brands that slipped through Amazon's filter.
        ctrl = _check_control(run_id)
        if ctrl not in ("stop", "pause") and phase_b_new:
            # Keep only names that look related to our original terms
            related_new = [
                name for name in phase_b_new.values()
                if any(
                    fuzz.token_set_ratio(name.lower(), t.lower()) >= 70
                    for t in search_terms
                )
            ]
            if related_new:
                log.info("brand_runner run=%d: Phase C — %d newly discovered variants: %s",
                         run_id, len(related_new), related_new)
                next_token = None
                pages_fetched = 0
                # Phase C also exhausts its token chain — same rationale as Phase B.
                while True:
                    ctrl = _check_control(run_id)
                    if ctrl in ("stop", "pause"):
                        break
                    try:
                        result = catalog.search_by_brand_names(
                            brand_names=related_new,
                            page_size=20,
                            page_token=next_token,
                        )
                    except Exception as exc:
                        log.warning("Phase C scan failed page %d: %s", pages_fetched, exc)
                        break

                    items = result.get("items") or []
                    for raw_item in items:
                        normalized = normalize_amazon_item(raw_item)
                        if _cat_target is not None:
                            _amz_cat = categorize("", normalized.get("sales_rank_category") or "")
                            if _amz_cat != UNKNOWN:
                                if category_distance(_cat_target, _amz_cat) > _CATEGORY_FILTER_THRESHOLD:
                                    continue
                        bsr = normalized.get("sales_rank")
                        if bsr:
                            if max_rank > 0 and bsr > max_rank:
                                continue
                            if min_rank > 0 and bsr < min_rank:
                                continue
                        amz_b = (normalized.get("brand") or normalized.get("manufacturer") or "").strip()
                        _upsert_item(run_id, amz_b or related_new[0], normalized)
                        asin_count += 1

                    pages_fetched += 1
                    done_pages += 1
                    _update_progress(
                        run_id,
                        phase=f"Variant scan ({', '.join(related_new[:2])}) — p{pages_fetched} · {asin_count} found",
                        done=done_pages,
                        total=total_pages,
                    )
                    next_token = (result.get("pagination") or {}).get("nextToken")
                    if not next_token:
                        break  # exhausted
                    time.sleep(PAGE_SLEEP)

    # Mark last_asin_updated_at on the run
    with database._LOCK, database._connect() as conn:
        conn.execute(
            "UPDATE brand_analytics_runs "
            "SET last_asin_updated_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP "
            "WHERE id=?",
            (run_id,),
        )

    _update_progress(
        run_id,
        status="Complete",
        phase=f"Done — {asin_count} ASINs found",
        done=total_pages,
        total=total_pages,
    )


def start_brand_run(
    run_id: int,
    search_terms: list[str],
    min_rank: int,
    max_rank: int,
    pages_per_brand: int,
    vetting_mode: str = "cpg",
    category_filter: str = "",
) -> None:
    """Launch the brand analytics pipeline in a background daemon thread."""
    clear_control(run_id)
    pages_per_brand = int(pages_per_brand or 10)
    if pages_per_brand < 0:
        pages_per_brand = 0  # normalise negatives to unlimited
    t = threading.Thread(
        target=_brand_pipeline,
        args=(run_id, search_terms, min_rank, max_rank, pages_per_brand, vetting_mode, category_filter),
        daemon=True,
    )
    t.start()


# --------------------------------------------------------------------------- #
# AI Fill pipeline
# --------------------------------------------------------------------------- #

_AI_FILL_SYSTEM = (
    "You are a product data extractor. Given a product title, description, and "
    "bullet points, extract any of the following identifiers that are explicitly "
    "stated in the text: MPN (manufacturer part number / model number / item number), "
    "UPC (12-digit numeric barcode), EAN (13-digit numeric barcode), GTIN (14-digit). "
    "MPNs are often found in titles as model numbers (e.g. 'Model AC141FB02-M'). "
    "UPC/EAN/GTIN are numeric codes rarely stated in product text — only return them "
    "if explicitly present as a numeric string. "
    "Return ONLY a raw JSON object with keys 'mpn', 'upc', 'ean', 'gtin'. "
    "Use null for any identifier not found. Never invent or guess values."
)


def _ai_fill_one(item: dict, client: Any) -> dict:
    from services.ai_recheck import _is_anthropic, _extract_json
    title   = item.get("title") or ""
    data    = {}
    try:
        data = json.loads(item.get("data_json") or "{}")
    except Exception:
        pass
    desc    = data.get("description") or ""
    bullets = " ".join(data.get("bullet_points") or [])

    user_msg = json.dumps({
        "title":       title,
        "description": desc[:800],
        "bullets":     bullets[:800],
    })

    try:
        if _is_anthropic(client):
            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                system=_AI_FILL_SYSTEM,
                messages=[{"role": "user", "content": user_msg}],
                temperature=0,
            )
            raw = resp.content[0].text if resp.content else "{}"
        else:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": _AI_FILL_SYSTEM},
                    {"role": "user",   "content": user_msg},
                ],
                max_tokens=128,
                temperature=0,
            )
            raw = resp.choices[0].message.content or "{}"
        return _extract_json(raw)
    except Exception as exc:
        log.warning("ai_fill failed for asin %s: %s", item.get("asin"), exc)
        return {}


def _ai_fill_pipeline(run_id: int, client: Any) -> None:
    _clear_ai_fill_control(run_id)

    # Load items needing fill — only those missing MPN (UPC/EAN/GTIN are
    # almost never in product text, but we try all four to be thorough)
    with database._connect() as conn:
        rows = conn.execute(
            "SELECT id, asin, title, data_json FROM brand_analytics_items "
            "WHERE run_id=? AND ai_fill_status IS NULL "
            "AND (mpn IS NULL OR upc IS NULL OR ean IS NULL OR gtin IS NULL)",
            (run_id,),
        ).fetchall()

    total = len(rows)
    if total == 0:
        with database._LOCK, database._connect() as conn:
            conn.execute(
                "UPDATE brand_analytics_runs SET ai_fill_status='done', "
                "ai_fill_done=0, ai_fill_total=0, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=?",
                (run_id,),
            )
        return

    with database._LOCK, database._connect() as conn:
        conn.execute(
            "UPDATE brand_analytics_runs SET ai_fill_status='running', "
            "ai_fill_done=0, ai_fill_total=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (total, run_id),
        )

    done = 0
    for row in rows:
        if _check_ai_fill_control(run_id) == "stop":
            _clear_ai_fill_control(run_id)
            with database._LOCK, database._connect() as conn:
                conn.execute(
                    "UPDATE brand_analytics_runs SET ai_fill_status='stopped', "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (run_id,),
                )
            return

        item = dict(row)
        result = _ai_fill_one(item, client)

        ai_mpn  = (result.get("mpn")  or "").strip() or None
        ai_upc  = (result.get("upc")  or "").strip() or None
        ai_ean  = (result.get("ean")  or "").strip() or None
        ai_gtin = (result.get("gtin") or "").strip() or None

        # Single connection: item update + progress counter (saves lock acquisition)
        done += 1
        with database._LOCK, database._connect() as conn:
            conn.execute(
                "UPDATE brand_analytics_items SET "
                "ai_mpn=?, ai_upc=?, ai_ean=?, ai_gtin=?, ai_fill_status='done', "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (ai_mpn, ai_upc, ai_ean, ai_gtin, item["id"]),
            )
            conn.execute(
                "UPDATE brand_analytics_runs SET ai_fill_done=?, "
                "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (done, run_id),
            )

    with database._LOCK, database._connect() as conn:
        conn.execute(
            "UPDATE brand_analytics_runs SET ai_fill_status='done', "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (run_id,),
        )


def start_ai_fill(run_id: int, client: Any) -> None:
    """Launch AI Fill in a background daemon thread."""
    t = threading.Thread(
        target=_ai_fill_pipeline,
        args=(run_id, client),
        daemon=True,
    )
    t.start()
