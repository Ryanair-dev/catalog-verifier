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

def _class_ids(normalized: dict) -> set[str]:
    """Distinct Amazon classificationIds for an item (read from its raw salesRanks).
    These seed the Phase D per-category drill that beats the per-query result cap."""
    out: set[str] = set()
    for sr in ((normalized.get("_raw") or {}).get("salesRanks") or []):
        for cr in (sr.get("classificationRanks") or []):
            cid = str(cr.get("classificationId") or "").strip()
            if cid:
                out.add(cid)
    return out


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

    # FBA storage fee (off-peak, Q4 peak) from the SP-API dimensions — free, no extra call.
    storage_off = storage_peak = None
    try:
        from services.storage_fees import extract_dimensions, calc_storage_fee
        _dims = extract_dimensions(raw.get("attributes") or {})
        if all(_dims.get(k) is not None for k in ("length_cm", "width_cm", "height_cm", "weight_g")):
            storage_off, storage_peak = calc_storage_fee(
                _dims["length_cm"], _dims["width_cm"], _dims["height_cm"], _dims["weight_g"])
    except Exception:
        pass

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
               mpn, upc, ean, gtin, amz_brand, pack_qty, uom_qty, image_url,
               storage_fee, storage_fee_peak, data_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
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
              storage_fee=excluded.storage_fee,
              storage_fee_peak=excluded.storage_fee_peak,
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
                storage_off, storage_peak,
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


def _keepa_brand_pipeline(
    run_id: int,
    search_terms: list[str],
    min_rank: int,
    max_rank: int,
    vetting_mode: str,
    category_filter: str,
) -> None:
    """Discover ALL of a brand/manufacturer's ASINs from Keepa (past SP-API's recall
    ceiling), then enrich each via SP-API (BSR / category / UPC/EAN/GTIN/MPN / dims →
    storage fee). Keeps the same category filtering + item storage as the SP-API path.
    Keepa token spend/balance are recorded on the run. Buy-box + eligibility are added
    by their own steps afterward."""
    from services import keepa

    # brand vs manufacturer for the Keepa selection
    with database._connect() as conn:
        row = conn.execute(
            "SELECT search_type FROM brand_analytics_runs WHERE id=?", (run_id,)
        ).fetchone()
    search_type = ((row[0] if row else "") or "brand").lower()

    try:
        catalog = get_catalog_api()
    except Exception as exc:  # noqa: BLE001
        _update_progress(run_id, status="Error", phase=f"SP-API init failed: {exc}")
        return

    _cat_target = _CATEGORY_FILTER_MAP.get(category_filter.lower()) if category_filter else None

    # ── 1) Keepa discovery ────────────────────────────────────────────────── #
    _update_progress(run_id, status="Searching", phase="Keepa — finding ASINs…", done=0, total=0)
    all_asins: set[str] = set()
    tok_spent = 0
    tok_left = None
    for term in search_terms:
        if _check_control(run_id) == "stop":
            _update_progress(run_id, status="Stopped", phase="Stopped by user")
            return

        def _prog(found: int, target: int, _t=term) -> None:
            _update_progress(run_id, status="Searching",
                             phase=f"Keepa — {_t}: {found:,}/{target:,} ASINs",
                             done=found, total=max(target, 1))
        try:
            kw = {"manufacturer": term} if search_type == "manufacturer" else {"brand": term}
            res = keepa.find_asins(progress=_prog, **kw)
        except Exception as exc:  # noqa: BLE001
            log.warning("[keepa] find_asins failed for %r: %s", term, exc)
            continue
        all_asins |= set(res["asins"])
        tok_spent += int(res.get("tokens_spent") or 0)
        tok_left = res.get("tokens_left")

    with database._LOCK, database._connect() as conn:
        conn.execute(
            "UPDATE brand_analytics_runs SET source='keepa', keepa_tokens_spent=?, "
            "keepa_tokens_left=? WHERE id=?",
            (tok_spent, tok_left, run_id),
        )

    asins = sorted(all_asins)
    total = len(asins)
    log.info("brand_runner run=%d: Keepa found %d ASINs (%s); enriching via SP-API",
             run_id, total, search_type)

    # ── 2) SP-API catalog enrichment (BSR / category / IDs / dims→storage) ──── #
    _update_progress(run_id, status="Searching",
                     phase=f"Enriching {total:,} ASINs (BSR / IDs / storage)…", done=0, total=total)
    kept = 0
    brand_label = search_terms[0] if search_terms else ""

    def _process(raw_item) -> bool:
        try:
            normalized = normalize_amazon_item(raw_item)
        except Exception:
            return False
        if _cat_target:
            amz_cat = categorize("", normalized.get("sales_rank_category") or "")
            if amz_cat != UNKNOWN and category_distance(_cat_target, amz_cat) >= _CATEGORY_FILTER_THRESHOLD:
                return False
        _upsert_item(run_id, brand_label, normalized)
        return True

    for i in range(0, total, 20):
        if _check_control(run_id) == "stop":
            _update_progress(run_id, status="Stopped", phase="Stopped by user")
            return
        while _check_control(run_id) == "pause":
            _update_progress(run_id, status="Paused", phase="Paused")
            time.sleep(1)
            if _check_control(run_id) == "stop":
                _update_progress(run_id, status="Stopped", phase="Stopped by user")
                return
        batch = asins[i:i + 20]
        got: set[str] = set()
        try:
            resp = catalog.search_by_identifiers(
                batch, id_type="ASIN",
                included_data="summaries,identifiers,attributes,salesRanks,images",
            )
            items = resp.get("items") or []
        except PermissionError as exc:
            _update_progress(run_id, status="Error", phase=f"SP-API {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            log.warning("[keepa-enrich] batch %d failed: %s", i, exc)
            items = []
        for raw_item in items:
            a = raw_item.get("asin")
            if a:
                got.add(a)
            if _process(raw_item):
                kept += 1
        # searchCatalogItems COLLAPSES/caps variation siblings, silently returning only
        # ~half of a brand's ASINs. Recover the dropped ones via single getCatalogItem
        # (each ASIN resolves individually) so the run matches Keepa's full count.
        for a in batch:
            if a in got:
                continue
            try:
                raw_item = catalog.get_by_asin(a)
            except PermissionError as exc:
                _update_progress(run_id, status="Error", phase=f"SP-API {exc}")
                return
            except Exception:
                raw_item = None
            if raw_item and raw_item.get("asin") and _process(raw_item):
                kept += 1
        _update_progress(run_id, status="Searching", done=min(i + 20, total), total=total,
                         phase=f"Enriching {min(i + 20, total):,}/{total:,} — {kept:,} kept")

    _update_progress(run_id, status="Complete",
                     phase=f"Done — {kept:,} products · Keepa {tok_spent} tokens",
                     done=total, total=total)


def _brand_pipeline(
    run_id: int,
    search_terms: list[str],
    min_rank: int,
    max_rank: int,
    pages_per_brand: int,
    vetting_mode: str = "cpg",
    category_filter: str = "",
) -> None:
    # When a Keepa key is configured, discover the FULL brand/manufacturer catalog via
    # Keepa (beats SP-API's ~6k recall ceiling) and enrich via SP-API. Falls back to the
    # SP-API keyword search below when Keepa isn't configured.
    try:
        from services import keepa
        if keepa.is_configured():
            _keepa_brand_pipeline(run_id, search_terms, min_rank, max_rank,
                                  vetting_mode, category_filter)
            return
    except Exception as exc:  # noqa: BLE001
        log.warning("brand_runner run=%d: Keepa path failed (%s); using SP-API search", run_id, exc)

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
    # Distinct Amazon category (classificationId) values seen on found items —
    # seeds the Phase D per-category drill that beats the per-query result cap.
    seen_class_ids: set[str] = set()
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
                seen_class_ids |= _class_ids(normalized)
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
                seen_class_ids |= _class_ids(normalized)
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
                        seen_class_ids |= _class_ids(normalized)
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

    # ── Phase D: category-partitioned brand scan ─────────────────────────── #
    # Amazon caps each keyword query at ~a few thousand results, so a large brand
    # keyword ("McKesson" → ~2,900) truncates when paged. Re-running the SAME
    # keyword + brandNames filter but restricted to ONE category (classificationId)
    # at a time returns each category's own small slice (well under the cap), so
    # paging each to exhaustion and unioning recovers the depth-capped tail.
    # (SP-API rejects a brandNames-only query — it must ride on the keyword.)
    # Seed categories are the classificationIds seen on items already found (A-C).
    ctrl = _check_control(run_id)
    if ctrl not in ("stop", "pause") and seen_class_ids and search_terms:
        _names = {_keyword_for_search(t).lower(): _keyword_for_search(t) for t in search_terms}
        _names.update(discovered_amz_brands)
        phase_d_brands = list(_names.values())
        # SP-API requires a keyword, so each per-category scan uses the primary
        # brand term as the keyword + the brandNames filter for correctness.
        phase_d_kw = _keyword_for_search(search_terms[0])
        cats = sorted(seen_class_ids)
        if not unlimited:
            total_pages += len(cats) * pages_per_brand
        log.info("brand_runner run=%d: Phase D — drilling %d categories with brands %s",
                 run_id, len(cats), phase_d_brands)

        for c_idx, cid in enumerate(cats, start=1):
            if not phase_d_brands or not phase_d_kw:
                break
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
                                 phase=f"Resumed — category {c_idx}/{len(cats)}")

            next_token = None
            pages_fetched = 0
            while True:
                ctrl = _check_control(run_id)
                if ctrl in ("stop", "pause"):
                    break
                try:
                    result = catalog.search_by_keywords(
                        keywords=phase_d_kw,
                        brand_names=phase_d_brands,
                        classification_ids=[cid],
                        page_size=20,
                        page_token=next_token,
                    )
                except Exception as exc:
                    log.warning("Phase D category %s failed page %d: %s", cid, pages_fetched, exc)
                    break

                for raw_item in (result.get("items") or []):
                    normalized = normalize_amazon_item(raw_item)
                    if _cat_target is not None:
                        _amz_cat = categorize("", normalized.get("sales_rank_category") or "")
                        if _amz_cat != UNKNOWN and category_distance(_cat_target, _amz_cat) > _CATEGORY_FILTER_THRESHOLD:
                            continue
                    bsr = normalized.get("sales_rank")
                    if bsr:
                        if max_rank > 0 and bsr > max_rank:
                            continue
                        if min_rank > 0 and bsr < min_rank:
                            continue
                    amz_b = (normalized.get("brand") or normalized.get("manufacturer") or "").strip()
                    _upsert_item(run_id, amz_b or phase_d_brands[0], normalized)
                    asin_count += 1

                pages_fetched += 1
                done_pages += 1
                _tot = 0 if unlimited else max(total_pages, done_pages)
                _update_progress(
                    run_id,
                    phase=f"Category scan {c_idx}/{len(cats)} — p{pages_fetched} · {asin_count} found",
                    done=done_pages,
                    total=_tot,
                )
                next_token = (result.get("pagination") or {}).get("nextToken")
                if not next_token:
                    break
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

_AI_FILL_FIELDS = ["mpn", "upc", "ean", "gtin"]
_ID_DESC = {
    "mpn":  "MPN (manufacturer part number / model number / item number)",
    "upc":  "UPC (12-digit numeric barcode)",
    "ean":  "EAN (13-digit numeric barcode)",
    "gtin": "GTIN (14-digit numeric barcode)",
}


def _clean_ai_fill_fields(fields) -> list[str]:
    """Validated subset of the ID columns to fill; defaults to all four."""
    fs = [str(f).strip().lower() for f in (fields or [])]
    fs = [f for f in fs if f in _AI_FILL_FIELDS]
    return fs or list(_AI_FILL_FIELDS)


def _ai_fill_system(fields: list[str]) -> str:
    ids  = "; ".join(_ID_DESC[f] for f in fields)
    keys = ", ".join(f"'{f}'" for f in fields)
    return (
        "You are a product data extractor. Given a product title, description, and "
        "bullet points, extract ONLY the following identifier(s) if EXPLICITLY stated "
        f"in the text: {ids}. "
        "MPNs are often found in titles as model numbers (e.g. 'Model AC141FB02-M'). "
        "UPC/EAN/GTIN are numeric codes rarely stated in product text — only return them "
        "if explicitly present as a numeric string. "
        f"Return ONLY a raw JSON object with keys {keys}. "
        "Use null for any identifier not found. Never invent or guess values."
    )


def _ai_fill_one(item: dict, client: Any, fields: list[str]) -> dict:
    from services.ai_recheck import _is_anthropic, _extract_json
    title   = item.get("title") or ""
    data    = {}
    try:
        data = json.loads(item.get("data_json") or "{}")
    except Exception:
        pass
    desc    = data.get("description") or ""
    bullets = " ".join(data.get("bullet_points") or [])
    system  = _ai_fill_system(fields)

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
                system=system,
                messages=[{"role": "user", "content": user_msg}],
                temperature=0,
            )
            raw = resp.content[0].text if resp.content else "{}"
        else:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": system},
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


def _ai_fill_pipeline(run_id: int, client: Any, fields: list[str] | None = None) -> None:
    _clear_ai_fill_control(run_id)
    fields = _clean_ai_fill_fields(fields)
    ai_col = {"mpn": "ai_mpn", "upc": "ai_upc", "ean": "ai_ean", "gtin": "ai_gtin"}

    # Load items still needing one of the SELECTED id columns: the real column is
    # empty AND it hasn't been AI-filled yet. Field-based (not the item ai_fill_status
    # flag) so a later run for a DIFFERENT column re-processes the same items.
    cond = " OR ".join(f"({f} IS NULL AND {ai_col[f]} IS NULL)" for f in fields)

    def _write(statements: list[tuple[str, tuple]]) -> bool:
        """Run one or more writes in a single locked connection, retrying a few
        times on a transient SQLite lock. Returns False if it ultimately fails
        (the caller keeps going rather than letting the whole run die)."""
        for attempt in range(4):
            try:
                with database._LOCK, database._connect() as conn:
                    for sql, params in statements:
                        conn.execute(sql, params)
                return True
            except Exception as wexc:  # noqa: BLE001
                if attempt == 3:
                    log.warning("[ai_fill] write failed for run %s: %s", run_id, str(wexc)[:120])
                    return False
                time.sleep(0.4)
        return False

    # Wrap the whole pipeline: an unhandled exception here used to kill the daemon
    # thread silently, stranding ai_fill_status='running' forever (progress frozen,
    # UI spinner never completes). Now any crash flips the run to 'error: …'.
    try:
        with database._connect() as conn:
            rows = conn.execute(
                f"SELECT id, asin, title, data_json FROM brand_analytics_items "
                f"WHERE run_id=? AND ({cond})",
                (run_id,),
            ).fetchall()

        total = len(rows)
        if total == 0:
            _write([(
                "UPDATE brand_analytics_runs SET ai_fill_status='done', "
                "ai_fill_done=0, ai_fill_total=0, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (run_id,),
            )])
            return

        _write([(
            "UPDATE brand_analytics_runs SET ai_fill_status='running', "
            "ai_fill_done=0, ai_fill_total=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (total, run_id),
        )])

        done = 0
        for row in rows:
            if _check_ai_fill_control(run_id) == "stop":
                _clear_ai_fill_control(run_id)
                _write([(
                    "UPDATE brand_analytics_runs SET ai_fill_status='stopped', "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (run_id,),
                )])
                return

            item = dict(row)
            result = _ai_fill_one(item, client, fields)

            # Only update the SELECTED ai_* columns (leave the others untouched).
            set_parts = []
            vals: list = []
            for f in fields:
                set_parts.append(f"{ai_col[f]}=?")
                vals.append((result.get(f) or "").strip() or None)
            set_parts.append("ai_fill_status='done'")
            set_parts.append("updated_at=CURRENT_TIMESTAMP")

            # Item update + progress counter, one connection, retried on lock.
            done += 1
            _write([
                (f"UPDATE brand_analytics_items SET {', '.join(set_parts)} WHERE id=?",
                 (*vals, item["id"])),
                ("UPDATE brand_analytics_runs SET ai_fill_done=?, "
                 "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                 (done, run_id)),
            ])

        _write([(
            "UPDATE brand_analytics_runs SET ai_fill_status='done', "
            "updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (run_id,),
        )])
    except Exception as exc:  # noqa: BLE001
        log.exception("[ai_fill] pipeline crashed for run %s", run_id)
        try:
            with database._LOCK, database._connect() as conn:
                conn.execute(
                    "UPDATE brand_analytics_runs SET ai_fill_status=?, "
                    "updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (f"error: {str(exc)[:80]}", run_id),
                )
        except Exception:  # noqa: BLE001
            pass


def start_ai_fill(run_id: int, client: Any, fields: list[str] | None = None) -> None:
    """Launch AI Fill in a background daemon thread. `fields` = which ID columns to
    fill (mpn/upc/ean/gtin); defaults to all four."""
    t = threading.Thread(
        target=_ai_fill_pipeline,
        args=(run_id, client, fields),
        daemon=True,
    )
    t.start()
