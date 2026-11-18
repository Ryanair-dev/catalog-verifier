"""
Competitor Analysis -- for one of OUR products (on Amazon already or not yet),
find every listing of the same item under a different brand and size up that
market: listings, brands, units sold, revenue, price per unit (avg / median /
lowest / highest), sales rank, who holds the Buy Box, FBA vs FBM.

Built 2026-11-03 (user: "our brand has started manufacturing 3 inch knives ...
analyze how all the other 3 inch knives are doing, market volume, avg cost per
knife, lowest cost, ranking sales, who is selling it, what brand"). Choices the
user made: one product OR a bulk list per run; exact-spec matches and close
variants kept separate (market totals use exact only); sources = SP-API +
Keepa (no SerpAPI).

Pipeline per product (see docs/features/competitor_analysis.md):
  1. plan      -- Claude turns the description (+ our ASIN's title if given)
                  into the spec, exact/close criteria, the unit being sold
                  ("knife"), SP-API search phrases and Keepa title terms.
  2. discover  -- SP-API keyword search (each phrase, up to SP_PAGES pages,
                  returns title + attributes) + Keepa Product Finder title
                  search (ranked products only, best sales rank first, capped).
  3. titles    -- Keepa-only ASINs have no title yet: one light Keepa /product
                  call (no stats/history, ~1 token/ASIN).
  4. classify  -- Claude (tool-use JSON) labels every candidate exact / close /
                  not, with the number of units in the listing (a 2-knife set
                  = 2) so price per unit is comparable.
  5. enrich    -- keepa.fetch_products (2-day cache) for exact + close only --
                  the expensive call (~12 tokens/ASIN) never runs on noise.
  6. metrics   -- units/month = Amazon's "bought in past month" (Keepa
                  monthlySold, a lower bound like "1K+"), else estimated from
                  sales rank + reviews with a power law fitted on this run's
                  own listings (approach ported from Jordan's
                  AmazonAsinResearch1 revenue_estimator); revenue = Buy Box x
                  units; market + brand + seller roll-ups.
"""
from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import io
import logging
import math
import re
import statistics
import time
from typing import Callable

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from services import keepa
from services.cancellation import JobCancelled

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
SP_PAGES = 10                 # x20 results per SP-API search phrase
KEEPA_BROAD_CAP = 800         # best-ranked ASINs taken from a broad title term
KEEPA_NARROW_CAP = 500        # ... from a spec-narrowed title term
MAX_CANDIDATES = 2500         # hard cap per product before AI classification
CLASSIFY_BATCH = 40
CLASSIFY_WORKERS = 6
MIN_FIT_POINTS = 20           # listings with real units + rank needed to fit the estimate

Progress = Callable[[int, int, str], None]


# ── Claude helpers ────────────────────────────────────────────────────────────

def _client():
    import os
    import anthropic
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured -- Competitor Analysis needs Claude.")
    return anthropic.Anthropic(api_key=key, max_retries=4)


def _tool_call(client, tool: dict, prompt: str, max_tokens: int = 4000) -> dict:
    resp = client.messages.create(
        model=MODEL, max_tokens=max_tokens, tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in resp.content:
        if getattr(block, "type", "") == "tool_use":
            return block.input or {}
    raise RuntimeError("Claude returned no structured result")


_PLAN_TOOL = {
    "name": "product_plan",
    "description": "Search plan for finding every competing listing of this product on Amazon.",
    "input_schema": {
        "type": "object",
        "properties": {
            "product_type": {"type": "string", "description": "Generic product type, no brand, e.g. 'paring knife'."},
            "spec_summary": {"type": "string", "description": "One line: the defining spec, e.g. '3-inch blade paring knife, single knife'."},
            "unit_name": {"type": "string", "description": "What one unit is, singular, e.g. 'knife'."},
            "exact_criteria": {"type": "array", "items": {"type": "string"},
                               "description": "What a listing must match to be the SAME item (spec, type, tolerances)."},
            "close_criteria": {"type": "array", "items": {"type": "string"},
                               "description": "What makes a listing a CLOSE variant / substitute (not exact)."},
            "sp_api_phrases": {"type": "array", "items": {"type": "string"},
                               "description": "4-8 Amazon search phrases a shopper would type, no brands, covering spec spellings (3 inch / 3\" / 3-inch) and synonyms."},
            "keepa_title_terms": {"type": "array", "items": {"type": "string"},
                                  "description": "2-5 title word-sets; a listing matches when its title contains ALL the words. First = broad product type (e.g. 'paring knife'), then spec-narrowed ones (e.g. 'paring knife 3 inch')."},
        },
        "required": ["product_type", "spec_summary", "unit_name", "exact_criteria", "close_criteria",
                     "sp_api_phrases", "keepa_title_terms"],
    },
}

_CLASSIFY_TOOL = {
    "name": "classify_listings",
    "description": "Verdict for every Amazon listing given.",
    "input_schema": {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "asin": {"type": "string"},
                        "verdict": {"type": "string", "enum": ["exact", "close", "not"]},
                        "units_in_listing": {"type": "integer",
                                             "description": "How many units of the product one purchase contains (a 2-pack = 2, a 3-piece set of these knives = 3). 1 if unclear."},
                        "spec_seen": {"type": "string", "description": "The spec read from the listing, e.g. '3.5 in blade, 2-pack'. Short."},
                        "reason": {"type": "string", "description": "Max 15 words."},
                    },
                    "required": ["asin", "verdict", "units_in_listing", "spec_seen", "reason"],
                },
            }
        },
        "required": ["results"],
    },
}


def plan_product(client, description: str, our_title: str | None = None, extra_terms: list[str] | None = None) -> dict:
    prompt = (
        "We are a brand launching (or already selling) the product below. We need to find EVERY competing "
        "Amazon listing of the same item from other brands, plus close substitutes.\n\n"
        f"Our product description: {description}\n"
        + (f"Our Amazon listing title: {our_title}\n" if our_title else "")
        + (f"User-supplied extra search phrases (include them): {', '.join(extra_terms)}\n" if extra_terms else "")
        + "\nRules: exact = same product type AND same defining spec (allow small tolerances that a shopper "
          "would treat as the same, e.g. 3 in vs 3.0 in vs 3-1/8 in); pack size / number of pieces may differ "
          "(we normalize price per unit) -- but a mixed set of different products is NOT exact. close = same "
          "use and type but a different spec (e.g. 2.5-4 in blade), or a set that contains the item. "
          "Search phrases must not contain brand names."
    )
    plan = _tool_call(client, _PLAN_TOOL, prompt, max_tokens=1500)
    if extra_terms:
        plan["sp_api_phrases"] = list(dict.fromkeys([*plan.get("sp_api_phrases", []), *extra_terms]))
    return plan


def _verify_exact_batch(client, plan: dict, rows: list[dict], median_ppu: float | None) -> list[dict]:
    """Second, stricter pass over EXACT rows only -- now with the price -- so the
    market numbers (built from exact rows) aren't polluted by a 3.74" knife
    called 3" or a single knife misread as a 24-pack (both seen on the first
    live run, 2026-11-03)."""
    lines = [f"ASIN {r['asin']} | title: {r['title']} | Buy Box ${r['price']} | first-pass units: "
             f"{r['units_in_listing']} | spec seen: {r.get('spec_seen')}" for r in rows]
    prompt = (
        f"Our product: {plan.get('spec_summary')} (one unit = one {plan.get('unit_name') or 'unit'}).\n"
        "EXACT requires ALL of: " + "; ".join(plan.get("exact_criteria") or []) + "\n"
        "Judge ONLY against the criteria above -- do not add requirements of your own (edge type, color, "
        "handle, material, ...). Tolerance ranges are INCLUSIVE: a value on the boundary is inside. Downgrade "
        "to 'close' only when the stated spec is clearly outside the range or the type differs; when in doubt "
        "keep 'exact'. Re-check units_in_listing using "
        "the title AND the price: a listing whose price per unit would be implausible for this product "
        + (f"(typical price per unit is around ${median_ppu:.2f}) " if median_ppu else "")
        + "is usually a single item, not a multi-pack -- unless the title clearly says it is a bulk pack. "
          "Return one result per ASIN (verdict exact / close / not).\n\n" + "\n".join(lines)
    )
    out = _tool_call(client, _CLASSIFY_TOOL, prompt, max_tokens=200 + 90 * len(rows)).get("results") or []
    return [r for r in out if isinstance(r, dict) and r.get("asin")]


def _safe(fn):
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        log.warning("[competitor] AI batch failed: %s", str(exc)[:200])
        return None


def _classify_batch(client, plan: dict, batch: list[dict]) -> list[dict]:
    lines = []
    for c in batch:
        bits = [f"ASIN {c['asin']}", f"title: {c.get('title') or '?'}"]
        if c.get("brand"):
            bits.append(f"brand: {c['brand']}")
        if c.get("attrs"):
            bits.append(f"attributes: {c['attrs']}")
        if c.get("category"):
            bits.append(f"category: {c['category']}")
        lines.append(" | ".join(bits))
    prompt = (
        f"Our product: {plan.get('spec_summary')} (type: {plan.get('product_type')}; one unit = one "
        f"{plan.get('unit_name') or 'unit'}).\n"
        "EXACT (same item, other brand) when: " + "; ".join(plan.get("exact_criteria") or []) + "\n"
        "CLOSE (variant / substitute) when: " + "; ".join(plan.get("close_criteria") or []) + "\n"
        "Otherwise NOT (different product, accessory, mixed set of different items, wrong type).\n"
        "Judge from the title and attributes. If the spec is not stated at all, use 'close' only when the "
        "type matches, never 'exact'. Return one result per ASIN.\n\n" + "\n".join(lines)
    )
    out = _tool_call(client, _CLASSIFY_TOOL, prompt, max_tokens=200 + 90 * len(batch)).get("results") or []
    return [r for r in out if isinstance(r, dict) and r.get("asin")]


# ── Discovery ────────────────────────────────────────────────────────────────

_ATTR_KEYS = ("size", "style", "blade", "blade_length", "blade_material", "item_length", "number_of_items",
              "unit_count", "item_package_quantity", "item_type_keyword", "material", "color")


def _attr_text(attrs: dict) -> str:
    parts = []
    for k in _ATTR_KEYS:
        v = attrs.get(k)
        if not v or not isinstance(v, list):
            continue
        first = v[0] if isinstance(v[0], dict) else {"value": v[0]}
        val = first.get("value")
        if isinstance(val, dict):
            val = val.get("value")
        if val is None and isinstance(first.get("length"), dict):
            val = f"{first['length'].get('value')} {first['length'].get('unit', '')}"
        unit = first.get("unit") or ""
        if val is not None and str(val).strip():
            parts.append(f"{k}={val}{(' ' + unit) if unit else ''}")
    return "; ".join(parts)[:300]


def _sp_search(phrases: list[str], should_cancel=None) -> dict[str, dict]:
    from services.spapi.client import get_catalog_api
    api = get_catalog_api("US", "default")
    found: dict[str, dict] = {}
    for phrase in phrases:
        token = None
        for _ in range(SP_PAGES):
            if should_cancel and should_cancel():
                raise JobCancelled()
            try:
                d = api.search_by_keywords(phrase, page_token=token,
                                           included_data="summaries,attributes,salesRanks")
            except Exception as exc:  # noqa: BLE001
                log.warning("[competitor] SP-API search %r failed: %s", phrase, str(exc)[:150])
                break
            for it in d.get("items") or []:
                asin = (it.get("asin") or "").upper()
                if not asin or asin in found:
                    continue
                s = (it.get("summaries") or [{}])[0]
                found[asin] = {
                    "asin": asin, "title": s.get("itemName"), "brand": s.get("brand"),
                    "category": (s.get("browseClassification") or {}).get("displayName"),
                    "attrs": _attr_text(it.get("attributes") or {}), "source": "Amazon search",
                }
            token = (d.get("pagination") or {}).get("nextToken")
            if not token:
                break
    return found


def _keepa_finder(terms: list[str]) -> tuple[list[str], int]:
    sess = keepa._session()
    asins: list[str] = []
    spent = 0
    for i, term in enumerate(terms):
        cap = KEEPA_BROAD_CAP if i == 0 else KEEPA_NARROW_CAP
        sel = {"title": term, "current_SALES_gte": 1, "sort": [["current_SALES", "asc"]]}
        try:
            al, _tot, used, _left = keepa._query(sess, sel, max(50, cap))
        except Exception as exc:  # noqa: BLE001
            log.warning("[competitor] Keepa finder %r failed: %s", term, str(exc)[:150])
            continue
        spent += used
        asins.extend(a.upper() for a in al[:cap])
    return list(dict.fromkeys(asins)), spent


def _keepa_titles(asins: list[str], should_cancel=None) -> tuple[dict[str, dict], int]:
    """Light /product (no stats/history) -> title/brand/pack for classification."""
    out: dict[str, dict] = {}
    spent = 0
    if not asins:
        return out, spent
    sess = keepa._session()
    for i in range(0, len(asins), 100):
        if should_cancel and should_cancel():
            raise JobCancelled()
        batch = asins[i:i + 100]
        keepa._wait_tokens(sess, len(batch) + 5, should_cancel=should_cancel)
        try:
            j = sess.get(f"{keepa._ENDPOINT}/product",
                         params={"domain": keepa._DOMAIN, "asin": ",".join(batch), "history": 0},
                         timeout=120).json()
        except Exception as exc:  # noqa: BLE001
            log.warning("[competitor] Keepa title batch failed: %s", str(exc)[:150])
            continue
        spent += int(j.get("tokensConsumed") or 0)
        for p in j.get("products") or []:
            asin = (p.get("asin") or "").upper()
            if not asin:
                continue
            bits = []
            for k, label in (("size", "size"), ("packageQuantity", "package_qty"), ("numberOfItems", "number_of_items"),
                             ("color", "color"), ("model", "model")):
                v = p.get(k)
                if v not in (None, "", -1, 0):
                    bits.append(f"{label}={v}")
            feats = p.get("features") or []
            if feats:
                bits.append("bullet: " + str(feats[0])[:140])
            ct = p.get("categoryTree") or []
            out[asin] = {"asin": asin, "title": p.get("title"), "brand": p.get("brand"),
                         "category": (ct[-1] or {}).get("name") if ct else None,
                         "attrs": "; ".join(bits)[:300], "source": "Keepa finder"}
    return out, spent


# ── Metrics ──────────────────────────────────────────────────────────────────

def _solve(a: list[list[float]], b: list[float]) -> list[float] | None:
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        p = max(range(c, n), key=lambda r: abs(m[r][c]))
        if abs(m[p][c]) < 1e-12:
            return None
        m[c], m[p] = m[p], m[c]
        for r in range(n):
            if r != c:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit_units_model(rows: list[dict]) -> dict | None:
    """units ~ a * rank^b * (reviews+1)^c, OLS in log space (rank-only fallback)."""
    pts = [(math.log(r["sales_rank"]), math.log((r.get("reviews") or 0) + 1), math.log(r["units_reported"]))
           for r in rows if r.get("units_reported") and r.get("sales_rank")]
    if len(pts) < MIN_FIT_POINTS:
        return None
    for use_reviews in (True, False):
        xs = [(1.0, lr, lv) if use_reviews else (1.0, lr) for lr, lv, _ in pts]
        ys = [ly for *_, ly in pts]
        k = len(xs[0])
        ata = [[sum(x[i] * x[j] for x in xs) for j in range(k)] for i in range(k)]
        aty = [sum(x[i] * y for x, y in zip(xs, ys)) for i in range(k)]
        sol = _solve(ata, aty)
        if sol and (sol[1] < 0):      # sales must fall as rank number grows
            pred = [sum(c * xv for c, xv in zip(sol, x)) for x in xs]
            mean = sum(ys) / len(ys)
            ss_tot = sum((y - mean) ** 2 for y in ys) or 1e-9
            r2 = 1 - sum((y - p) ** 2 for y, p in zip(ys, pred)) / ss_tot
            return {"coef": sol + ([0.0] if not use_reviews else []), "r2": round(r2, 3), "points": len(pts)}
    return None


def _estimate_units(model: dict, rank, reviews) -> int | None:
    if not model or not rank:
        return None
    a, b, c = model["coef"]
    return max(0, int(round(math.exp(a + b * math.log(rank) + c * math.log((reviews or 0) + 1)))))


def _row_from_keepa(cand: dict, verdict: dict, k: dict, own: bool) -> dict:
    price = k.get("current_buybox_usd") or k.get("buybox_30d") or k.get("buybox_90d")
    units_in = max(1, int(verdict.get("units_in_listing") or 1))
    seller = k.get("buybox_seller_name") or k.get("buybox_seller_id")
    if k.get("buybox_seller_id") == keepa._AMAZON_SELLER_ID:
        seller = "Amazon"
    return {
        "asin": cand["asin"], "match": "Ours" if own else verdict.get("verdict", "").capitalize(),
        "brand": (k.get("brand") or cand.get("brand") or "").strip() or "(no brand)",
        "title": k.get("title") or cand.get("title"),
        "price": price, "units_in_listing": units_in,
        "price_per_unit": round(price / units_in, 2) if price else None,
        "units_reported": k.get("monthly_sold_quantity"),
        "sales_rank": k.get("sales_rank_current") or k.get("sales_rank_30d"),
        "category": k.get("category") or cand.get("category"),
        "reviews": k.get("reviews_rating_count"), "rating": k.get("rating"),
        "bb_seller": seller, "amazon_bb_pct": k.get("amazon_bb_90d_percentage"),
        "fba_offers": k.get("offer_count_fba"), "fbm_offers": k.get("offer_count_fbm"),
        "spec_seen": verdict.get("spec_seen"), "reason": verdict.get("reason"),
        "found_via": cand.get("source"), "url": f"https://www.amazon.com/dp/{cand['asin']}",
        "family": k.get("parent_asin") or cand["asin"], "note": "",
    }


def _apply_units(rows: list[dict], model: dict | None) -> None:
    """Units/month per row. Variations share their parent's sales rank, so an
    estimate per child counts the same sales several times (44 of 196 estimated
    exact rows shared a rank on the first live run). Per variation family:
    reported children keep Amazon's figure and unreported siblings get none;
    a family with nothing reported gets ONE estimate split across members
    (+5% per extra member, Jordan's variation bonus)."""
    fams: dict[str, list[dict]] = {}
    for r in rows:
        fams.setdefault(r.get("family") or r["asin"], []).append(r)
    for fam in fams.values():
        reported = [r for r in fam if r.get("units_reported")]
        for r in reported:
            r["units"], r["units_source"] = r["units_reported"], "Amazon (bought past month)"
        rest = [r for r in fam if not r.get("units_reported")]
        if not rest:
            continue
        if reported:
            for r in rest:
                r["units"], r["units_source"] = None, "Variation (sales on sibling)"
            continue
        ranked = [r for r in rest if r.get("sales_rank")]
        base = _estimate_units(model, min(r["sales_rank"] for r in ranked),
                               max((r.get("reviews") or 0) for r in ranked)) if ranked else None
        n = len(rest)
        for r in rest:
            if base is None:
                r["units"], r["units_source"] = None, "No data"
            else:
                r["units"] = int(round(base * (1 + 0.05 * (n - 1)) / n))
                r["units_source"] = "Estimated" if n == 1 else f"Estimated (split over {n} variations)"
    for r in rows:
        r["revenue"] = round(r["price"] * r["units"], 2) if r.get("price") and r.get("units") else None


def _market(rows: list[dict]) -> dict:
    ppu = [r["price_per_unit"] for r in rows if r.get("price_per_unit")]
    units = sum(r.get("units") or 0 for r in rows)
    rev = sum(r.get("revenue") or 0 for r in rows)
    cheapest = min((r for r in rows if r.get("price_per_unit")), key=lambda r: r["price_per_unit"], default=None)
    weighted = (sum(r["price_per_unit"] * r["units"] for r in rows if r.get("price_per_unit") and r.get("units"))
                / max(1, sum(r["units"] for r in rows if r.get("price_per_unit") and r.get("units")))) if units else None
    return {
        "listings": len(rows),
        "brands": len({r["brand"].lower() for r in rows}),
        "units_month": units, "revenue_month": round(rev, 2),
        "units_estimated_share": round(sum(r.get("units") or 0 for r in rows if str(r.get("units_source") or "").startswith("Estimated"))
                                       / units, 3) if units else None,
        "avg_ppu": round(statistics.mean(ppu), 2) if ppu else None,
        "median_ppu": round(statistics.median(ppu), 2) if ppu else None,
        "weighted_ppu": round(weighted, 2) if weighted else None,
        "min_ppu": cheapest["price_per_unit"] if cheapest else None,
        "min_ppu_brand": cheapest["brand"] if cheapest else None,
        "min_ppu_asin": cheapest["asin"] if cheapest else None,
        "max_ppu": max(ppu) if ppu else None,
        "best_rank": min((r["sales_rank"] for r in rows if r.get("sales_rank")), default=None),
        "amazon_bb_listings": sum(1 for r in rows if r.get("bb_seller") == "Amazon"),
        "fba_listings": sum(1 for r in rows if (r.get("fba_offers") or 0) > 0),
    }


def _rollup(rows: list[dict], key: str) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(r.get(key) or "(unknown)", []).append(r)
    total_units = sum(r.get("units") or 0 for r in rows) or 1
    out = []
    for name, rs in groups.items():
        ppu = [r["price_per_unit"] for r in rs if r.get("price_per_unit")]
        units = sum(r.get("units") or 0 for r in rs)
        out.append({
            "name": name, "listings": len(rs), "units": units,
            "revenue": round(sum(r.get("revenue") or 0 for r in rs), 2),
            "share": round(units / total_units, 4),
            "avg_ppu": round(statistics.mean(ppu), 2) if ppu else None,
            "min_ppu": min(ppu) if ppu else None,
            "best_rank": min((r["sales_rank"] for r in rs if r.get("sales_rank")), default=None),
        })
    return sorted(out, key=lambda x: (-x["revenue"], -x["units"]))


# ── One product ──────────────────────────────────────────────────────────────

def analyze_product(description: str, our_asin: str | None = None, our_price: float | None = None,
                    extra_terms: list[str] | None = None, progress: Progress | None = None,
                    should_cancel=None) -> dict:
    prog = progress or (lambda d, t, p: None)
    client = _client()
    our_asin = (our_asin or "").strip().upper() or None
    our_title = None
    if our_asin:
        try:
            from services.spapi.client import get_catalog_api
            item = get_catalog_api("US", "default").get_by_asin(our_asin, included_data="summaries")
            our_title = ((item.get("summaries") or [{}])[0]).get("itemName")
        except Exception as exc:  # noqa: BLE001
            log.warning("[competitor] our ASIN %s lookup failed: %s", our_asin, str(exc)[:120])
    description = (description or "").strip() or (our_title or "")
    if not description:
        raise ValueError("Give a product description or our ASIN.")

    prog(0, 0, "working out the spec and search terms")
    plan = plan_product(client, description, our_title, extra_terms)

    prog(0, 0, "searching Amazon")
    cands = _sp_search(plan.get("sp_api_phrases") or [], should_cancel)
    n_sp = len(cands)
    prog(0, 0, "searching Keepa")
    k_asins, tokens = _keepa_finder(plan.get("keepa_title_terms") or [])
    new = [a for a in k_asins if a not in cands][: max(0, MAX_CANDIDATES - len(cands))]
    prog(0, len(new), "reading Keepa titles")
    titles, t2 = _keepa_titles(new, should_cancel)
    tokens += t2
    cands.update(titles)
    if our_asin:
        cands.pop(our_asin, None)
    cand_list = [c for c in cands.values() if c.get("title")]

    # 4. classify
    batches = [cand_list[i:i + CLASSIFY_BATCH] for i in range(0, len(cand_list), CLASSIFY_BATCH)]
    verdicts: dict[str, dict] = {}
    done = 0
    prog(0, len(cand_list), "AI matching listings")
    with cf.ThreadPoolExecutor(CLASSIFY_WORKERS) as ex:
        futs = {ex.submit(_classify_batch, client, plan, b): b for b in batches}
        for f in cf.as_completed(futs):
            if should_cancel and should_cancel():
                for g in futs:
                    g.cancel()
                raise JobCancelled()
            try:
                for r in f.result():
                    verdicts[r["asin"].upper()] = r
            except Exception as exc:  # noqa: BLE001
                log.warning("[competitor] classify batch failed: %s", str(exc)[:200])
            done += len(futs[f])
            prog(done, len(cand_list), "AI matching listings")

    keep = [c for c in cand_list if verdicts.get(c["asin"], {}).get("verdict") in ("exact", "close")]
    keep_ids = {c["asin"] for c in keep}
    excluded = [{**c, "reason": verdicts.get(c["asin"], {}).get("reason") or "not classified",
                 "spec_seen": verdicts.get(c["asin"], {}).get("spec_seen")}
                for c in cand_list if c["asin"] not in keep_ids]

    # 5. enrich
    want = [c["asin"] for c in keep] + ([our_asin] if our_asin else [])
    kdata = keepa.fetch_products(want, on_progress=lambda d, t: prog(d, t, "Keepa sales + price data"),
                                 should_cancel=should_cancel)
    rows = []
    for c in keep:
        k = kdata.get(c["asin"])
        if k:
            rows.append(_row_from_keepa(c, verdicts[c["asin"]], k, own=False))
    own_row = None
    if our_asin and kdata.get(our_asin):
        own_row = _row_from_keepa({"asin": our_asin, "title": our_title, "source": "our ASIN"},
                                  {"units_in_listing": 1, "verdict": "ours"}, kdata[our_asin], own=True)

    # 5b. stricter second pass on exact rows (with price) -> demotions + pack fixes
    exact_rows = [r for r in rows if r["match"] == "Exact"]
    ppus = [r["price"] for r in exact_rows if r.get("price") and r["units_in_listing"] == 1]
    med = statistics.median(ppus) if ppus else None
    by_asin = {r["asin"]: r for r in rows}
    batches = [exact_rows[i:i + CLASSIFY_BATCH] for i in range(0, len(exact_rows), CLASSIFY_BATCH)]
    prog(0, len(exact_rows), "double-checking exact matches")
    with cf.ThreadPoolExecutor(CLASSIFY_WORKERS) as ex:
        for res in ex.map(lambda b: _safe(lambda: _verify_exact_batch(client, plan, b, med)), batches):
            for v in res or []:
                r = by_asin.get(str(v["asin"]).upper())
                if not r:
                    continue
                verdict = v.get("verdict")
                if verdict in ("close", "not"):
                    r["note"] = f"Downgraded from exact on re-check: {v.get('reason')}"
                    r["match"], r["reason"] = "Close" if verdict == "close" else "Not", v.get("reason")
                new_units = max(1, int(v.get("units_in_listing") or 1))
                if new_units != r["units_in_listing"]:
                    r["note"] = (r["note"] + " " if r["note"] else "") + \
                        f"Units in listing corrected {r['units_in_listing']} -> {new_units} on re-check."
                    r["units_in_listing"] = new_units
                    r["price_per_unit"] = round(r["price"] / new_units, 2) if r.get("price") else None
    for r in [r for r in rows if r["match"] == "Not"]:
        rows.remove(r)
        excluded.append({"asin": r["asin"], "brand": r["brand"], "title": r["title"], "spec_seen": r.get("spec_seen"),
                         "reason": r["note"], "source": r.get("found_via")})

    # 6. units + revenue
    all_rows = rows + ([own_row] if own_row else [])
    model = fit_units_model(all_rows)
    _apply_units(all_rows, model)
    rows.sort(key=lambda r: (r["match"] != "Exact", -(r.get("revenue") or 0), r.get("sales_rank") or 10**9))

    exact = [r for r in rows if r["match"] == "Exact"]
    close = [r for r in rows if r["match"] == "Close"]
    m_exact, m_close = _market(exact), _market(close)
    our_ppu = None
    if our_price:
        our_ppu = float(our_price)
    elif own_row and own_row.get("price_per_unit"):
        our_ppu = own_row["price_per_unit"]
    cheaper_than = None
    if our_ppu and exact:
        ppus = [r["price_per_unit"] for r in exact if r.get("price_per_unit")]
        cheaper_than = round(sum(1 for p in ppus if p > our_ppu) / len(ppus), 3) if ppus else None
    return {
        "description": description, "our_asin": our_asin, "our_price_per_unit": our_ppu,
        "our_cheaper_than_pct": cheaper_than, "plan": plan, "exact": m_exact, "close": m_close,
        "rows": rows, "own_row": own_row, "excluded": excluded,
        "brands": _rollup(exact, "brand"), "brands_close": _rollup(close, "brand"),
        "sellers": _rollup(exact, "bb_seller"),
        "screened": len(cand_list), "from_amazon_search": n_sp, "from_keepa": len(new),
        "keepa_finder_tokens": tokens, "units_model": model,
    }


# ── Workbook ─────────────────────────────────────────────────────────────────

_HDR_FILL = PatternFill("solid", fgColor="1F3864")
_HDR_FONT = Font(color="FFFFFF", bold=True)
_MONEY = '"$"#,##0.00'


def _sheet(wb, title: str, headers: list[str], rows: list[list], money_cols=(), pct_cols=(), int_cols=(), widths=None):
    ws = wb.create_sheet(title)
    ws.append(headers)
    for c in ws[1]:
        c.fill, c.font = _HDR_FILL, _HDR_FONT
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for r in rows:
        ws.append(r)
    for idx, h in enumerate(headers, start=1):
        col = get_column_letter(idx)
        fmt = _MONEY if h in money_cols else "0.0%" if h in pct_cols else "#,##0" if h in int_cols else None
        if fmt:
            for cell in ws[col][1:]:
                cell.number_format = fmt
        ws.column_dimensions[col].width = (widths or {}).get(h, max(10, min(45, len(h) + 4)))
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    return ws


def build_workbook(results: list[dict]) -> bytes:
    wb = Workbook()
    wb.remove(wb.active)
    sh = ["Product", "Spec", "Exact listings", "Exact brands", "Exact units / month", "Exact revenue / month",
          "Avg price / unit", "Median price / unit", "Units-weighted price / unit", "Lowest price / unit",
          "Lowest brand", "Lowest ASIN", "Highest price / unit", "Best sales rank", "Amazon holds Buy Box",
          "Share of units estimated", "Close listings", "Close units / month", "Close revenue / month",
          "Close avg price / unit", "Our price / unit", "We're cheaper than (exact)", "Listings screened",
          "Sales model R2 (points)"]
    srows = []
    for res in results:
        if res.get("error"):
            srows.append([res.get("description"), f"ERROR: {res['error']}"])
            continue
        e, c = res["exact"], res["close"]
        mdl = res.get("units_model")
        srows.append([
            res["description"], res["plan"].get("spec_summary"), e["listings"], e["brands"], e["units_month"],
            e["revenue_month"], e["avg_ppu"], e["median_ppu"], e["weighted_ppu"], e["min_ppu"], e["min_ppu_brand"],
            e["min_ppu_asin"], e["max_ppu"], e["best_rank"], e["amazon_bb_listings"], e["units_estimated_share"],
            c["listings"], c["units_month"], c["revenue_month"], c["avg_ppu"], res.get("our_price_per_unit"),
            res.get("our_cheaper_than_pct"), res["screened"],
            f"{mdl['r2']} ({mdl['points']})" if mdl else "not enough data"])
    _sheet(wb, "Summary", sh, srows,
           money_cols={"Exact revenue / month", "Avg price / unit", "Median price / unit", "Units-weighted price / unit",
                       "Lowest price / unit", "Highest price / unit", "Close revenue / month", "Close avg price / unit",
                       "Our price / unit"},
           pct_cols={"Share of units estimated", "We're cheaper than (exact)"},
           int_cols={"Exact units / month", "Close units / month", "Best sales rank"},
           widths={"Product": 40, "Spec": 40})

    ch = ["Product", "Match", "ASIN", "Brand", "Title", "Buy Box price", "Units in listing", "Price / unit",
          "Units sold / month", "Units source", "Revenue / month", "Sales rank", "Category", "Reviews", "Rating",
          "Buy Box seller", "Amazon BB % (90d)", "FBA offers", "FBM offers", "Spec seen", "Why", "Note", "Found via", "Link"]
    crows = []
    for res in results:
        for r in ([res["own_row"]] if res.get("own_row") else []) + res.get("rows", []):
            crows.append([res["description"], r["match"], r["asin"], r["brand"], r["title"], r["price"],
                          r["units_in_listing"], r["price_per_unit"], r.get("units"), r.get("units_source"),
                          r.get("revenue"), r["sales_rank"], r["category"], r["reviews"], r["rating"], r["bb_seller"],
                          (r["amazon_bb_pct"] or 0) / 100 if r.get("amazon_bb_pct") is not None else None,
                          r["fba_offers"], r["fbm_offers"], r.get("spec_seen"), r.get("reason"), r.get("note"), r.get("found_via"),
                          r["url"]])
    _sheet(wb, "Competitors", ch, crows, money_cols={"Buy Box price", "Price / unit", "Revenue / month"},
           pct_cols={"Amazon BB % (90d)"}, int_cols={"Units sold / month", "Sales rank", "Reviews"},
           widths={"Product": 28, "Title": 60, "Why": 40, "Note": 40, "Spec seen": 28, "Link": 34})

    bh = ["Product", "Match", "Brand", "Listings", "Units / month", "Revenue / month", "Share of units",
          "Avg price / unit", "Lowest price / unit", "Best sales rank"]
    brows = []
    for res in results:
        for label, key in (("Exact", "brands"), ("Close", "brands_close")):
            for b in res.get(key, []):
                brows.append([res["description"], label, b["name"], b["listings"], b["units"], b["revenue"],
                              b["share"], b["avg_ppu"], b["min_ppu"], b["best_rank"]])
    _sheet(wb, "Brands", bh, brows, money_cols={"Revenue / month", "Avg price / unit", "Lowest price / unit"},
           pct_cols={"Share of units"}, int_cols={"Units / month", "Best sales rank"}, widths={"Product": 28, "Brand": 28})

    seh = ["Product", "Buy Box seller", "Listings held", "Units / month", "Revenue / month", "Share of units"]
    serows = [[res["description"], s["name"], s["listings"], s["units"], s["revenue"], s["share"]]
              for res in results for s in res.get("sellers", [])]
    _sheet(wb, "Sellers (exact)", seh, serows, money_cols={"Revenue / month"}, pct_cols={"Share of units"},
           int_cols={"Units / month"}, widths={"Product": 28, "Buy Box seller": 34})

    eh = ["Product", "ASIN", "Brand", "Title", "Spec seen", "Why excluded", "Found via"]
    erows = [[res["description"], x["asin"], x.get("brand"), x.get("title"), x.get("spec_seen"), x.get("reason"),
              x.get("source")] for res in results for x in res.get("excluded", [])]
    _sheet(wb, "Excluded", eh, erows, widths={"Product": 28, "Title": 70, "Why excluded": 45})

    nh = ["Note"]
    notes = [
        ["Market totals (Summary) use EXACT matches only; close variants are reported separately."],
        ["Price / unit = Buy Box price (current, else 30d/90d average) / units in the listing (a 2-pack counts 2)."],
        ["Units sold / month = Amazon's 'bought in past month' via Keepa (a lower bound: '1K+' = 1,000). "
         "Missing values are estimated from sales rank + reviews with a power law fitted on this run's listings."],
        ["Revenue / month = Buy Box price x units sold / month."],
        ["Variations share their parent's sales rank: when a sibling has Amazon's own figure, the other siblings get "
         "none ('Variation (sales on sibling)'); otherwise one estimate is split across the family."],
        ["Exact matches get a second, stricter AI check with the price visible; downgrades and pack-count "
         "corrections are listed in the Note column."],
        [f"Generated {dt.datetime.now():%Y-%m-%d %H:%M} by catalog-verifier Competitor Analysis."],
    ]
    for res in results:
        if res.get("plan"):
            p = res["plan"]
            notes.append([f"{res['description']}: exact = {'; '.join(p.get('exact_criteria') or [])} | close = "
                          f"{'; '.join(p.get('close_criteria') or [])} | searched: "
                          f"{', '.join(p.get('sp_api_phrases') or [])} | Keepa titles: {', '.join(p.get('keepa_title_terms') or [])}"])
    _sheet(wb, "How to read", nh, notes, widths={"Note": 160})

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def run(products: list[dict], on_progress: Callable[[int, int, str], None] | None = None,
        should_cancel=None) -> tuple[bytes, list[dict]]:
    """products: [{description, our_asin?, our_price?, extra_terms?}] -> (xlsx, results)."""
    results = []
    n = len(products)
    for i, p in enumerate(products, start=1):
        label = (p.get("description") or p.get("our_asin") or "")[:50]

        def prog(d, t, phase, _i=i, _label=label):
            if on_progress:
                on_progress(d, t, f"{_i}/{n} {_label}: {phase}" if n > 1 else phase)
        try:
            res = analyze_product(p.get("description") or "", p.get("our_asin"), p.get("our_price"),
                                  p.get("extra_terms"), prog, should_cancel)
        except JobCancelled:
            raise
        except Exception as exc:  # noqa: BLE001
            log.exception("[competitor] product %r failed", label)
            res = {"description": label, "error": str(exc)[:300]}
        results.append(res)
    if on_progress:
        on_progress(n, n, "building the workbook")
    return build_workbook(results), results


def summary_for_ui(results: list[dict]) -> list[dict]:
    out = []
    for r in results:
        if r.get("error"):
            out.append({"description": r.get("description"), "error": r["error"]})
            continue
        out.append({
            "description": r["description"], "spec": r["plan"].get("spec_summary"),
            "exact": r["exact"], "close": r["close"],
            "top_brands": r["brands"][:8], "top_listings": [
                {k: x.get(k) for k in ("asin", "brand", "title", "price_per_unit", "units", "revenue", "sales_rank",
                                       "bb_seller")} for x in r["rows"] if x["match"] == "Exact"][:10],
            "our_price_per_unit": r.get("our_price_per_unit"), "our_cheaper_than_pct": r.get("our_cheaper_than_pct"),
            "screened": r["screened"],
        })
    return out


def filename(results: list[dict]) -> str:
    stem = "bulk" if len(results) != 1 else re.sub(r"[^A-Za-z0-9]+", "_", results[0].get("description") or "product")[:40].strip("_")
    return f"competitor_analysis_{stem}_{dt.date.today().isoformat()}.xlsx"
