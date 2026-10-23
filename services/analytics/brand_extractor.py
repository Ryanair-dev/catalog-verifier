"""
AI field extraction for analytics runs: GPT-4o-mini, only when the run has "AI clean titles" ticked.

What it reads
  - vendor titles (before the Amazon search): brand, product_type, model, size, pack_info (used for the Tier 3
    search query and the brand score, as before) and the variant ATTRIBUTES the title states;
  - Amazon listings (before they are scored): the variant attributes their title + description state.
  Attributes: color, finish, size, type, flavor, scent, gender, material, count, volume, weight.
  Each attribute comes with "evidence" = the words of the text that state it.  The code keeps an attribute only
  when that evidence is really written in the text, so a made-up value is dropped.

Where the results go
  - vendor rows: the run's extracted_json, as before (brand, product_type, ...) plus "attrs";
  - listings: attached to the Amazon item as "ai_attrs" when the pair is scored.
  attr_extract.py reads both LAST: they only fill attributes the rules left empty.

Saved once per product (table ai_field_cache): vendor rows by UPC (brand + title when there is no UPC),
listings by ASIN.  A product already in the table is never sent again.  Every call is logged in ai_field_usage.

Needs OPENAI_API_KEY.  Report for one run (reads only, changes nothing):
    uv run python -m services.analytics.brand_extractor --report --run 68
  1. cost   what the run spent, how much came from the table, the cost per 1,000 products
  2. found  how many vendor rows / listings got attributes, and which ones
  3. value  every pair scored twice in memory, without and with the AI attributes: how many verdicts change
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

FILE_VERSION = "2026-10-23 brand_extractor v2 (GPT-4o-mini, attributes with evidence, cache)"
MODEL = "gpt-4o-mini"
PROMPT_VERSION = "fields-v2"
BATCH_SIZE = 20                 # texts per call
MAX_WORKERS = 4                 # parallel calls
DESCRIPTION_CHARS = 800
PRICE_IN, PRICE_OUT = 0.15 / 1_000_000, 0.60 / 1_000_000          # GPT-4o-mini list price per token
ATTRIBUTES = ("color", "finish", "size", "type", "flavor", "scent", "gender", "material", "count", "volume", "weight")
VENDOR_FIELDS = ("brand", "product_type", "model", "size", "pack_info")

_SYSTEM = """\
You read product texts for a program that matches vendor products to Amazon listings.
You get a JSON object {"items": [...]}; each item has an "id", a "kind" ("vendor" = a vendor product title,
"listing" = an Amazon title followed by its description) and the "text".

For EVERY item return "attributes": the variant attributes the text states, each with "evidence" = the exact
words of the text that state it, copied from the text as written:
  color     a color                                   finish  door hardware / faucet finish (satin nickel ...)
  size      a clothing size (S, M, L, XS/S ...)       type    function: privacy, passage, entry, keyed, dummy,
                                                              deadbolt, handleset ...
  flavor    a food flavor                             scent   a scent
  gender    men / women / kids / unisex               material
  count     units in ONE package, a number (not a case or carton quantity like "12/cs")
  volume    with its unit, e.g. "8 fl oz"             weight  with its unit, e.g. "16 oz", "500 g"
Abbreviations and codes may be decoded (BLK -> black, SS -> stainless steel, SN -> satin nickel, PRIV -> privacy);
then the evidence is the abbreviation as written. Leave out what the text does not state. Never guess from the
brand or the product type alone. If unsure, leave it out.

For "vendor" items ALSO return:
  brand         the brand / manufacturer name
  product_type  the product category (e.g. "deodorant", "door lever")
  model         the specific variant / model name, without brand and type; null if none
  size          the size / volume / weight string (e.g. "3.4 oz"); null if none
  pack_info     the pack count if listed (e.g. "pack of 3"); null if single

Reply with a JSON object only:
{"items": [{"id": "0", "brand": "Kwikset", "product_type": "door lever", "model": "Tustin", "size": null,
            "pack_info": null, "attributes": {"finish": {"value": "satin nickel", "evidence": "SN"}}},
           {"id": "1", "attributes": {"color": {"value": "black", "evidence": "Black"}}}]}
"""


def enabled() -> bool:
    return bool(os.getenv("OPENAI_API_KEY"))


# --------------------------------------------------------------------------- #
# Keys and texts
# --------------------------------------------------------------------------- #

def _norm(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").lower()).strip()


def vendor_key(upc: Any, brand: Any, title: Any) -> str | None:
    """'vendor:upc:760172011715', else 'vendor:title:<brand>|<title>', else None."""
    digits = re.sub(r"\D", "", re.sub(r"\.0+$", "", str(upc or "").strip())).lstrip("0")
    if len(digits) >= 6:
        return f"vendor:upc:{digits}"
    t = _norm(title)
    return f"vendor:title:{_norm(brand)}|{t}" if t else None


def listing_key(item: dict) -> str | None:
    asin = str(item.get("asin") or "").strip().upper()
    return f"listing:{asin}" if asin else None


def listing_text(item: dict) -> str:
    title = str(item.get("title") or "").strip()
    description = re.sub(r"\s+", " ", str(item.get("description") or "")).strip()[:DESCRIPTION_CHARS]
    return f"{title}\n{description}".strip()


# --------------------------------------------------------------------------- #
# Tables
# --------------------------------------------------------------------------- #

_TABLES_READY = False


def _db() -> sqlite3.Connection:
    global _TABLES_READY
    from services import database
    conn = database._connect()
    conn.row_factory = sqlite3.Row
    if not _TABLES_READY:
        with database._LOCK:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS ai_field_cache (
                    key TEXT PRIMARY KEY, kind TEXT, result_json TEXT, model TEXT, prompt_version TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS ai_field_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, run_id INTEGER, kind TEXT, items INTEGER,
                    cached INTEGER, sent INTEGER, calls INTEGER, tokens_in INTEGER, tokens_out INTEGER,
                    cost_usd REAL, error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP);
            """)
            conn.commit()
        _TABLES_READY = True
    return conn


def _lookup(conn: sqlite3.Connection, keys: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    keys = [k for k in dict.fromkeys(keys) if k]
    for i in range(0, len(keys), 500):
        chunk = keys[i:i + 500]
        for r in conn.execute(f"SELECT key, result_json FROM ai_field_cache WHERE key IN ({','.join('?' * len(chunk))})",
                              chunk).fetchall():
            try:
                value = json.loads(r["result_json"] or "{}")
            except (TypeError, ValueError):
                value = {}
            out[r["key"]] = value if isinstance(value, dict) else {}
    return out


# --------------------------------------------------------------------------- #
# The AI call and the check
# --------------------------------------------------------------------------- #

def check_attributes(text: str, attrs: Any) -> dict:
    """Keep an attribute only when it is a known one, has a short value, and its evidence is written in the text."""
    kept: dict = {}
    haystack = re.sub(r"\s+", " ", text.lower())
    for box, entry in (attrs.items() if isinstance(attrs, dict) else []):
        if box not in ATTRIBUTES or not isinstance(entry, dict):
            continue
        value = str(entry.get("value") or "").strip()
        evidence = re.sub(r"\s+", " ", str(entry.get("evidence") or "")).strip()
        if value and len(value) <= 40 and evidence and evidence.lower() in haystack:
            kept[box] = {"value": value, "evidence": evidence}
    return kept


def _result(kind: str, text: str, raw: dict) -> dict:
    """What is saved for one product: the checked attributes (+ the vendor fields for a vendor row)."""
    out: dict[str, Any] = {"attrs": check_attributes(text, (raw or {}).get("attributes"))}
    if kind == "vendor":
        for field in VENDOR_FIELDS:
            value = (raw or {}).get(field)
            out[field] = str(value).strip() if value not in (None, "", "null") else ""
    return out


def _call(client: Any, batch: list[dict]) -> tuple[dict[str, dict], int, int]:
    payload = json.dumps({"items": [{"id": str(n), "kind": e["kind"], "text": e["text"]}
                                    for n, e in enumerate(batch)]}, ensure_ascii=False)
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = client.chat.completions.create(
                model=MODEL, temperature=0, response_format={"type": "json_object"},
                messages=[{"role": "system", "content": _SYSTEM}, {"role": "user", "content": payload}],
            )
            parsed = json.loads(response.choices[0].message.content or "{}")
            answers = {str(it.get("id")): it for it in parsed.get("items") or [] if isinstance(it, dict)}
            return answers, int(response.usage.prompt_tokens or 0), int(response.usage.completion_tokens or 0)
        except Exception as exc:  # noqa: BLE001 - retried, then reported
            last_error = exc
            time.sleep(2 ** attempt)
    raise RuntimeError(f"{type(last_error).__name__}: {last_error}")


def _extract(run_id: Any, kind: str, entries: list[dict], progress_cb=None) -> dict[str, dict]:
    """entries: [{"key", "text"}] of one kind.  Looks them up in the table, sends the missing ones to the AI (20 per
    call, 4 calls at a time), saves the answers and logs the usage.  Returns {key: result} for all entries.
    A failed call is not saved, so those products are tried again next time."""
    unique: dict[str, dict] = {}
    for e in entries:
        if e.get("key") and e.get("text") and e["key"] not in unique:
            unique[e["key"]] = {**e, "kind": kind}
    if not unique or not enabled():
        return {}
    from services import database
    conn = _db()
    try:
        results = _lookup(conn, list(unique))
        missing = [e for k, e in unique.items() if k not in results]
        usage = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "sent": 0, "error": ""}
        if missing:
            from openai import OpenAI
            client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            batches = [missing[i:i + BATCH_SIZE] for i in range(0, len(missing), BATCH_SIZE)]

            def run(batch: list[dict]):
                try:
                    return batch, *_call(client, batch), ""
                except Exception as exc:  # noqa: BLE001
                    return batch, None, 0, 0, str(exc)

            done = 0
            with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
                for batch, answers, t_in, t_out, error in pool.map(run, batches):
                    usage["calls"] += 1
                    usage["tokens_in"] += t_in
                    usage["tokens_out"] += t_out
                    done += len(batch)
                    if progress_cb:
                        progress_cb(done, len(missing))
                    if answers is None:
                        usage["error"] = usage["error"] or error[:500]
                        log.warning("[brand_extractor] batch failed: %s", error)
                        continue
                    rows = []
                    for n, e in enumerate(batch):
                        results[e["key"]] = _result(kind, e["text"], answers.get(str(n)) or {})
                        rows.append((e["key"], kind, json.dumps(results[e["key"]], ensure_ascii=False),
                                     MODEL, PROMPT_VERSION))
                    with database._LOCK:
                        conn.executemany("INSERT OR REPLACE INTO ai_field_cache(key, kind, result_json, model, "
                                         "prompt_version) VALUES (?, ?, ?, ?, ?)", rows)
                        conn.commit()
                    usage["sent"] += len(batch)
        cost = round(usage["tokens_in"] * PRICE_IN + usage["tokens_out"] * PRICE_OUT, 5)
        with database._LOCK:
            conn.execute("INSERT INTO ai_field_usage(run_id, kind, items, cached, sent, calls, tokens_in, tokens_out, "
                         "cost_usd, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (run_id, kind, len(unique), len(unique) - len(missing), usage["sent"], usage["calls"],
                          usage["tokens_in"], usage["tokens_out"], cost, usage["error"] or None))
            conn.commit()
    finally:
        conn.close()
    return results


# --------------------------------------------------------------------------- #
# Public entry points (runner.py)
# --------------------------------------------------------------------------- #

def extract_brands(rows: list, run_id: int = 0, progress_cb=None) -> dict[int, dict]:
    """Vendor rows (SourceRow-like: row_idx, upc, brand, title) -> {row_idx: {brand, product_type, model, size,
    pack_info, attrs}}.  Rows without a title are skipped.  Results are saved once per product."""
    keyed = [(r.row_idx, vendor_key(getattr(r, "upc", ""), getattr(r, "brand", ""), r.title), r.title)
             for r in rows if getattr(r, "title", "")]
    results = _extract(run_id, "vendor", [{"key": k, "text": t} for _, k, t in keyed], progress_cb)
    out = {row_idx: results[key] for row_idx, key, _ in keyed if key in results}
    log.info("[brand_extractor] %d/%d vendor rows have AI fields for run %s", len(out), len(keyed), run_id)
    return out


def extract_listing_attrs(run_id: Any, items: list[dict]) -> dict[str, dict]:
    """Amazon items -> {ASIN: attrs} from their title + description.  Saved once per ASIN."""
    keyed = [(str(i.get("asin") or "").upper(), listing_key(i), listing_text(i)) for i in items]
    keyed = [k for k in keyed if k[1] and k[2]]
    results = _extract(run_id, "listing", [{"key": key, "text": text} for _, key, text in keyed])
    return {asin: (results.get(key) or {}).get("attrs") or {} for asin, key, _ in keyed if key in results}


def run_summary(run_id: Any, vendor_rows: list[dict]) -> dict:
    """For the UI: what the AI found for this run's vendor rows, and what the run spent.
    vendor_rows: [{"row_idx", "extracted"}]."""
    with_attrs = {vr["row_idx"]: (vr.get("extracted") or {}).get("attrs") or {} for vr in vendor_rows}
    out = {"rows_total": len(vendor_rows), "rows_with_result": sum(1 for a in with_attrs.values() if a),
           "attributes_found": sum(len(a) for a in with_attrs.values()), "per_row": {k: v for k, v in with_attrs.items() if v},
           "sent": 0, "cached": 0, "cost_usd": 0.0, "error": ""}
    conn = _db()
    try:
        u = conn.execute("SELECT SUM(sent) sent, SUM(cached) cached, SUM(cost_usd) cost, MAX(error) error "
                         "FROM ai_field_usage WHERE run_id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    out.update(sent=int(u["sent"] or 0), cached=int(u["cached"] or 0), cost_usd=round(float(u["cost"] or 0), 4),
               error=str(u["error"] or ""))
    return out


# --------------------------------------------------------------------------- #
# Report (reads only)
# --------------------------------------------------------------------------- #

def _json(text: Any) -> dict:
    try:
        value = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def report(run_id: int, limit: int = 0) -> None:
    from services.analytics.matching_core.adapter import score_pair
    from services.analytics.matching_core.matching.normalize import normalize_text

    conn = _db()
    try:
        run = conn.execute("SELECT vetting_mode, scoring_method, ai_clean_titles FROM analytics_runs WHERE id = ?",
                           (run_id,)).fetchone()
        if not run:
            print(f"run {run_id} not found")
            return
        cat = {r["row_idx"]: (_json(r["data_json"]), _json(r["extracted_json"]), _json(r["query_agent_json"]))
               for r in conn.execute("SELECT row_idx, data_json, extracted_json, query_agent_json "
                                     "FROM analytics_catalog_rows WHERE run_id = ?", (run_id,)).fetchall()}
        cands = conn.execute("SELECT row_idx, asin, sources, review_status, data_json FROM analytics_candidates "
                             "WHERE run_id = ?", (run_id,)).fetchall()
        usage = conn.execute("SELECT kind, SUM(items) items, SUM(cached) cached, SUM(sent) sent, SUM(calls) calls, "
                             "SUM(cost_usd) cost, MAX(error) error FROM ai_field_usage WHERE run_id = ? GROUP BY kind",
                             (run_id,)).fetchall()
        listing_store = _lookup(conn, [listing_key(_json(c["data_json"]).get("amazon") or {}) or "" for c in cands])
    finally:
        conn.close()
    print(f"run {run_id}  ({FILE_VERSION})  AI clean titles: {'on' if run['ai_clean_titles'] else 'OFF'}")

    # 1. cost
    print("\n1. COST")
    total_cost, total_sent = 0.0, 0
    for u in usage:
        total_cost += float(u["cost"] or 0)
        total_sent += int(u["sent"] or 0)
        print(f"   {u['kind']:8s} {int(u['items'] or 0):5d} products: {int(u['cached'] or 0)} from the table, "
              f"{int(u['sent'] or 0)} sent in {int(u['calls'] or 0)} call(s), ${float(u['cost'] or 0):.4f}"
              + (f"   ERROR: {str(u['error'])[:200]}" if u["error"] else ""))
    if not usage:
        print("   nothing logged for this run (AI clean titles off, no OPENAI_API_KEY, or the run predates this version)")
    if total_sent:
        print(f"   ${total_cost:.4f} for {total_sent} new products = ${1000 * total_cost / total_sent:.2f} per 1,000 products")

    # 2. found
    v_attrs = {ri: (ext.get("attrs") or {}) for ri, (_, ext, _) in cat.items()}
    found = Counter(b for a in v_attrs.values() for b in a)
    l_found = Counter(b for r in listing_store.values() for b in (r.get("attrs") or {}))
    print(f"\n2. FOUND   vendor rows with AI attributes: {sum(1 for a in v_attrs.values() if a)} of {len(cat)} "
          f"({', '.join(f'{b} {n}' for b, n in found.most_common()) or 'none'})")
    print(f"           listings with AI attributes: {sum(1 for r in listing_store.values() if r.get('attrs'))} of "
          f"{len({c['asin'] for c in cands})} ({', '.join(f'{b} {n}' for b, n in l_found.most_common()) or 'none'})")

    # 3. value
    moves: Counter = Counter()
    examples: list[str] = []
    done = 0
    for c in cands:
        if (c["review_status"] or "").strip() and c["review_status"] not in ("ASIN claimed", "Variant settled"):
            continue
        if limit and done >= limit:
            break
        data, extracted, agent = cat.get(c["row_idx"], ({}, {}, {}))
        item = dict(_json(c["data_json"]).get("amazon") or {})
        if not item:
            continue
        try:
            sources = json.loads(c["sources"] or "[]")
        except (TypeError, ValueError):
            sources = []
        source = {"upc": data.get("upc", ""), "mpn": data.get("itemid", ""), "itemid": data.get("itemid", ""),
                  "brand": data.get("brand", ""), "manufacturer": data.get("brand", ""), "raw": data.get("raw") or {},
                  "run_id": run_id, "title": normalize_text(agent.get("comparison_title") or data.get("title") or "")}
        without_ext = {k: v for k, v in extracted.items() if k != "attrs"}
        item.pop("ai_attrs", None)
        _, without = score_pair(source, item, method=run["scoring_method"] or "weighted",
                                mode=run["vetting_mode"] or "cpg", extracted=without_ext or None, sources=sources)
        l_attrs = (listing_store.get(listing_key(item) or "") or {}).get("attrs")
        if l_attrs:
            item["ai_attrs"] = l_attrs
        _, with_ai = score_pair(source, item, method=run["scoring_method"] or "weighted",
                                mode=run["vetting_mode"] or "cpg", extracted=extracted or None, sources=sources)
        done += 1
        if without != with_ai:
            moves[(without, with_ai)] += 1
            if len(examples) < 30:
                used = "; ".join(f"{b} {a['value']}" for b, a in (extracted.get("attrs") or {}).items())
                examples.append(f"   row {int(c['row_idx']) + 1:>5}  {c['asin']}  {without:>12} -> {with_ai:<12} "
                                f"vendor AI: {used or '-'}")
    names = {"verified": "Approved", "review": "Review", "not_approved": "Not Approved"}
    print(f"\n3. VALUE   {done} pairs scored without and with the AI attributes: {sum(moves.values())} verdicts change")
    for (a, b), n in moves.most_common():
        print(f"   {names.get(a, a):>12} -> {names.get(b, b):<12} {n}")
    if examples:
        print("\n   examples:\n" + "\n".join(examples))


def main(argv: list[str] | None = None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).resolve().parents[2] / ".env")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="AI field extraction: cost / found / value report for one run")
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--run", type=int, required=True)
    ap.add_argument("--limit", type=int, default=0, help="score at most this many pairs (0 = all)")
    a = ap.parse_args(argv)
    if a.report:
        report(a.run, a.limit)
    return 0


if __name__ == "__main__":
    sys.exit(main())
