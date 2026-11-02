"""
Keepa Product Finder — brand/manufacturer → complete ASIN list.

Keepa's /query (Product Finder) returns the full catalog slice SP-API can't reach
(e.g. Medline brand = ~31.5k ASINs vs SP-API's ~6k), but it caps at 10,000 ASINs
per query and `page` pagination does NOT go deeper. So `find_asins` recursively
partitions the search on `trackingSince` (a timestamp EVERY tracked product has —
sales rank can't be used because most medical SKUs are unranked) until each
partition is under the 10k cap, then merges + dedupes.

Token cost: ~10 + 1 per 100 ASINs per query. `find_asins` reports total tokens
spent and the remaining bucket balance. Requires KEEPA_API in the environment.
"""
from __future__ import annotations

import collections
import datetime as dt
import json
import logging
import os
import re
import time
from typing import Callable

import requests

from services import database
from services.cancellation import JobCancelled, cancellable_sleep

log = logging.getLogger(__name__)

_ENDPOINT      = "https://api.keepa.com"
_DOMAIN        = 1                 # amazon.com (US)
_MAX_PER_QUERY = 10000             # Keepa Product Finder hard cap per selection
_KEEPA_EPOCH   = 1293840000        # 2011-01-01 UTC — Keepa "minutes" origin


def is_configured() -> bool:
    return bool(os.getenv("KEEPA_API"))


def _session() -> requests.Session:
    s = requests.Session()
    s.params = {"key": os.getenv("KEEPA_API")}
    return s


def _now_keepa_min() -> int:
    return int((time.time() - _KEEPA_EPOCH) / 60)


def _term_variants(term: str) -> list[str]:
    """Lowercased Keepa selection variants for a brand/manufacturer term. Keepa matches
    these EXACTLY, so a missing trailing period ('medline industries, lp' vs '…lp.')
    silently loses thousands of products. We pass all common punctuation/suffix variants
    as an OR list ('is one of') so callers don't have to match Keepa's punctuation."""
    t = str(term).strip().lower()
    out = {t}
    out.add(t[:-1] if t.endswith(".") else t + ".")     # toggle trailing period
    if t.endswith(","):
        out.add(t.rstrip(", "))
    # corporate suffix with & without a period (lp / inc / llc / ltd / co / corp)
    m = re.search(r"(.*\b)(lp|inc|llc|ltd|co|corp)\.?\s*$", t)
    if m:
        stem, suf = m.group(1), m.group(2)
        out.add(f"{stem}{suf}"); out.add(f"{stem}{suf}.")
    return sorted(v for v in out if v)


def token_status() -> dict:
    """Current Keepa token bucket: {'tokens_left', 'refill_rate'}."""
    try:
        j = _session().get(f"{_ENDPOINT}/token", timeout=15).json()
        return {"tokens_left": j.get("tokensLeft"), "refill_rate": j.get("refillRate")}
    except Exception as exc:  # noqa: BLE001
        log.warning("[keepa] token status failed: %s", exc)
        return {"tokens_left": None, "refill_rate": None}


# ── Live product data — ported from Fordmed-Dev/asin-data (main.py) ──────────
# Keepa price-type array indices (verified against raw API responses).
_AMAZON_SELLER_ID = "ATVPDKIKX0DER"
_IDX_SALES_RANK, _IDX_RATING, _IDX_BB_SHIPPING, _IDX_COUNT_FBA, _IDX_COUNT_FBM = 3, 17, 18, 34, 35


def _kval(v):
    """Keepa sentinels: negatives (-1 no data / -2 not retrieved) → None."""
    if isinstance(v, (int, float)) and v < 0:
        return None
    return v


def _kcents(v):
    v = _kval(v)
    return round(v / 100, 2) if isinstance(v, (int, float)) else None


def _karr(stats: dict, key: str, idx: int):
    lst = stats.get(key)
    if isinstance(lst, list) and idx < len(lst):
        return _kval(lst[idx])
    return None


def _bb_window(stats: dict):
    """(top_seller_id, top_seller_pct, amazon_pct) from a window's buyBoxStats."""
    bb = stats.get("buyBoxStats") or {}
    if not bb:
        return None, None, None
    top = max(bb, key=lambda s: bb[s].get("percentageWon", 0) if isinstance(bb[s], dict) else 0,
              default=None)
    tp = bb[top].get("percentageWon") if top and isinstance(bb.get(top), dict) else None
    ap = bb[_AMAZON_SELLER_ID].get("percentageWon") if isinstance(bb.get(_AMAZON_SELLER_ID), dict) else None
    return top, tp, ap


# Per user request (2026-10-19): "out of 400 tokens (we upgraded just now) we
# leave like 5% of tokens left" -- user was seeing real 429s on Keepa's own
# dashboard. Confirmed live why: `need` below used to badly UNDERESTIMATE the
# real per-ASIN cost of a /product buybox+stats+fbaFees+rating call -- a
# 2-tokens/ASIN guess against a measured REAL cost of 6 tokens/ASIN (verified
# live, twice, both the stats=30 and stats=90 calls: 18 tokensConsumed for a
# 3-ASIN batch = 6.0/ASIN each time). For a 100-ASIN batch that's a real cost
# of 600 tokens against an estimated "need" of only 200 -- `_wait_tokens`
# would happily let the call through with, say, 250 tokens left (>= the
# wrong 200 estimate), the request would then actually consume 600, driving
# the real balance deeply NEGATIVE, which is exactly what trips Keepa's own
# 429 on the next request until the bucket refills back to positive.
# `need` below now reflects the measured real cost. `_RESERVE_PCT` is the
# explicit safety margin on top the user asked for -- computed as a
# percentage of the ACCOUNT'S LIVE `refillRate` (not a hardcoded "400"),
# so it keeps tracking correctly through any future plan upgrade/downgrade.
_RESERVE_PCT = 0.05


def _wait_tokens(sess: requests.Session, need: int = 20,
                  should_cancel: Callable[[], bool] | None = None) -> None:
    """`should_cancel`, if given, is checked before every poll and during the
    refill wait (which can otherwise block for minutes across this loop's up
    to 90 iterations) — see services.cancellation.JobCancelled. Added
    2026-10-20 after a real user report that cancelling a Vendor Offers
    export didn't actually stop it; this loop was one of two un-interruptible
    waits found (the other being SP-API's own retry backoff, services/spapi/
    catalog.py)."""
    for _ in range(90):
        if should_cancel and should_cancel():
            raise JobCancelled()
        try:
            j = sess.get(f"{_ENDPOINT}/token", timeout=10).json()
            left = int(j.get("tokensLeft", 0))
            refill_rate = int(j.get("refillRate") or 0)
            reserve = int(refill_rate * _RESERVE_PCT) if refill_rate else 0
            if left >= need + reserve:
                return
            cancellable_sleep(max(2, int((j.get("refillIn") or 3000) / 1000)), should_cancel)
        except JobCancelled:
            raise
        except Exception:  # noqa: BLE001
            cancellable_sleep(5, should_cancel)


# ASINs per /product call. We use `buybox=1` (Buy Box price + seller + share
# stats) instead of `offers=20`: offers ship every offer object (~50 KB/ASIN →
# 100/call made ~5 MB responses that broke mid-download / ChunkedEncodingError,
# and cost ~6 extra tokens/ASIN). buybox gives the same Buy-Box columns we need
# in a small (~5 KB/ASIN) payload, so 100/call is reliable and cheap.
_PRODUCT_BATCH = 100


def _call_product(sess: requests.Session, asins: list[str], stats_days: int,
                   should_cancel: Callable[[], bool] | None = None) -> dict:
    params = {"domain": _DOMAIN, "asin": ",".join(asins), "history": 0, "stats": stats_days,
              "stock": 1, "fbaFees": 1, "buybox": 1, "update": 72, "product": 1, "rating": 1}
    # NOTE (2026-10-07): without `rating=1`, Keepa never populates the RATING
    # (idx 16) / COUNT_REVIEWS (idx 17) stats slots at all -- they come back as
    # -1 ("no data") even for products that genuinely have thousands of
    # reviews (`hasReviews: true`), which is why "Total Ratings Count" was
    # silently 0 for every single row. Verified live: the exact same ASIN
    # returned current[17]=-1 without this param and current[17]=7414 with it.
    # Real measured cost (2026-10-19, live): 6 tokens/ASIN for this exact param
    # combination (buybox+stock+fbaFees+rating+stats), confirmed identically on
    # BOTH the stats=30 and stats=90 calls. The old `* 2` estimate here was a 3x
    # UNDERCOUNT -- it let `_wait_tokens` pass a call through with, say, 250
    # tokens left for a 100-ASIN batch (>= the wrong 200 estimate), the request
    # then actually consumed 600, driving the real balance deeply NEGATIVE --
    # which is exactly what trips Keepa's own 429 on the next request until the
    # bucket refills back to positive. This single fix is the real root cause
    # of the 429s the user was seeing on Keepa's own dashboard.
    need = max(20, len(asins) * 6)
    for attempt in range(12):
        if should_cancel and should_cancel():
            raise JobCancelled()
        _wait_tokens(sess, need, should_cancel=should_cancel)
        try:
            r = sess.get(f"{_ENDPOINT}/product", params=params, timeout=120)
            if r.status_code in (400, 401, 402, 403):
                raise RuntimeError(f"Keepa fatal {r.status_code}: {r.text[:200]}")
            if r.status_code != 200:
                cancellable_sleep(min(30, 3 * 2 ** attempt), should_cancel)
                continue
            j = r.json()
            if j.get("error"):
                err = str(j["error"])
                if "token" in err.lower():
                    # RECOVERABLE: the account is temporarily out of tokens. This
                    # used to raise immediately and kill the whole (potentially
                    # hours-long) export the moment the bucket ran dry near the
                    # end -- wait for the refill and retry instead, same pattern
                    # _query() already uses for Product Finder token errors.
                    log.warning("[keepa] token exhaustion on /product (attempt %d): %s",
                                attempt + 1, err[:150])
                    cancellable_sleep(min(60, 6 * (attempt + 1)), should_cancel)
                    continue
                raise RuntimeError(f"Keepa error: {err}")
            return j
        except requests.RequestException as exc:      # incl. ChunkedEncodingError (broken read)
            log.warning("[keepa] /product read failed (attempt %d): %s", attempt + 1, str(exc)[:100])
            cancellable_sleep(min(30, 3 * 2 ** attempt), should_cancel)
    raise RuntimeError("Keepa /product failed after retries (token exhaustion or repeated errors)")


def seller_asins(seller_id: str) -> dict:
    """Keepa /seller?storefront=1 -- this seller's full CURRENT ASIN roster plus
    basic identity info. Flat ~10 tokens regardless of catalog size (verified
    live) -- this is the Seller Analytics discovery step: unlike a vendor
    offer's UPC, which needs a barcode->ASIN lookup pass, a seller ID already
    IS the confirmed identity, so Keepa just hands back their ASIN list
    directly -- no recovery/resolution pass needed."""
    seller_id = (seller_id or "").strip()
    if not seller_id:
        return {"asins": [], "seller_name": None, "error": "No seller ID given."}
    if not is_configured():
        return {"asins": [], "seller_name": None, "error": "Keepa is not configured (KEEPA_API missing)."}
    sess = _session()
    _wait_tokens(sess, 10)
    try:
        r = sess.get(f"{_ENDPOINT}/seller",
                     params={"domain": _DOMAIN, "seller": seller_id, "storefront": 1},
                     timeout=60)
        j = r.json()
    except requests.RequestException as exc:
        return {"asins": [], "seller_name": None, "error": str(exc)[:200]}
    if j.get("error"):
        return {"asins": [], "seller_name": None, "error": str(j["error"])[:200]}
    sellers = j.get("sellers") or {}
    obj = sellers.get(seller_id) or next(iter(sellers.values()), None)
    if not obj:
        return {"asins": [], "seller_name": None,
                "error": f"Seller {seller_id!r} not found on Keepa."}
    asin_list = obj.get("asinList") or []
    return {
        "asins": sorted({a for a in asin_list if a}),
        "seller_name": obj.get("sellerName") or obj.get("businessName"),
        "total_storefront_asins": obj.get("totalStorefrontAsins"),
        "has_fba": obj.get("hasFBA"),
        "rating": _kval(obj.get("currentRating")),
        "rating_count": _kval(obj.get("currentRatingCount")),
        "error": None,
    }


def _call_sellers(sess: requests.Session, seller_ids) -> dict:
    names: dict = {}
    uniq = list({s for s in seller_ids if s})
    for i in range(0, len(uniq), 100):
        _wait_tokens(sess, 10)
        try:
            j = sess.get(f"{_ENDPOINT}/seller",
                         params={"domain": _DOMAIN, "seller": ",".join(uniq[i:i + 100])},
                         timeout=60).json()
            for sid, obj in (j.get("sellers") or {}).items():
                if isinstance(obj, dict):
                    names[sid] = obj.get("sellerName") or obj.get("name")
        except Exception:  # noqa: BLE001
            pass
    return names


def _flatten_product(p: dict, s30: dict, s90: dict) -> dict:
    ct = p.get("categoryTree")
    row = {
        "asin": p.get("asin"), "title": p.get("title"), "brand": p.get("brand"),
        "parent_asin": p.get("parentAsin"),
        "category": (ct[0] or {}).get("name") if isinstance(ct, list) and ct else None,
        "monthly_sold_quantity": _kval(p.get("monthlySold")),
        "package_qty": _kval(p.get("packageQuantity")),
        "fba_fees_usd": _kcents((p.get("fbaFees") or {}).get("pickAndPackFee")),
        # current Buy Box from buybox=1 (stats.buyBoxPrice), else price history [18]
        "current_buybox_usd": _kcents(s90.get("buyBoxPrice")) or _kcents(_karr(s90, "current", _IDX_BB_SHIPPING)),
        "buybox_30d": _kcents(_karr(s30, "avg30", _IDX_BB_SHIPPING)),
        "buybox_90d": _kcents(_karr(s90, "avg90", _IDX_BB_SHIPPING)),
        "offer_count_fba": _karr(s90, "current", _IDX_COUNT_FBA),
        "offer_count_fbm": _karr(s90, "current", _IDX_COUNT_FBM),
        "reviews_rating_count": _karr(s90, "current", _IDX_RATING),
        "sales_rank_current": _karr(s90, "current", _IDX_SALES_RANK),
        "sales_rank_30d": _karr(s30, "avg30", _IDX_SALES_RANK),
        "url": f"https://www.amazon.com/dp/{p.get('asin')}",
        # Only needed by Seller Analytics (no vendor-offer UPC to key off of
        # there) -- additive, existing callers never read these keys.
        "ean": next(iter(p.get("eanList") or []), None),
        "upc": next(iter(p.get("upcList") or []), None),
    }
    if isinstance(p.get("variationCSV"), list):
        row["variation_asin"] = ",".join(v for v in p["variationCSV"][1:] if isinstance(v, str))
    for src, dst in (("itemHeight", "height"), ("itemLength", "length"), ("itemWidth", "width")):
        v = _kval(p.get(src))
        row[dst] = (v / 10) if isinstance(v, (int, float)) else None      # Keepa mm → cm
    top30, tp30, ap30 = _bb_window(s30)
    top90, tp90, ap90 = _bb_window(s90)
    row["top_seller_30d_percentage"], row["top_seller_90d_percentage"] = tp30, tp90
    row["amazon_bb_30d_percentage"], row["amazon_bb_90d_percentage"] = ap30, ap90
    # BB seller = current Buy Box seller (buybox=1), else the dominant 90d winner
    row["buybox_seller_id"] = s90.get("buyBoxSellerId") or top90
    return row


_PRODUCT_CACHE_TTL_DAYS = 2   # per user request (2026-10-19): "keepa data for 2 days"


def _ensure_product_cache_table(conn) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS keepa_product_cache "
                 "(asin TEXT PRIMARY KEY, data_json TEXT, as_of TEXT)")


def _product_cache_get(asins) -> dict:
    """{asin: full result dict} for entries cached within the TTL. Honest tradeoff,
    not hidden: Buy Box price/rank/etc. can genuinely move within 2 days, so a
    cache hit may show slightly stale pricing -- the user explicitly chose this
    tradeoff (fewer live Keepa calls/tokens spent) over always-fresh data."""
    cutoff = (dt.datetime.now() - dt.timedelta(days=_PRODUCT_CACHE_TTL_DAYS)).isoformat()
    out: dict = {}
    clean = sorted({a.strip().upper() for a in asins if a})
    if not clean:
        return out
    with database._connect() as conn:
        _ensure_product_cache_table(conn)
        for i in range(0, len(clean), 400):
            ch = clean[i:i + 400]
            q = ",".join("?" * len(ch))
            for asin, dj in conn.execute(
                    f"SELECT asin, data_json FROM keepa_product_cache "
                    f"WHERE as_of >= ? AND asin IN ({q})", [cutoff, *ch]):
                try:
                    out[asin] = json.loads(dj)
                except (TypeError, ValueError):
                    pass
    return out


def _product_cache_put(rows: dict) -> None:
    if not rows:
        return
    now = dt.datetime.now().isoformat()
    with database._LOCK, database._connect() as conn:
        _ensure_product_cache_table(conn)
        conn.executemany(
            "INSERT INTO keepa_product_cache(asin, data_json, as_of) VALUES(?,?,?) "
            "ON CONFLICT(asin) DO UPDATE SET data_json=excluded.data_json, as_of=excluded.as_of",
            [(a, json.dumps(r), now) for a, r in rows.items()])


def fetch_products(asins, on_progress=None, should_cancel=None) -> dict:
    """Keepa /product for `asins` → {asin: {…}} with the same keys the Offer
    Analytics export reads (title, current/30d/90d buy box, fba fee, ranks, BB
    seller + share, Amazon %, FBA/FBM counts, brand, category, ratings, dims,
    parent/variation). Two LIVE calls per 100-ASIN batch (stats=30 and stats=90)
    so the 30d AND 90d buy-box-share percentages are both correct.

    Cached for 2 days (`_PRODUCT_CACHE_TTL_DAYS`, per user request 2026-10-19) —
    an ASIN already fetched recently skips the live call entirely (saves both
    time and Keepa tokens, the latter a real past failure mode for long exports
    — see this module's own `_call_product` token-exhaustion handling).

    `should_cancel`, if given, is checked between batches AND raises
    JobCancelled (propagating to the caller) rather than silently breaking the
    loop, so a caller like services/vendor_offers_template_export.py's
    build_template_xlsx (which runs this on its own thread alongside
    enrich_asins) can tell a genuine cancellation apart from "ran out of
    ASINs." It's also threaded into `_call_product`/`_wait_tokens` so a
    cancel mid-retry-backoff or mid-token-wait doesn't have to wait out the
    full sequence first — added 2026-10-20 after a real report that
    cancelling a Vendor Offers export didn't actually stop it."""
    if not is_configured():
        return {}
    sess = _session()
    uniq = sorted({a.strip().upper() for a in asins if a})
    out: dict = {}
    seller_ids: set = set()

    cached = _product_cache_get(uniq)
    out.update(cached)
    to_fetch = [a for a in uniq if a not in cached]

    total = len(uniq)
    done = len(uniq) - len(to_fetch)
    if on_progress:
        on_progress(done, total)

    fresh: dict = {}
    for i in range(0, len(to_fetch), _PRODUCT_BATCH):
        if should_cancel and should_cancel():
            raise JobCancelled()
        batch = to_fetch[i:i + _PRODUCT_BATCH]
        try:
            j30 = _call_product(sess, batch, 30, should_cancel=should_cancel)
            j90 = _call_product(sess, batch, 90, should_cancel=should_cancel)
        except JobCancelled:
            raise  # a cancelled run, not a failed batch -- let it propagate
        except Exception as exc:  # noqa: BLE001
            log.warning("[keepa] batch %d–%d permanently failed, skipping "
                        "(%d ASINs will have no Keepa data): %s",
                        i, i + len(batch), len(batch), str(exc)[:200])
            done += len(batch)
            if on_progress:
                on_progress(done, total)
            continue
        p30 = {p["asin"]: p for p in (j30.get("products") or []) if p.get("asin")}
        for p in (j90.get("products") or []):
            asin = p.get("asin")
            if not asin:
                continue
            row = _flatten_product(p, (p30.get(asin, {}).get("stats") or {}), (p.get("stats") or {}))
            key = asin.strip().upper()
            out[key] = row
            fresh[key] = row
            if row.get("buybox_seller_id"):
                seller_ids.add(row["buybox_seller_id"])
        done += len(batch)
        if on_progress:
            on_progress(done, total)
    names = _call_sellers(sess, seller_ids)
    for key, row in fresh.items():
        row["buybox_seller_name"] = names.get(row.get("buybox_seller_id"))
    _product_cache_put(fresh)
    return out


def _query(sess: requests.Session, selection: dict, per_page: int) -> tuple[list, int, int, int]:
    """One Product Finder call → (asinList, totalResults, tokensConsumed, tokensLeft)."""
    sel = dict(selection, perPage=per_page, page=0)
    for attempt in range(5):
        try:
            r = sess.post(f"{_ENDPOINT}/query", params={"domain": _DOMAIN},
                          data=json.dumps(sel), timeout=90)
            j = r.json()
            err = j.get("error")
            if err:
                # token exhaustion → wait for refill and retry; other errors are fatal
                if "token" in str(err).lower():
                    time.sleep(min(60, 6 * (attempt + 1)))
                    continue
                raise RuntimeError(f"Keepa query error: {err}")
            return (j.get("asinList") or [], int(j.get("totalResults") or 0),
                    int(j.get("tokensConsumed") or 0), j.get("tokensLeft"))
        except (requests.Timeout, requests.ConnectionError) as exc:
            log.warning("[keepa] query network error (%s), retry %d", exc, attempt + 1)
            time.sleep(3 * (attempt + 1))
    raise RuntimeError("Keepa query failed after retries")


def find_asins(brand: str | None = None, manufacturer: str | None = None,
               min_rank: int = 0, max_rank: int = 0,
               min_sold: int = 0, max_sold: int = 0, progress=None) -> dict:
    """All ASINs for a brand or manufacturer, partitioned past the 10k cap.

    `min_rank`/`max_rank` filter by sales rank DIRECTLY in the Keepa Product Finder
    (`current_SALES_gte`/`_lte`) — same query, NO extra tokens — so the ASIN list is
    trimmed to the wanted rank window before any SP-API enrichment (e.g. Medline max
    50k: 31,375 → 403). NOTE: a Keepa rank filter requires a rank, so it excludes
    UNRANKED products (matches the app's Min-BSR rule; for Max-BSR it also drops
    unranked — usually desired, as a max rank means "must be selling").

    Returns {'asins': [...], 'count': N, 'total_results': N, 'tokens_spent': N,
             'tokens_left': N}. `progress(found, target)` is called as it goes.
    """
    if not is_configured():
        raise RuntimeError("KEEPA_API not configured")
    sess = _session()
    base: dict = {}
    if brand and str(brand).strip():
        base["brand"] = _term_variants(brand)
    if manufacturer and str(manufacturer).strip():
        base["manufacturer"] = _term_variants(manufacturer)
    if not base:
        raise ValueError("find_asins requires a brand or manufacturer")
    # Sales-rank window, applied in-query (free). Keepa's current sales rank field.
    if min_rank and int(min_rank) > 0:
        base["current_SALES_gte"] = int(min_rank)
    if max_rank and int(max_rank) > 0:
        base["current_SALES_lte"] = int(max_rank)
    # Estimated units-sold-per-month window (Amazon "bought in past month"), in-query,
    # also free. Only products with a published figure carry it, so a min excludes the
    # rest (that's the intent: "at least N sold/mo").
    if min_sold and int(min_sold) > 0:
        base["monthlySold_gte"] = int(min_sold)
    if max_sold and int(max_sold) > 0:
        base["monthlySold_lte"] = int(max_sold)

    asins: set[str] = set()
    spent = [0]
    left = [None]
    target = [0]

    def _probe(sel: dict) -> int:
        al, tot, tok, lf = _query(sess, sel, 50)
        spent[0] += tok
        left[0] = lf
        return tot

    def _retrieve(sel: dict) -> None:
        al, tot, tok, lf = _query(sess, sel, _MAX_PER_QUERY)
        spent[0] += tok
        left[0] = lf
        asins.update(al)
        if progress:
            progress(len(asins), target[0])

    # get the true total once (cheap) so progress has a denominator
    target[0] = _probe(dict(base)) or 0

    def _collect(lo: int, hi: int | None) -> None:
        sel = dict(base)
        if lo > 0:
            sel["trackingSince_gte"] = lo
        if hi is not None:
            sel["trackingSince_lte"] = hi
        tot = _probe(sel)
        if tot == 0:
            return
        if tot <= _MAX_PER_QUERY:
            _retrieve(sel)
            return
        if hi is None:
            hi = _now_keepa_min()
        if lo >= hi:                      # can't split further — take what we can
            log.warning("[keepa] partition still >10k at trackingSince=%d; truncating", lo)
            _retrieve(sel)
            return
        mid = (lo + hi) // 2
        _collect(lo, mid)
        _collect(mid + 1, hi)

    _collect(0, None)
    return {
        "asins": sorted(asins),
        "count": len(asins),
        "total_results": target[0],
        "tokens_spent": spent[0],
        "tokens_left": left[0],
    }


def discover_entities(seed: str, sample_size: int = 200, max_values: int = 15) -> dict:
    """Discover the ACTUAL Keepa brand + manufacturer strings (and their exact product
    counts) for a seed like 'Medline' — so the user picks real values (incl. exact
    punctuation like 'Medline Industries, LP.') instead of guessing. Samples products'
    brand/manufacturer fields, then gets an exact finder count per distinct value.

    Returns {'entities': [{'type','value','total','sample_count'}], 'tokens_spent',
             'tokens_left'} sorted by total desc."""
    if not is_configured():
        raise RuntimeError("KEEPA_API not configured")
    sess = _session()
    spent = 0
    left = None
    s = str(seed).strip().lower()

    # light seed capture (one page) — brand first, then manufacturer
    al, _tot, tok, left = _query(sess, {"brand": [s]}, _MAX_PER_QUERY)
    spent += tok
    if not al:
        al, _tot, tok, left = _query(sess, {"manufacturer": [s]}, _MAX_PER_QUERY)
        spent += tok
    if not al:
        return {"entities": [], "tokens_spent": spent, "tokens_left": left}

    step = max(1, len(al) // sample_size)
    samp = al[::step][:sample_size]

    brands: collections.Counter = collections.Counter()
    manus: collections.Counter = collections.Counter()
    canon: dict[str, str] = {}   # lowercased → best display casing
    for i in range(0, len(samp), 100):
        r = sess.get(f"{_ENDPOINT}/product",
                     params={"domain": _DOMAIN, "asin": ",".join(samp[i:i + 100]),
                             "history": 0, "stats": 0}, timeout=90)
        j = r.json()
        spent += int(j.get("tokensConsumed") or 0)
        left = j.get("tokensLeft")
        for p in (j.get("products") or []):
            for kind, ctr in (("brand", brands), ("manufacturer", manus)):
                v = (p.get(kind) or "").strip()
                if v:
                    lv = v.lower()
                    ctr[lv] += 1
                    canon.setdefault(lv, v)

    # exact finder count per distinct value (cheap: perPage=50 ≈ 11 tokens each)
    entities = []
    seen: set[str] = set()
    for kind, ctr in (("brand", brands), ("manufacturer", manus)):
        for lv, scount in ctr.most_common(max_values):
            key = (kind, lv)
            if key in seen:
                continue
            seen.add(key)
            _al, total, tok, left = _query(sess, {kind: [lv]}, 50)
            spent += tok
            entities.append({"type": kind, "value": canon.get(lv, lv),
                             "total": total, "sample_count": scount})
    entities.sort(key=lambda e: e["total"], reverse=True)
    return {"entities": entities, "tokens_spent": spent, "tokens_left": left}


_DISCOVER_POOL = 3000   # ASINs pulled per finder query to sample from (bounds cost)
_JUNK_BRANDS = {"generic", "unknown", "artist unknown", "n/a", "na", "none", "null",
                "oem", "brand", "no brand", "unbranded", "generic brand", "-"}


def discover_sub_brands(seed: str, extra_terms: list[str] | None = None,
                        sample_size: int = 400, max_brands: int = 40) -> dict:
    """Find a manufacturer's/brand's REAL sub-brands from ACTUAL Keepa data.

    AI guessing misses house product lines (e.g. it never lists 'Flowflex' for ACON).
    But on Amazon those products carry manufacturer='ACON Laboratories', so we search
    products whose MANUFACTURER matches the seed (and its known longer forms — the AI's
    sub-brand guesses like 'ACON Laboratories'/'ACON Labs' ARE those forms) and collect
    the distinct BRAND field values. That surfaces Flowflex, ON/GO, On Call, Mission, …

    Returns {'brands': [{'value','count'}], 'tokens_spent', 'tokens_left'} — brand
    values that actually exist under the seed, each with its exact finder count, desc."""
    if not is_configured():
        raise RuntimeError("KEEPA_API not configured")
    sess = _session()
    spent = [0]
    left = [None]

    def _sample_brands(selection: dict) -> tuple[collections.Counter, set[str], int]:
        al, _tot, tok, lf = _query(sess, selection, min(_MAX_PER_QUERY, _DISCOVER_POOL))
        spent[0] += tok
        left[0] = lf
        if not al:
            return collections.Counter(), set(), 0
        step = max(1, len(al) // sample_size)
        samp = al[::step][:sample_size]
        bc: collections.Counter = collections.Counter()
        mfrs: set[str] = set()
        for i in range(0, len(samp), 100):
            r = sess.get(f"{_ENDPOINT}/product",
                         params={"domain": _DOMAIN, "asin": ",".join(samp[i:i + 100]),
                                 "history": 0, "stats": 0}, timeout=90)
            j = r.json()
            spent[0] += int(j.get("tokensConsumed") or 0)
            left[0] = j.get("tokensLeft")
            for p in (j.get("products") or []):
                b = (p.get("brand") or "").strip()
                if b:
                    bc[b] += 1
                m = (p.get("manufacturer") or "").strip()
                if m:
                    mfrs.add(m)
        return bc, mfrs, len(samp)

    tok_seed = re.sub(r"[^a-z0-9]", "", str(seed).lower())

    def _has_seed(s: str) -> bool:
        # the manufacturer link must actually mention the seed (so 'Mission' — a generic
        # AI guess that pulls in Mission Foods/Darts/Bauer — is NOT searched as a mfr).
        return bool(tok_seed) and tok_seed in re.sub(r"[^a-z0-9]", "", str(s).lower())

    # manufacturer search terms: the seed's variants + ONLY the seed-anchored longer
    # forms from extra_terms ('ACON Laboratories'/'ACON Labs', NOT 'Mission').
    terms: set[str] = set(_term_variants(seed))
    for t in (extra_terms or [])[:20]:
        if t and _has_seed(t):
            terms.update(_term_variants(t))
    terms = sorted(terms)[:40]

    brand_counts: collections.Counter = collections.Counter()
    total_sampled = 0
    # (1) products under the manufacturer terms → collect their brand values
    b_mfr, seen_mfrs, n1 = _sample_brands({"manufacturer": terms})
    brand_counts.update(b_mfr)
    total_sampled += n1
    # (2) products branded as the seed itself (so the parent brand is always offered)
    b_brand, seen_mfrs2, n2 = _sample_brands({"brand": sorted(_term_variants(seed))})
    brand_counts.update(b_brand)
    total_sampled += n2
    # (3) expand: any SEEN manufacturer string that mentions the seed but wasn't searched
    # yet (e.g. 'ACON Laboratories' discovered via Flowflex products) → search it too so
    # we catch the rest of the family. The seed-token guard keeps it from drifting.
    more = sorted({m.lower() for m in (seen_mfrs | seen_mfrs2)
                   if _has_seed(m) and m.lower() not in terms})[:20]
    if more:
        b_more, _, n3 = _sample_brands({"manufacturer": more})
        brand_counts.update(b_more)
        total_sampled += n3

    # Confidence floor BEFORE spending tokens on the exact-count lookup, not after.
    # Verified live (2026-10-02, "Safetec" manufacturer run): a handful of
    # products with an unrelated/incidental brand value (a shared kit listing,
    # a data-entry quirk, etc.) show up only 1-3 times out of 500+ samples —
    # true noise, not a real house sub-brand ("Safetec" itself was 507/526 =
    # 96%; every noise brand was <=3/526 = <=0.6%). The OLD code took every
    # such one-off hit and looked up its GLOBAL Amazon-wide popularity, which
    # rewards exactly the wrong thing: a brand that happens to be big
    # EVERYWHERE ELSE on Amazon (e.g. "BND" 696 products, "PrimeMed" 490,
    # unrelated office/safety suppliers) outranked the genuine seed brand in
    # the final sorted-by-count output, even though its connection to the
    # seed was a single coincidental sample. A real house sub-brand (e.g.
    # Acon's "Flowflex", 212/~400 samples) clears this easily; a one-off
    # mislabeled/bundled listing does not.
    min_hits = max(3, round(0.02 * total_sampled)) if total_sampled else 3
    candidates = [(val, cnt) for val, cnt in brand_counts.most_common(max_brands * 2)
                  if cnt >= min_hits]

    # exact finder count per distinct brand (cheap: perPage=50 ≈ 11 tokens each)
    out = []
    for val, _sc in candidates[:max_brands]:
        if val.strip().lower() in _JUNK_BRANDS:
            continue
        _al, total, tok, lf = _query(sess, {"brand": [val.lower()]}, 50)
        spent[0] += tok
        left[0] = lf
        if total > 0:
            out.append({"value": val, "count": total})
    out.sort(key=lambda e: e["count"], reverse=True)
    return {"brands": out, "tokens_spent": spent[0], "tokens_left": left[0]}
