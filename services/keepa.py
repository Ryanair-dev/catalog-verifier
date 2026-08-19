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
import json
import logging
import os
import re
import time

import requests

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
               progress=None) -> dict:
    """All ASINs for a brand or manufacturer, partitioned past the 10k cap.

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
