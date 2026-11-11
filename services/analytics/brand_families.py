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
Where it is used: a sub-brand IS the same brand, in every scorer (data_cleaning.normalize.candidate_brand_matches):
brand agreement, brand points, the variant check and the brand + MPN rule.
Saved in table brand_families (all runs, all vendors).  The run page lists the pairs a run learned or used,
with a Remove button; a removed pair is never learned again.
"""
from __future__ import annotations

import json
import logging
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
