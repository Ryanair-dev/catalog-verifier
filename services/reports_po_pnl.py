"""
PO P&L dashboard -- the first "Reports" dashboard. Shows, per Purchase Order,
how much was bought, how much has sold, and how much is left, in both units
and dollars -- scoped to POs assigned (on the Inbound Shipments Monday
board's "Purchaser" people-column) to a given person, Avi Schoemann by
default.

Data source: Azure's own `analytics.fulfilled_po_to_order_cash_cycle` view --
NOT a live Keepa/SP-API pull. This view is a pre-built FIFO allocation engine
that has already solved the hard problem this dashboard needs: Amazon pools
FBA inventory by ASIN, not by PO/lot, so there's no native "which PO did this
unit ship from" signal anywhere in Amazon's own data -- this view already
does that allocation (oldest-unsold-PO-first) at the data-warehouse level,
and even carries FORWARD-LOOKING forecast rows (ship_date in the future,
cash_in_source='Forecast') for units that haven't sold yet.

Confirmed live (2026-10-19) against real PO 51563: `qty_received` is
REPLICATED across every allocation-slice row for a given (po_number,
product_id) pair -- it is a constant per group, never additive, so it must
be MAX()'d, never SUM()'d. `cash_out`/`allocated_qty`/`cash_in`/
`allocated_profit` each represent that ONE slice's own share and DO sum
correctly across a (po, product) group (verified: SUM(allocated_qty) across
every slice for one (po,product) pair equals its qty_received exactly).

"Sold" here means ACTUALLY already shipped (ship_date <= today) -- a row
whose ship_date is in the future is a PROJECTION, not a real sale yet, and
is deliberately excluded from "sold" (those units still count as "left",
since they're still physically on the shelf right now). "Left" = qty
received minus qty actually already shipped.

"Amazon" sold is approximated as customer='FBA' (confirmed live: the only
Amazon-labelled value in this view's `customer` column across 618k rows --
no separate 'FBM'/'Amazon' value exists). If FBM sales are a meaningful
share of this business's Amazon volume, they are not currently split out
from true FBA sales, or are missed from the Amazon figure entirely -- not
yet confirmed which, flagged rather than silently assumed.

`source_type` is restricted implicitly by requiring `po_number IS NOT NULL`:
confirmed live that of the view's 9 source_type values, only 'Purchase'
(440,465 rows) and 'Prepaid PO' (2,702 rows) ever carry a po_number at all --
every other type (Warehouse Adjustment, FBA Adjustment, Transfer, Opening,
No Supply, Adjustment, Add Product) has po_number always NULL. So this
filter alone already scopes correctly to real vendor POs without needing an
explicit source_type clause.

KNOWN GAP (see services/monday_eta.py `pos_for_purchaser`'s own docstring):
the Purchaser filter only sees POs on the CURRENT live Inbound Shipments
board. An older PO that's since been archived off that board won't be found
by purchaser, even if it still has real P&L data sitting in this view.
"""
from __future__ import annotations

import logging
import re
import threading
import time

from services import azure_sql, monday_eta

log = logging.getLogger(__name__)

_VIEW = "analytics.fulfilled_po_to_order_cash_cycle"

# Same shadow-child suffix grammar `CatalogIndex.children_for_main` matches
# (services/sellercloud/catalog_index.py _CHILD_SUFFIX_RE), applied in
# REVERSE: given a full SKU, strip this trailing pattern to recover its Main
# SKU root. Doing it this direction lets brand/ASIN enrichment build one
# {root: {asins}} map in a SINGLE pass over the whole catalog, instead of
# calling children_for_main() (itself an O(catalog size) scan) once per line
# item -- confirmed live this was the actual bottleneck: 32s for ~2,500 line
# items against a ~115k-row catalog (O(n*m)) dropped to under a second once
# rewritten as one pass (O(n)).
_ROOT_SUFFIX_RE = re.compile(r"(?:_QY\d+)?-FB[AM](?:TRB)?\d*$", re.IGNORECASE)


def _chunks(items, n: int = 600):
    items = sorted({x for x in items if x is not None})
    for i in range(0, len(items), n):
        yield items[i:i + n]


def _inlist_int(vals) -> str:
    return ",".join(str(int(v)) for v in vals)


def _f(v) -> float:
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _line_items(po_numbers: set[int] | None) -> list[dict]:
    """One row per (po_number, product_id) with bought/sold/left figures, in
    both units and dollars, plus an Amazon-only (customer='FBA') breakout.
    `po_numbers=None` pulls EVERY PO ever in the view -- always pass a scoped
    set in practice; this app has no reason to ever want the unscoped form."""
    if not azure_sql.is_configured():
        return []
    conn = azure_sql._connect()
    try:
        cur = conn.cursor()
        chunks = list(_chunks(po_numbers)) if po_numbers is not None else [None]
        out: list[dict] = []
        for chunk in chunks:
            where = "WHERE po_number IS NOT NULL"
            if chunk is not None:
                if not chunk:
                    continue
                where += f" AND po_number IN ({_inlist_int(chunk)})"
            cur.execute(f"""
                SELECT po_number, MAX(vendor) AS vendor, product_id,
                       MAX(qty_received) AS qty_received,
                       SUM(cash_out) AS bought_cost,
                       MIN(received_date) AS received_date,
                       SUM(CASE WHEN ship_date IS NOT NULL AND ship_date <= CAST(GETDATE() AS DATE)
                                THEN allocated_qty ELSE 0 END) AS sold_qty,
                       SUM(CASE WHEN ship_date IS NOT NULL AND ship_date <= CAST(GETDATE() AS DATE)
                                THEN cash_in ELSE 0 END) AS sold_revenue,
                       SUM(CASE WHEN ship_date IS NOT NULL AND ship_date <= CAST(GETDATE() AS DATE)
                                THEN allocated_profit ELSE 0 END) AS sold_profit,
                       SUM(CASE WHEN ship_date IS NOT NULL AND ship_date <= CAST(GETDATE() AS DATE)
                                 AND customer = 'FBA' THEN allocated_qty ELSE 0 END) AS sold_qty_amazon,
                       SUM(CASE WHEN ship_date IS NOT NULL AND ship_date <= CAST(GETDATE() AS DATE)
                                 AND customer = 'FBA' THEN cash_in ELSE 0 END) AS sold_revenue_amazon
                FROM {_VIEW}
                {where}
                GROUP BY po_number, product_id
            """)
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                d = dict(zip(cols, row))
                qty_received = int(d["qty_received"] or 0)
                sold_qty = int(d["sold_qty"] or 0)
                bought_cost = round(_f(d["bought_cost"]), 2)
                left_qty = max(0, qty_received - sold_qty)
                unit_cost = (bought_cost / qty_received) if qty_received else 0.0
                out.append({
                    "po_number": int(d["po_number"]),
                    "vendor": (d["vendor"] or "").strip(),
                    "product_id": (d["product_id"] or "").strip(),
                    "qty_received": qty_received,
                    "bought_cost": bought_cost,
                    "received_date": d["received_date"].date().isoformat() if d["received_date"] else None,
                    "sold_qty": sold_qty,
                    "sold_revenue": round(_f(d["sold_revenue"]), 2),
                    "sold_profit": round(_f(d["sold_profit"]), 2),
                    "sold_qty_amazon": int(d["sold_qty_amazon"] or 0),
                    "sold_revenue_amazon": round(_f(d["sold_revenue_amazon"]), 2),
                    "left_qty": left_qty,
                    "left_value": round(left_qty * unit_cost, 2),
                })
        return out
    finally:
        conn.close()


def _enrich_brand_asin(items: list[dict], company: str = "Ford Medical") -> None:
    """Mutates each item dict in place, adding `brand` and `asins` (a sorted
    list of every FBA/FBM child ASIN for that Main SKU)."""
    try:
        idx = azure_sql.catalog_index(company)
    except Exception:  # noqa: BLE001
        log.exception("[reports_po_pnl] catalog_index failed")
        for it in items:
            it.setdefault("brand", "")
            it.setdefault("asins", [])
        return
    main_to_asins: dict[str, set[str]] = {}
    for sku_u, row in idx.by_sku.items():
        root = _ROOT_SUFFIX_RE.sub("", sku_u)
        if root == sku_u:
            continue   # not a recognized shadow-child SKU at all
        asin = (row.get("ASIN") or "").strip()
        if asin:
            main_to_asins.setdefault(root, set()).add(asin)
    for it in items:
        key = it["product_id"].upper()
        row = idx.by_sku.get(key)
        it["brand"] = (row.get("BrandName") or "").strip() if row else ""
        it["asins"] = sorted(main_to_asins.get(key, ()))


# Short in-memory cache of the enriched (brand/ASIN-joined) line items per
# purchaser -- not because catalog_index() itself is slow (it's already a
# shared, hourly-refreshed Azure cache every other feature in this app also
# relies on), but so retyping a filter box doesn't re-pay the Monday lookup +
# cash-cycle query + enrich pass (a few seconds, not huge, but noticeable on
# every keystroke of a live dashboard) more than once every few minutes.
# 35 min since 2026-10-30: the 30-min background scheduler below is what keeps
# the default dashboard fresh; this only catches a missed/failed scheduled run
# (and covers any non-default purchaser key).
_CACHE_TTL = 2100.0
_cache_lock = threading.Lock()
_cache: dict[str, tuple[float, list[dict]]] = {}
# One build at a time per purchaser (2026-10-30): the page fires /po-pnl and
# /po-pnl/filters together, and on a cold cache both used to run the full
# Azure build in parallel. Also lets a stale cache be served instantly while a
# single background thread refreshes it (stale-while-revalidate).
_build_locks: dict[str, threading.Lock] = {}
_refreshing: set[str] = set()


def _build_lock(key: str) -> threading.Lock:
    with _cache_lock:
        return _build_locks.setdefault(key, threading.Lock())


def _build_unlocked(purchaser: str, key: str, wait_for_catalog: bool = True) -> list[dict]:
    """Caller must hold _build_lock(key). With wait_for_catalog=False and the
    shared Azure catalog not loaded yet (the first minutes after a restart),
    skips the brand/ASIN enrichment instead of blocking on that ~3-min cold
    pull, and caches the result as already-stale so the next request
    refreshes it in the background with brands filled in."""
    po_numbers = monday_eta.pos_for_purchaser(purchaser) if purchaser else None
    built_at = time.time()
    if po_numbers is not None and not po_numbers:
        lines: list[dict] = []
    else:
        lines = _line_items(po_numbers)
        if wait_for_catalog or azure_sql.is_warm():
            _enrich_brand_asin(lines)
        else:
            for it in lines:
                it.setdefault("brand", "")
                it.setdefault("asins", [])
            built_at = 0.0
    with _cache_lock:
        _cache[key] = (built_at, lines)
    return lines


def _build(purchaser: str, key: str) -> list[dict]:
    with _build_lock(key):
        return _build_unlocked(purchaser, key)


def _refresh_in_background(purchaser: str, key: str) -> None:
    with _cache_lock:
        if key in _refreshing:
            return
        _refreshing.add(key)

    def run():
        try:
            _build(purchaser, key)
        except Exception:  # noqa: BLE001
            log.exception("[reports_po_pnl] background refresh failed")
        finally:
            with _cache_lock:
                _refreshing.discard(key)
    threading.Thread(target=run, name=f"po-pnl-refresh-{key[:20]}", daemon=True).start()


def _enriched_lines_for(purchaser: str, force: bool = False) -> list[dict]:
    """Fresh (<5 min) cache -> returned as-is. Stale cache -> returned
    instantly while one background thread rebuilds it. No cache, or the UI's
    Refresh button (force) -> build now; a concurrent caller waits on the same
    build instead of starting its own."""
    key = (purchaser or "").strip().lower()
    with _cache_lock:
        hit = _cache.get(key)
    if hit and not force:
        if time.time() - hit[0] >= _CACHE_TTL:
            _refresh_in_background(purchaser, key)
        return hit[1]
    if force:
        return _build(purchaser, key)
    with _build_lock(key):               # a build already in flight? wait for it and reuse
        with _cache_lock:
            hit = _cache.get(key)
        if hit:
            return hit[1]
        lines = _build_unlocked(purchaser, key, wait_for_catalog=False)
    with _cache_lock:
        partial = _cache.get(key, (1, None))[0] == 0.0
    if partial:                          # brands skipped (catalog cold) -> fill them in
        _refresh_in_background(purchaser, key)
    return lines


# Dashboard scope (2026-10-30, per user: "lets leave only Avi's and Victoria's").
DEFAULT_PURCHASERS = "Avi Schoemann, Victoria Romanoff"

# Keep the default dashboard pre-built (2026-10-30, user: "maybe we should cache
# it every morning"): rebuilt in the background every 30 min from startup, so a
# visit never waits on a build and the numbers are never more than ~30 min old
# (a once-a-morning build would be a day stale by the afternoon).
REFRESH_SECONDS = 1800
_sched_thread: threading.Thread | None = None
_sched_stop = threading.Event()
_last_refresh: dict = {"at": None, "error": None, "pos": None}


def _sched_loop() -> None:
    key = DEFAULT_PURCHASERS.strip().lower()
    while not _sched_stop.is_set():
        try:
            lines = _build(DEFAULT_PURCHASERS, key)
            _last_refresh.update(at=time.time(), error=None, pos=len({l["po_number"] for l in lines}))
        except Exception as exc:  # noqa: BLE001
            _last_refresh["error"] = str(exc)[:300]
            log.exception("[reports_po_pnl] scheduled refresh failed")
        if _sched_stop.wait(REFRESH_SECONDS):
            return


def start_scheduler() -> None:
    global _sched_thread
    if _sched_thread and _sched_thread.is_alive():
        return
    _sched_stop.clear()
    _sched_thread = threading.Thread(target=_sched_loop, name="po-pnl-refresh", daemon=True)
    _sched_thread.start()


def stop_scheduler() -> None:
    _sched_stop.set()


def scheduler_status() -> dict:
    at = _last_refresh["at"]
    summary = (f"{_last_refresh['pos']} POs, last built {time.strftime('%H:%M', time.localtime(at))}"
               if at else "not built yet")
    if _last_refresh["error"]:
        summary += f" — last error: {_last_refresh['error'][:120]}"
    return {"running_now": False, "scheduler_alive": bool(_sched_thread and _sched_thread.is_alive()),
            "interval_seconds": REFRESH_SECONDS, "summary": summary,
            "next_run": (at + REFRESH_SECONDS) * 1000 if at else None}


_PO_SUM_FIELDS = (
    "qty_received", "bought_cost", "sold_qty", "sold_revenue", "sold_profit",
    "sold_qty_amazon", "sold_revenue_amazon", "left_qty", "left_value",
)


def _aggregate_to_pos(lines: list[dict]) -> list[dict]:
    by_po: dict[int, dict] = {}
    for l in lines:
        po = by_po.get(l["po_number"])
        if po is None:
            po = {"po_number": l["po_number"], "vendor": l["vendor"] or "",
                  "brands": set(), "received_date": None, "items": []}
            for k in _PO_SUM_FIELDS:
                po[k] = 0
            by_po[l["po_number"]] = po
        if not po["vendor"] and l["vendor"]:
            po["vendor"] = l["vendor"]
        if l.get("brand"):
            po["brands"].add(l["brand"])
        if l["received_date"] and (po["received_date"] is None or l["received_date"] < po["received_date"]):
            po["received_date"] = l["received_date"]
        po["items"].append(l)
        for k in _PO_SUM_FIELDS:
            po[k] += l[k]
    pos = list(by_po.values())
    for po in pos:
        po["brands"] = sorted(po["brands"])
        po["pct_sold"] = round(po["sold_qty"] / po["qty_received"], 4) if po["qty_received"] else 0.0
        po["bought_cost"] = round(po["bought_cost"], 2)
        po["sold_revenue"] = round(po["sold_revenue"], 2)
        po["sold_profit"] = round(po["sold_profit"], 2)
        po["sold_revenue_amazon"] = round(po["sold_revenue_amazon"], 2)
        po["left_value"] = round(po["left_value"], 2)
        po["fully_sold"] = po["left_qty"] <= 0 and po["qty_received"] > 0
    pos.sort(key=lambda p: p["received_date"] or "", reverse=True)
    return pos


def _empty_summary() -> dict:
    return {"po_count": 0, "bought_cost": 0.0, "sold_revenue": 0.0,
            "sold_profit": 0.0, "left_value": 0.0}


def _summarize(pos: list[dict]) -> dict:
    return {
        "po_count": len(pos),
        "bought_cost": round(sum(p["bought_cost"] for p in pos), 2),
        "sold_revenue": round(sum(p["sold_revenue"] for p in pos), 2),
        "sold_profit": round(sum(p["sold_profit"] for p in pos), 2),
        "left_value": round(sum(p["left_value"] for p in pos), 2),
    }


def get_po_pnl(purchaser: str = DEFAULT_PURCHASERS, vendor: str = "", po_number: str = "",
               brand: str = "", item: str = "", bucket: str = "active",
               refresh: bool = False, include_items: bool = True) -> dict:
    """Returns {"pos": [...], "summary": {...}}. `bucket` is 'active' (not
    yet fully sold through) or 'archive' (100% sold out), per the user's own
    rule: a PO that's completely sold stops being "live" and moves to an
    archive view instead of cluttering the working dashboard."""
    lines = _enriched_lines_for(purchaser, force=refresh)

    po_number = (po_number or "").strip()
    vendor_l, brand_l, item_l = vendor.strip().lower(), brand.strip().lower(), item.strip().lower()
    if po_number:
        try:
            wanted = int(po_number)
        except ValueError:
            wanted = None
        lines = [l for l in lines if l["po_number"] == wanted]
    if vendor_l:
        lines = [l for l in lines if vendor_l in l["vendor"].lower()]
    if brand_l:
        lines = [l for l in lines if brand_l in l.get("brand", "").lower()]
    if item_l:
        lines = [l for l in lines
                 if item_l in l["product_id"].lower() or any(item_l in a.lower() for a in l.get("asins", []))]

    pos = _aggregate_to_pos(lines)
    bucket = (bucket or "active").strip().lower()
    if bucket == "archive":
        pos = [p for p in pos if p["fully_sold"]]
    else:
        pos = [p for p in pos if not p["fully_sold"]]

    summary = _summarize(pos)
    if not include_items:
        # The dashboard list only needs PO-level totals; a PO's item lines are
        # fetched when its row is expanded (get_po_items) -- the full payload
        # was ~0.9 MB for Avi alone and the page built every hidden item table.
        pos = [{**{k: v for k, v in p.items() if k != "items"}, "item_count": len(p["items"])}
               for p in pos]
    return {"pos": pos, "summary": summary}


def get_po_items(purchaser: str = DEFAULT_PURCHASERS, po_number: str = "",
                 brand: str = "", item: str = "") -> list[dict]:
    """One PO's item lines, from the same cache (same brand/item filters as the list)."""
    try:
        wanted = int(str(po_number).strip())
    except ValueError:
        return []
    lines = [l for l in _enriched_lines_for(purchaser) if l["po_number"] == wanted]
    brand_l, item_l = brand.strip().lower(), item.strip().lower()
    if brand_l:
        lines = [l for l in lines if brand_l in l.get("brand", "").lower()]
    if item_l:
        lines = [l for l in lines
                 if item_l in l["product_id"].lower() or any(item_l in a.lower() for a in l.get("asins", []))]
    return lines


def get_filter_options(purchaser: str = DEFAULT_PURCHASERS) -> dict:
    """Distinct vendors/brands within this purchaser's PO scope, for
    populating the dashboard's filter dropdowns without a second round-trip
    per keystroke."""
    lines = _enriched_lines_for(purchaser)
    vendors = sorted({l["vendor"] for l in lines if l["vendor"]})
    brands = sorted({l["brand"] for l in lines if l.get("brand")})
    return {"vendors": vendors, "brands": brands}
