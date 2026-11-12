"""
Sub-brands learned from Amazon itself (Hygea -> PDI, Sani Professional -> PDI ...).

Why: the "brand + MPN = same item" rule needs the Amazon brand to match the vendor's brand.  Many listings use a
sub-brand instead ("Hygea" for a PDI product), so the rule did not fire and correct items went to Review.

How a pair is learned (after a run's search, and at the start of a re-score):
  one MPN of the vendor file appears on Amazon with the vendor's brand (PDI) AND with another brand (Hygea)
  -> Hygea is learned as a sub-brand of PDI.  Safety checks, all required:
     - the Amazon MPN is the SAME as the vendor row's MPN (exact, mpns_match);
     - the MPN is a real code (not a short number like "100");
     - the sub-brand listing's title is similar to the vendor title (>= MIN_TITLE_SIMILARITY), so a part number
       two companies use by accident (fuel pump vs wipes) teaches nothing;
     - the brand is not a placeholder ("Generic", "Unbranded" ...);
     - the pair was not removed by a person before.
AI brand check (ask_ai_about_brands): Amazon cannot teach a pair when no listing uses the vendor's brand (vendor
file says "Essity", every listing says "JOBST").  Then Claude is asked once per pair, "same brand family?"
(same brand, sub-brand / product line, or owner; NOT sister brands or distributors).  Only a high-confidence
yes is saved (source 'ai', with the reason); every answer is cached, so no pair is asked twice.
Where it is used: a sub-brand IS the same brand, in every scorer (data_cleaning.normalize.candidate_brand_matches):
brand agreement, brand points, the variant check and the brand + MPN rule.
Saved in table brand_families (all runs, all vendors).  The run page lists the pairs a run learned or used,
with a Remove button; a removed pair is never learned again.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

MIN_TITLE_SIMILARITY = 50.0
CACHE_SECONDS = 30.0
PLACEHOLDER_BRANDS = frozenset({
    "generic", "unbranded", "unknown", "none", "na", "nobrand", "other", "various", "assorted",
    "notapplicable", "brandless", "oem",
})

# AI brand check: for brand pairs Amazon cannot teach (no listing says "Essity", so "JOBST -> Essity" is never
# seen), ask Claude once per pair whether the two names are the same brand family.  On by default when an
# Anthropic key is set; BRAND_AI_CHECK=0 turns it off.  One call per run at most, answers kept forever.
AI_MODEL = os.getenv("BRAND_AI_MODEL", "claude-sonnet-4-6")
AI_MAX_PAIRS = 40

_TABLE_READY = False
_CACHE: dict[str, Any] = {"at": None, "pairs": {}}          # at=None: not loaded / cleared
_CACHE_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# Table
# --------------------------------------------------------------------------- #

def _db() -> sqlite3.Connection:
    global _TABLE_READY
    from services import database
    conn = database._connect()
    conn.row_factory = sqlite3.Row
    if not _TABLE_READY:
        with database._LOCK:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS brand_families (
                    sub_key TEXT NOT NULL, parent_key TEXT NOT NULL,
                    sub_brand TEXT, parent_brand TEXT,
                    status TEXT DEFAULT 'active',            -- 'active' | 'removed'
                    evidence_mpn TEXT, evidence_asin TEXT, anchor_asin TEXT, run_id INTEGER,
                    vendor_title TEXT, amazon_title TEXT, title_similarity REAL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP, removed_at TEXT,
                    PRIMARY KEY (sub_key, parent_key))
            """)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(brand_families)").fetchall()}
            if "source" not in cols:            # 'amazon' = learned from listings, 'ai' = confirmed by Claude
                conn.execute("ALTER TABLE brand_families ADD COLUMN source TEXT DEFAULT 'amazon'")
            if "note" not in cols:              # the AI's one-line reason
                conn.execute("ALTER TABLE brand_families ADD COLUMN note TEXT")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS brand_ai_answers (
                    vendor_key TEXT NOT NULL, amazon_key TEXT NOT NULL,
                    vendor_brand TEXT, amazon_brand TEXT,
                    same_family INTEGER, confidence TEXT, relation TEXT, model TEXT, run_id INTEGER,
                    asked_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (vendor_key, amazon_key))
            """)
            conn.commit()
        _TABLE_READY = True
    return conn


def _key(brand: Any) -> str:
    from services.analytics.data_cleaning.normalize import normalize_brand
    return normalize_brand(str(brand or ""))


def _invalidate() -> None:
    with _CACHE_LOCK:
        _CACHE["at"] = None


def _active_pairs() -> dict[tuple[str, str], tuple[str, str]]:
    """{(sub_key, parent_key): (sub_brand, parent_brand)} for active pairs, cached for CACHE_SECONDS."""
    with _CACHE_LOCK:
        if _CACHE["at"] is not None and time.monotonic() - _CACHE["at"] < CACHE_SECONDS:
            return _CACHE["pairs"]
    pairs: dict[tuple[str, str], tuple[str, str]] = {}
    try:
        conn = _db()
        try:
            for r in conn.execute("SELECT sub_key, parent_key, sub_brand, parent_brand FROM brand_families "
                                  "WHERE status='active'").fetchall():
                pairs[(r["sub_key"], r["parent_key"])] = (r["sub_brand"] or r["sub_key"], r["parent_brand"] or r["parent_key"])
        finally:
            conn.close()
    except Exception:  # noqa: BLE001 - no table / no DB (scripts, tests): simply no learned pairs
        log.debug("brand_families: could not load pairs", exc_info=True)
    with _CACHE_LOCK:
        _CACHE["pairs"], _CACHE["at"] = pairs, time.monotonic()
    return pairs


# --------------------------------------------------------------------------- #
# Used by the brand + MPN rule
# --------------------------------------------------------------------------- #

def family_pair(vendor_brand: Any, candidate_brand: Any, candidate_manufacturer: Any = None) -> tuple[str, str] | None:
    """(sub_brand, parent_brand) when the Amazon brand/manufacturer and the vendor brand are a learned
    sub-brand / parent pair (either way round: vendor 'PDI' + Amazon 'Hygea', or vendor 'Hygea' + Amazon 'PDI').
    None otherwise.  candidate_brand_matches (every scorer) and the brand + MPN rule call this."""
    vk = _key(vendor_brand)
    if not vk:
        return None
    pairs = _active_pairs()
    if not pairs:
        return None
    for value in (candidate_brand, candidate_manufacturer):
        ck = _key(value)
        if not ck or ck == vk:
            continue
        hit = pairs.get((ck, vk)) or pairs.get((vk, ck))
        if hit:
            return hit
    return None


# --------------------------------------------------------------------------- #
# Learning
# --------------------------------------------------------------------------- #

def _real_code(mpn: Any) -> bool:
    """A part number long and specific enough that two companies are unlikely to share it by accident."""
    from services.analytics.data_cleaning.normalize import base_mpn
    b = base_mpn(str(mpn or ""))
    if len(b) < 5 or not re.search(r"\d", b):
        return False
    return bool(re.search(r"[A-Za-z]", b)) or len(b) >= 7        # all-digit codes need 7+ digits


def _json(text: Any) -> dict:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def learn_sub_brands(run_id: int) -> list[dict]:
    """Learn new sub-brand pairs from this run's candidates.  Returns the pairs learned now (may be empty)."""
    from services import database
    from services.analytics.data_cleaning.normalize import (
        base_mpn, candidate_brand_matches, candidate_part_number, mpns_match, title_similarity,
    )
    conn = _db()
    try:
        cat = conn.execute("SELECT row_idx, data_json, extracted_json FROM analytics_catalog_rows WHERE run_id=?",
                           (run_id,)).fetchall()
        cands = conn.execute("SELECT row_idx, asin, data_json FROM analytics_candidates WHERE run_id=?",
                             (run_id,)).fetchall()
        known = {(r["sub_key"], r["parent_key"]) for r in conn.execute(
            "SELECT sub_key, parent_key FROM brand_families").fetchall()}        # active AND removed
    finally:
        conn.close()

    rows: dict[int, dict] = {}
    for r in cat:
        d, ext = _json(r["data_json"]), _json(r["extracted_json"])
        rows[int(r["row_idx"])] = {
            "mpn": str(d.get("itemid") or d.get("mpn") or "").strip(),
            "brand": str(d.get("brand") or ext.get("brand") or "").strip(),
            "titles": [t for t in (str(d.get("title") or ""), str(ext.get("keyword_specs") or "")) if t],
        }

    # group the listings by the vendor MPN they really carry: {base mpn: {"vendor": row info, "items": [...]}}
    groups: dict[str, dict] = {}
    for c in cands:
        row = rows.get(int(c["row_idx"]))
        if not row or not row["mpn"] or not row["brand"] or not _real_code(row["mpn"]):
            continue
        amz = _json(c["data_json"]).get("amazon") or {}
        amz_mpn = candidate_part_number(amz)
        if not amz_mpn or not mpns_match(row["mpn"], amz_mpn):
            continue
        g = groups.setdefault(base_mpn(row["mpn"]), {"vendor": row, "items": {}})
        g["items"][str(c["asin"])] = amz

    learned: list[dict] = []
    new_keys: set[tuple[str, str]] = set()
    for mpn_key, g in groups.items():
        vendor = g["vendor"]
        parent_brand, parent_key = vendor["brand"], _key(vendor["brand"])
        if not parent_key:
            continue
        anchors = [asin for asin, amz in g["items"].items()
                   if candidate_brand_matches(parent_brand, amz.get("brand"), amz.get("manufacturer"), use_learned=False)]
        if not anchors:
            continue                                   # no listing with the vendor's own brand: nothing proves it
        for asin, amz in g["items"].items():
            sub_brand = str(amz.get("brand") or "").strip()
            sub_key = _key(sub_brand)
            if (not sub_key or sub_key == parent_key or sub_key in PLACEHOLDER_BRANDS
                    or candidate_brand_matches(parent_brand, amz.get("brand"), amz.get("manufacturer"), use_learned=False)):
                continue
            if (sub_key, parent_key) in known or (sub_key, parent_key) in new_keys:
                continue
            sim = max((title_similarity(t, amz.get("title")) for t in vendor["titles"]), default=0.0)
            if sim < MIN_TITLE_SIMILARITY:
                continue
            new_keys.add((sub_key, parent_key))
            learned.append({
                "sub_key": sub_key, "parent_key": parent_key, "sub_brand": sub_brand, "parent_brand": parent_brand,
                "evidence_mpn": vendor["mpn"], "evidence_asin": asin, "anchor_asin": anchors[0], "run_id": run_id,
                "vendor_title": vendor["titles"][0][:300] if vendor["titles"] else "",
                "amazon_title": str(amz.get("title") or "")[:300], "title_similarity": round(sim, 1),
            })

    if learned:
        conn = _db()
        try:
            with database._LOCK:
                conn.executemany(
                    "INSERT OR IGNORE INTO brand_families(sub_key, parent_key, sub_brand, parent_brand, status, "
                    "evidence_mpn, evidence_asin, anchor_asin, run_id, vendor_title, amazon_title, title_similarity) "
                    "VALUES (:sub_key, :parent_key, :sub_brand, :parent_brand, 'active', :evidence_mpn, "
                    ":evidence_asin, :anchor_asin, :run_id, :vendor_title, :amazon_title, :title_similarity)",
                    learned,
                )
                conn.commit()
        finally:
            conn.close()
        _invalidate()
        log.info("Run %s learned sub-brands: %s", run_id,
                 ", ".join(f"{p['sub_brand']} -> {p['parent_brand']}" for p in learned))
    return learned


def rows_to_rescore(run_id: int) -> set[int]:
    """Vendor rows that have a candidate whose brand matches only through a learned pair (a sub-brand is the
    same brand in every scorer, so all of those rows score differently once a pair is learned)."""
    from services.analytics.data_cleaning.normalize import candidate_brand_matches
    if not _active_pairs():
        return set()
    conn = _db()
    try:
        cat = {int(r["row_idx"]): _json(r["data_json"]) for r in conn.execute(
            "SELECT row_idx, data_json FROM analytics_catalog_rows WHERE run_id=?", (run_id,)).fetchall()}
        cands = conn.execute("SELECT row_idx, data_json FROM analytics_candidates WHERE run_id=?",
                             (run_id,)).fetchall()
    finally:
        conn.close()
    out: set[int] = set()
    for c in cands:
        ri = int(c["row_idx"])
        d = cat.get(ri) or {}
        amz = _json(c["data_json"]).get("amazon") or {}
        brand = str(d.get("brand") or "")
        if not brand:
            continue
        if candidate_brand_matches(brand, amz.get("brand"), amz.get("manufacturer"), use_learned=False):
            continue
        if family_pair(brand, amz.get("brand"), amz.get("manufacturer")):
            out.add(ri)
    return out


# --------------------------------------------------------------------------- #
# AI brand check (Claude): pairs Amazon cannot teach
# --------------------------------------------------------------------------- #

_AI_SYSTEM = """You check brand ownership for product matching (vendor catalog vs. Amazon listings).
For each pair, decide if the two names are the SAME BRAND FAMILY, meaning one of these is true:
  - they are the same brand (other spelling, abbreviation or old name), or
  - one is a product line or sub-brand of the other (Sani-Cloth is a PDI brand), or
  - one is the company that owns the other (Essity owns JOBST; BSN medical owns Leukoplast).
NOT the same family:
  - sister brands that only share an owner (JOBST and Leukoplast are both Essity brands, but they are
    different brands, so the answer is false),
  - a distributor, retailer or store brand next to a manufacturer brand (McKesson, Medline, Amazon Basics),
    unless it really owns that brand,
  - two unrelated brands.
Answer only from what you know for sure. If you are not sure, answer false with confidence "low".
Reply with JSON only, no other text:
{"answers": [{"i": 0, "same_family": true, "confidence": "high", "relation": "JOBST is a brand of Essity (via BSN medical)"}]}
"""


def _ai_pairs(run_id: int) -> list[dict]:
    """(vendor brand, Amazon brand) pairs of this run worth asking about: the plain comparison says different,
    the Amazon brand is a real brand, and the listing looks like the vendor product (exact MPN, or a title
    at least MIN_TITLE_SIMILARITY similar).  Most-seen pairs first, at most AI_MAX_PAIRS."""
    from services.analytics.data_cleaning.normalize import (
        candidate_brand_matches, candidate_part_number, mpns_match, title_similarity,
    )
    conn = _db()
    try:
        cat = {int(r["row_idx"]): (_json(r["data_json"]), _json(r["extracted_json"])) for r in conn.execute(
            "SELECT row_idx, data_json, extracted_json FROM analytics_catalog_rows WHERE run_id=?", (run_id,)).fetchall()}
        cands = conn.execute("SELECT row_idx, data_json FROM analytics_candidates WHERE run_id=?", (run_id,)).fetchall()
        asked = {(r[0], r[1]) for r in conn.execute("SELECT vendor_key, amazon_key FROM brand_ai_answers").fetchall()}
        known = set()
        for r in conn.execute("SELECT sub_key, parent_key FROM brand_families").fetchall():
            known |= {(r[0], r[1]), (r[1], r[0])}
    finally:
        conn.close()
    pairs: dict[tuple[str, str], dict] = {}
    for c in cands:
        d, ext = cat.get(int(c["row_idx"]), ({}, {}))
        vendor_brand = str(d.get("brand") or ext.get("brand") or "").strip()
        amz = _json(c["data_json"]).get("amazon") or {}
        amazon_brand = str(amz.get("brand") or "").strip()
        vk, ak = _key(vendor_brand), _key(amazon_brand)
        if not vk or not ak or vk == ak or ak in PLACEHOLDER_BRANDS or vk in PLACEHOLDER_BRANDS:
            continue
        if (vk, ak) in asked or (vk, ak) in known:
            continue
        if candidate_brand_matches(vendor_brand, amazon_brand, amz.get("manufacturer"), use_learned=False):
            continue
        vendor_title = str(d.get("title") or "")
        same_mpn = bool(d.get("itemid")) and mpns_match(str(d.get("itemid")), candidate_part_number(amz))
        if not same_mpn and title_similarity(vendor_title, amz.get("title")) < MIN_TITLE_SIMILARITY:
            continue
        p = pairs.setdefault((vk, ak), {"vendor_key": vk, "amazon_key": ak, "vendor_brand": vendor_brand,
                                        "amazon_brand": amazon_brand, "seen": 0,
                                        "vendor_title": vendor_title[:160], "amazon_title": str(amz.get("title") or "")[:160]})
        p["seen"] += 1
    return sorted(pairs.values(), key=lambda p: -p["seen"])[:AI_MAX_PAIRS]


def _parse_answers(text: str) -> list[dict]:
    text = re.sub(r"^```(?:json)?|```$", "", (text or "").strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return []
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return []
    answers = data.get("answers") if isinstance(data, dict) else None
    return [a for a in (answers or []) if isinstance(a, dict)]


def ask_ai_about_brands(run_id: int, client: Any = None) -> list[dict]:
    """Ask Claude about this run's brand pairs that are not known yet (one call).  Every answer is cached, so a
    pair is never asked twice.  A "same family" answer with high confidence becomes a learned pair (source
    'ai'), used exactly like a pair learned from Amazon and removable on the run page.  Returns the new pairs."""
    if os.getenv("BRAND_AI_CHECK", "1").strip() == "0":
        return []
    if client is None:
        if not os.getenv("ANTHROPIC_API_KEY"):
            return []
        import anthropic
        client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"), max_retries=2)
    pairs = _ai_pairs(run_id)
    if not pairs:
        return []
    msg = json.dumps([{"i": n, "vendor_brand": p["vendor_brand"], "amazon_brand": p["amazon_brand"],
                       "example_vendor_title": p["vendor_title"], "example_amazon_title": p["amazon_title"]}
                      for n, p in enumerate(pairs)], ensure_ascii=False)
    resp = client.messages.create(model=AI_MODEL, max_tokens=4000, system=_AI_SYSTEM,
                                  messages=[{"role": "user", "content": msg}])
    text = "".join(getattr(b, "text", "") for b in (resp.content or []))
    usage = getattr(resp, "usage", None)
    log.info("AI brand check, run %s: %d pairs, tokens in %s / out %s", run_id, len(pairs),
             getattr(usage, "input_tokens", "?"), getattr(usage, "output_tokens", "?"))
    by_i = {}
    for a in _parse_answers(text):
        try:
            by_i[int(a.get("i"))] = a
        except (TypeError, ValueError):
            continue
    if not by_i:
        log.warning("AI brand check, run %s: no usable answer (%s)", run_id, text[:200])
        return []

    from services import database
    learned: list[dict] = []
    conn = _db()
    try:
        with database._LOCK:
            for n, p in enumerate(pairs):
                a = by_i.get(n)
                if not a:
                    continue                                 # not answered: may be asked again next run
                same = a.get("same_family") is True
                conf = str(a.get("confidence") or "").lower()
                relation = str(a.get("relation") or "").strip()[:300]
                conn.execute(
                    "INSERT OR REPLACE INTO brand_ai_answers(vendor_key, amazon_key, vendor_brand, amazon_brand, "
                    "same_family, confidence, relation, model, run_id) VALUES (?,?,?,?,?,?,?,?,?)",
                    (p["vendor_key"], p["amazon_key"], p["vendor_brand"], p["amazon_brand"], int(same), conf,
                     relation, AI_MODEL, run_id))
                if same and conf == "high":
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO brand_families(sub_key, parent_key, sub_brand, parent_brand, status, "
                        "run_id, vendor_title, amazon_title, source, note) VALUES (?,?,?,?, 'active', ?,?,?, 'ai', ?)",
                        (p["amazon_key"], p["vendor_key"], p["amazon_brand"], p["vendor_brand"], run_id,
                         p["vendor_title"], p["amazon_title"], relation))
                    if cur.rowcount:
                        learned.append({"sub_brand": p["amazon_brand"], "parent_brand": p["vendor_brand"],
                                        "note": relation})
            conn.commit()
    finally:
        conn.close()
    if learned:
        _invalidate()
        log.info("Run %s: AI confirmed brands: %s", run_id,
                 ", ".join(f"{p['sub_brand']} = {p['parent_brand']}" for p in learned))
    return learned


# --------------------------------------------------------------------------- #
# Run page: list + remove
# --------------------------------------------------------------------------- #

def pairs_for_run(run_id: int) -> list[dict]:
    """Pairs this run learned, plus pairs its candidates used (scores.brand_family), active or removed."""
    conn = _db()
    try:
        used = {str(r[0]) for r in conn.execute(
            "SELECT DISTINCT json_extract(data_json,'$.scores.brand_family') FROM analytics_candidates "
            "WHERE run_id=? AND json_extract(data_json,'$.scores.brand_family') IS NOT NULL", (run_id,)).fetchall()}
        rows = conn.execute("SELECT * FROM brand_families ORDER BY created_at").fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        label = f"{r['sub_brand']} → {r['parent_brand']}"
        if r["run_id"] == run_id or label in used:
            out.append({
                "sub_brand": r["sub_brand"], "parent_brand": r["parent_brand"],
                "sub_key": r["sub_key"], "parent_key": r["parent_key"], "status": r["status"],
                "evidence_mpn": r["evidence_mpn"], "evidence_asin": r["evidence_asin"],
                "learned_in_run": r["run_id"], "used_in_this_run": label in used,
                "source": (r["source"] if "source" in r.keys() else None) or "amazon",
                "note": (r["note"] if "note" in r.keys() else "") or "",
            })
    return out


def remove_pair(sub_key: str, parent_key: str) -> bool:
    """A person said this pair is wrong: never use or learn it again."""
    from services import database
    conn = _db()
    try:
        with database._LOCK:
            cur = conn.execute("UPDATE brand_families SET status='removed', removed_at=CURRENT_TIMESTAMP "
                               "WHERE sub_key=? AND parent_key=?", (sub_key, parent_key))
            conn.commit()
    finally:
        conn.close()
    _invalidate()
    return bool(cur.rowcount)
