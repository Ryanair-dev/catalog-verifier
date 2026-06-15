"""
Analytics AI Check — batch verification of analytics candidates.

Tries Anthropic Claude first (ANTHROPIC_API_KEY); falls back to OpenAI
(OPENAI_API_KEY).  For each source catalog row, groups all its scored
candidates and sends them (up to BATCH_SIZE per call) asking the AI to decide
which ones actually match the vendor item.  Results are stored as ai_verdict /
ai_reasoning on analytics_candidates and shown as badge overlays in the UI
without changing the scored verdict.

Progress is tracked via three columns on analytics_runs:
  ai_check_status  TEXT  — null | "Running" | "Done" | "Error: ..."
  ai_check_done    INT   — candidates processed so far
  ai_check_total   INT   — total candidates to process
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from collections import defaultdict

from services import database

log = logging.getLogger(__name__)

BATCH_SIZE   = 10   # candidates per AI call
MAX_WORKERS  = 4    # parallel calls

AVG_INPUT_TOKENS  = 350
AVG_OUTPUT_TOKENS = 100
AVG_LATENCY_MS    = 900

PRICING = {
    # Claude models (primary)
    "claude-sonnet-4-6":         {"input": 0.003,   "output": 0.015},
    "claude-haiku-4-5-20251001": {"input": 0.0008,  "output": 0.004},
    # OpenAI models (fallback)
    "gpt-4o-mini": {"input": 0.00015, "output": 0.00060},
    "gpt-4o":      {"input": 0.00250, "output": 0.01000},
}


# --------------------------------------------------------------------------- #
# Cost estimation
# --------------------------------------------------------------------------- #

def estimate_cost(candidate_count: int, model: str = "claude-sonnet-4-6") -> dict:
    batches   = (candidate_count + BATCH_SIZE - 1) // BATCH_SIZE
    price     = PRICING.get(model) or PRICING["claude-sonnet-4-6"]
    in_tok    = batches * AVG_INPUT_TOKENS
    out_tok   = batches * AVG_OUTPUT_TOKENS
    cost_usd  = (in_tok / 1000) * price["input"] + (out_tok / 1000) * price["output"]
    duration  = batches * AVG_LATENCY_MS / MAX_WORKERS
    return {
        "candidate_count":    candidate_count,
        "model":              model,
        "cost_usd_est":       round(cost_usd, 4),
        "duration_ms_est":    int(duration),
    }


# --------------------------------------------------------------------------- #
# Prompt helpers
# --------------------------------------------------------------------------- #

_SYSTEM = (
    "You are a CPG product matching auditor. "
    "You receive a vendor catalog item and a numbered list of Amazon candidates. "
    "Each candidate includes a 'matcher_score' — the confidence the automated matcher "
    "already assigned (0–100). Use this as a prior: high scores mean the matcher is "
    "confident they match, so only reject on CLEAR, UNAMBIGUOUS mismatches.\n"
    "Each candidate may also include a Description and bullet points (•) from the "
    "Amazon listing — use these to verify product type, ingredients, size, scent, "
    "and other details that are often absent from the title alone.\n\n"
    "Rules:\n"
    "- Brand identification (most important rule):\n"
    "  1. Use 'extracted_brand' when provided — it was parsed from the vendor title and is "
    "the most reliable signal.\n"
    "  2. When 'extracted_brand' is absent, use 'title_brand' (first word(s) of vendor title "
    "before the first comma or hyphen) — vendor titles almost always start with the brand.\n"
    "  3. The vendor 'brand' field often contains a DISTRIBUTOR or PARENT COMPANY name "
    "(e.g. 'Fabrication Industries', 'McKesson', 'Medline') — do NOT use it for brand "
    "matching unless it clearly matches the Amazon brand.\n"
    "  4. If the Amazon title brand and the vendor title brand are clearly different companies "
    "(e.g. vendor='SpiderTech', Amazon='TheraCopper') → reject with high confidence.\n"
    "  5. Treat brand names as the SAME when they differ ONLY by spacing, punctuation, "
    "capitalization, a corporate/legal suffix, or a minor spelling variant — e.g. "
    "'SheaMoisture'='Shea Moisture', 'Cardinal Health'='Cardinal', 'Moleskine'='Moleskin', "
    "'Palmer\\'s'='Palmers'. Do NOT reject these as different brands.\n"
    "- SAME-ITEM RULE (strong): when the vendor and Amazon brand are the same (allowing the "
    "variations in rule 5) AND the size matches within 10% AND the core product type is the "
    "same → 'approve'. These are the same item even when marketing/descriptor words differ. "
    "Only withhold approval if the core product type clearly differs, or a scent/count/color "
    "contradiction is present.\n"
    "- Product type must be the same core item. Minor label copy differences are NOT a "
    "mismatch — e.g. 'Cocktail Peanuts' vs 'Peanuts', 'Lightly Salted' vs 'Salted', "
    "'Dry Roasted' vs 'Dry Roasted Lightly Salted', 'Classic' vs no qualifier. "
    "Only reject when the core product type clearly differs (shampoo ≠ conditioner, "
    "deodorant ≠ hair dye, peanuts ≠ mixed nuts).\n"
    "- When matcher_score ≥ 90: only reject on HARD mismatches — completely different "
    "brand, completely different product category, or a clear size difference > 20%. "
    "Word order differences, packaging descriptor words, and minor variant naming "
    "(e.g. 'Lightly Salted' vs 'Salted', 'Cocktail' descriptor, 'Classic' label) "
    "should yield 'uncertain' at most, never 'reject'.\n"
    "- When matcher_score ≥ 80 and brand + product type clearly match: default to "
    "'approve' unless you see an unmistakable hard mismatch.\n"
    "- UPC match is a strong signal but NOT conclusive — Amazon sometimes cross-attaches "
    "UPCs to wrong products. Verify brand AND product type even when UPC matches.\n"
    "- APPAREL/GARMENT SIZE (S/M/L/XL) is a HARD reject when it differs: vendor "
    "'X-Large' vs Amazon 'Medium', 'Small/Medium' vs 'Large/X-Large' → reject. "
    "Overlapping ranges that share a size ('Large' vs 'Large/X-Large') are OK.\n"
    "- COLOR is a HARD reject when both sides name a clearly different colour "
    "(Black vs White, Blue vs Red). Same or overlapping colours are fine; a "
    "colour on only one side is not a mismatch.\n"
    "- Size must match within 10% (8 oz ≠ 16 oz; 8 oz ≈ 236 ml is fine). "
    "When the Amazon title lists a size alongside a pack count (e.g. '3.25 oz (2 Pack)'), "
    "the size is PER UNIT — compare it directly to the vendor size, do NOT multiply by pack count.\n"
    "- Pack / bundle differences are NOT a mismatch: when the brand, product type, and "
    "per-unit size match, a different pack or case count (vendor case of 144 vs Amazon "
    "'3 Count', single vs multipack) is just bundling — 'approve'. Only a per-UNIT count "
    "that defines the SKU and differs (e.g. '80 Count' wipes vs '110 Count' wipes, where "
    "there is no per-unit size) is a reject.\n"
    "- SHADE / COLOR-VARIANT differences are a HARD reject: for hair colour, cosmetics, and "
    "similar shade-keyed products the brand/size/type are identical across the whole line and "
    "only the shade is the SKU. If the shade codes differ (Bigen '#46' vs '#48', 'No. 7' vs "
    "'No. 5') OR the tonal qualifiers differ ('Light Chestnut' vs 'Dark Chestnut', 'Medium' vs "
    "'Light') → reject. Do NOT approve one shade against another.\n"
    "- Scent/flavor differences → reject only when BOTH sides specify it AND they clearly conflict\n\n"
    "Respond ONLY with a raw JSON object (no markdown, no code blocks):\n"
    '{"results": [{"asin":"...","verdict":"approve|reject|uncertain",'
    '"confidence":"high|medium|low","reasoning":"<25 words"}]}\n'
    "One entry per candidate, IN THE SAME ORDER as the input."
)


def _fmt_candidate(i: int, c: dict) -> str:
    amz    = c.get("amazon") or {}
    attrs  = amz.get("attributes") or {}
    scores = (c.get("data") or {}).get("scores") or {}
    matcher_score = round(float(c.get("confidence") or 0), 1)
    lines = [
        f"[{i + 1}] ASIN: {c['asin']}  matcher_score: {matcher_score}%",
        f"    Title: {amz.get('title') or '-'}",
        f"    Brand: {amz.get('brand') or amz.get('manufacturer') or '-'}",
    ]
    if attrs.get("size"):  lines.append(f"    Size: {attrs['size']}")
    if attrs.get("color"): lines.append(f"    Color: {attrs['color']}")
    if scores.get("upc_match"):
        lines.append("    UPC: matched (verify brand+type still correct)")

    # Include description (first 300 chars) — surfaces variant/ingredient info
    # that rarely appears in the title but is decisive for correct matching.
    desc = (amz.get("description") or "").strip()
    if desc:
        lines.append(f"    Description: {desc[:300]}{'…' if len(desc) > 300 else ''}")

    # Include first 3 bullet points (each capped at 120 chars) — bullet points
    # often contain size, count, scent, or product-type details not in the title.
    bullets = [b for b in (amz.get("bullet_points") or []) if b and str(b).strip()]
    for b in bullets[:3]:
        b_text = str(b).strip()
        lines.append(f"    • {b_text[:120]}{'…' if len(b_text) > 120 else ''}")

    return "\n".join(lines)


def _title_brand(title: str) -> str:
    """
    Extract the likely brand from a vendor product title.
    Almost all CPG/medical titles start with the brand name followed by a
    comma, hyphen, or long descriptor word.  Return the first 1-3 words
    before the first comma or ' - ', capped at 30 characters.
    """
    if not title:
        return ""
    # Split on first comma or " - "
    first_part = re.split(r",|\s+-\s+", title.strip(), maxsplit=1)[0].strip()
    # Take at most the first 3 words (avoids pulling in model/size info)
    words = first_part.split()
    return " ".join(words[:3]) if words else ""


def _build_user_msg(source: dict, batch: list[dict]) -> str:
    cands = "\n\n".join(_fmt_candidate(i, c) for i, c in enumerate(batch))
    ext = source.get("_extracted") or {}
    vendor_title = source.get("title") or "-"
    source_obj: dict = {
        "title":           vendor_title,
        "brand":           source.get("brand") or "-",
        "upc":             source.get("upc")   or "-",
    }
    if ext.get("brand"):
        source_obj["extracted_brand"] = ext["brand"]
    elif vendor_title and vendor_title != "-":
        # Fallback when AI title-cleaning wasn't run: derive brand from title
        tb = _title_brand(vendor_title)
        if tb:
            source_obj["title_brand"] = tb
    if ext.get("product_type"):
        source_obj["product_type"] = ext["product_type"]
    if ext.get("size"):
        source_obj["size"] = ext["size"]
    return json.dumps({"source": source_obj, "candidates": cands}, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# JSON extraction helper — imported from ai_recheck to avoid duplication
# --------------------------------------------------------------------------- #

from services.ai_recheck import _extract_json  # noqa: E402


# --------------------------------------------------------------------------- #
# Single-batch AI calls
# --------------------------------------------------------------------------- #

def _call_claude(client, source: dict, batch: list[dict]) -> list[dict]:
    msg     = _build_user_msg(source, batch)
    retries = 3
    for attempt in range(retries):
        try:
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                system=_SYSTEM,
                messages=[{"role": "user", "content": msg}],
                temperature=0,
            )
            raw    = resp.content[0].text if resp.content else "{}"
            parsed = _extract_json(raw)
            items  = parsed.get("results") or []
            while len(items) < len(batch):
                items.append({})
            return [
                {
                    "row_idx":       batch[j]["row_idx"],
                    "asin":          batch[j]["asin"],
                    "ai_verdict":    _safe_v(items[j].get("verdict")),
                    "ai_confidence": _safe_c(items[j].get("confidence")),
                    "ai_reasoning":  (items[j].get("reasoning") or "")[:300],
                }
                for j in range(len(batch))
            ]
        except Exception as exc:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                log.warning("[ai_check] claude batch failed: %s", exc)
                return _error_batch(batch)
    return _error_batch(batch)


def _call_openai(client, source: dict, batch: list[dict]) -> list[dict]:
    msg    = _build_user_msg(source, batch)
    retries = 3
    for attempt in range(retries):
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user",   "content": msg},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            raw    = resp.choices[0].message.content or "{}"
            parsed = json.loads(raw)
            items  = parsed.get("results") or []
            while len(items) < len(batch):
                items.append({})
            return [
                {
                    "row_idx":       batch[j]["row_idx"],
                    "asin":          batch[j]["asin"],
                    "ai_verdict":    _safe_v(items[j].get("verdict")),
                    "ai_confidence": _safe_c(items[j].get("confidence")),
                    "ai_reasoning":  (items[j].get("reasoning") or "")[:300],
                }
                for j in range(len(batch))
            ]
        except Exception as exc:
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
            else:
                log.warning("[ai_check] openai batch failed: %s", exc)
                return _error_batch(batch)
    return _error_batch(batch)


def _safe_v(v) -> str:
    return v if v in ("approve", "reject", "uncertain") else "uncertain"

def _safe_c(c) -> str:
    return c if c in ("high", "medium", "low") else "low"

def _error_batch(batch: list[dict]) -> list[dict]:
    return [
        {
            "row_idx":       c.get("row_idx"),
            "asin":          c["asin"],
            "ai_verdict":    "uncertain",
            "ai_confidence": "low",
            "ai_reasoning":  "AI analysis failed — review manually",
        }
        for c in batch
    ]


# --------------------------------------------------------------------------- #
# Background pipeline
# --------------------------------------------------------------------------- #

_RUNNING: dict[int, bool] = {}
_STOP_EVENTS: dict[int, threading.Event] = {}  # run_id → Event; set() to stop
_RUNNING_LOCK = threading.Lock()


def start_ai_check(run_id: int, verdict_filter: str = "review") -> None:
    """Launch AI check in a background daemon thread. No-op if already running."""
    with _RUNNING_LOCK:
        if _RUNNING.get(run_id):
            return
        _RUNNING[run_id] = True
        # Fresh event for this run (clear any leftover from a previous run)
        ev = _STOP_EVENTS.get(run_id)
        if ev:
            ev.clear()
        else:
            _STOP_EVENTS[run_id] = threading.Event()
    t = threading.Thread(
        target=_pipeline,
        args=(run_id, verdict_filter),
        daemon=True,
    )
    t.start()


def stop_ai_check(run_id: int) -> None:
    """Signal the running AI check for this run to stop immediately."""
    with _RUNNING_LOCK:
        ev = _STOP_EVENTS.get(run_id)
    log.warning("[ai_check] stop_ai_check called for run %s — ev=%s is_set=%s _RUNNING=%s",
                run_id, ev, ev.is_set() if ev else "N/A", _RUNNING.get(run_id))
    if ev:
        ev.set()
        log.warning("[ai_check] stop event SET for run %s", run_id)


def is_running(run_id: int) -> bool:
    with _RUNNING_LOCK:
        return bool(_RUNNING.get(run_id))


def _is_anthropic(client) -> bool:
    return client is not None and hasattr(client, "messages") and not hasattr(client, "chat")


def _pipeline(run_id: int, verdict_filter: str) -> None:
    from services.ai_recheck import make_client
    try:
        client = make_client()
        if not client:
            _set_status(run_id, "Error: no AI API key set (ANTHROPIC_API_KEY or OPENAI_API_KEY)", 0, 0)
            return

        use_claude = _is_anthropic(client)
        _call_batch = _call_claude if use_claude else _call_openai

        # ---- load data -------------------------------------------------- #
        with database._connect() as conn:
            conn.row_factory = sqlite3.Row

            cat_map: dict[int, dict] = {}
            extracted_map: dict[int, dict] = {}
            for r in conn.execute(
                "SELECT row_idx, data_json, extracted_json "
                "FROM analytics_catalog_rows WHERE run_id=?",
                (run_id,),
            ).fetchall():
                try:
                    cat_map[r["row_idx"]] = json.loads(r["data_json"] or "{}")
                except Exception:
                    pass
                try:
                    if r["extracted_json"]:
                        extracted_map[r["row_idx"]] = json.loads(r["extracted_json"])
                except Exception:
                    pass

            q      = ("SELECT row_idx, asin, data_json FROM analytics_candidates "
                      "WHERE run_id=?")
            params: list = [run_id]
            if verdict_filter:
                verdicts = [v.strip() for v in verdict_filter.split(",") if v.strip()]
                if len(verdicts) == 1:
                    q += " AND verdict=?"
                    params.append(verdicts[0])
                elif verdicts:
                    q += f" AND verdict IN ({','.join('?' * len(verdicts))})"
                    params.extend(verdicts)
            q += " ORDER BY row_idx, confidence DESC"
            cand_rows = conn.execute(q, params).fetchall()

        # ---- group by source row ---------------------------------------- #
        by_row: dict[int, list[dict]] = defaultdict(list)
        for c in cand_rows:
            try:
                data = json.loads(c["data_json"] or "{}")
            except Exception:
                data = {}
            by_row[c["row_idx"]].append({
                "row_idx": c["row_idx"],
                "asin":    c["asin"],
                "amazon":  data.get("amazon") or {},
                "data":    data,
            })

        total = sum(len(v) for v in by_row.values())
        _set_status(run_id, "Running", 0, total)

        # ---- build work units ------------------------------------------- #
        work: list[tuple[dict, list[dict]]] = []
        for row_idx, candidates in by_row.items():
            source = dict(cat_map.get(row_idx) or {})
            ext = extracted_map.get(row_idx)
            if ext:
                source["_extracted"] = ext
            for i in range(0, len(candidates), BATCH_SIZE):
                work.append((source, candidates[i:i + BATCH_SIZE]))

        # ---- parallel execution ----------------------------------------- #
        done        = 0
        all_results: list[dict] = []
        stopped     = False

        with _RUNNING_LOCK:
            stop_ev = _STOP_EVENTS.get(run_id)
        log.warning("[ai_check] pipeline got stop_ev=%s for run %s (id=%s)", stop_ev, run_id, id(stop_ev) if stop_ev else None)
        if stop_ev is None:
            stop_ev = threading.Event()   # fallback; shouldn't happen
            log.warning("[ai_check] WARNING: no stop event found for run %s — created fallback", run_id)

        def _run_batch(src: dict, batch: list[dict]) -> list[dict]:
            # Each worker checks the stop event before spending an API call.
            # Queued-but-not-yet-started batches drain instantly when stopped.
            if stop_ev.is_set():
                return _error_batch(batch)
            return _call_batch(client, src, batch)

        # Do NOT use `with ThreadPoolExecutor(...) as pool` — the context
        # manager's __exit__ calls shutdown(wait=True) which blocks until
        # every submitted future drains, defeating the stop button entirely.
        pool = ThreadPoolExecutor(max_workers=MAX_WORKERS)
        try:
            futures: dict = {
                pool.submit(_run_batch, src, batch): batch
                for src, batch in work
            }
            pending = set(futures.keys())

            while pending:
                # stop_ev.wait(0.5) blocks for up to 0.5 s BUT returns True
                # immediately the instant stop_ai_check() calls ev.set().
                if stop_ev.wait(timeout=0.5):
                    log.info("[ai_check] stop event fired for run %s — flushing partial results", run_id)
                    stopped = True
                    break

                # Collect whichever futures finished during the wait window.
                finished, pending = wait(pending, timeout=0,
                                         return_when=FIRST_COMPLETED)
                for fut in finished:
                    try:
                        batch_results = fut.result()
                    except Exception:
                        batch_results = _error_batch(futures[fut])
                    all_results.extend(batch_results)
                    done += len(futures[fut])
                    _set_status(run_id, "Running", done, total)
        finally:
            # Cancel pending futures; let the 4 in-flight workers finish on
            # their own — shutdown(wait=False) returns immediately.
            pool.shutdown(wait=False, cancel_futures=True)

        _flush(run_id, all_results)
        if stopped:
            _set_status(run_id, "Stopped", done, total)
        else:
            _set_status(run_id, "Done", total, total)

    except Exception as exc:
        log.exception("[ai_check] pipeline error: %s", exc)
        _set_status(run_id, f"Error: {exc}", 0, 0)
    finally:
        with _RUNNING_LOCK:
            _RUNNING.pop(run_id, None)


def _set_status(run_id: int, status: str, done: int, total: int) -> None:
    with database._LOCK, database._connect() as conn:
        conn.execute(
            "UPDATE analytics_runs "
            "SET ai_check_status=?, ai_check_done=?, ai_check_total=? "
            "WHERE id=?",
            (status, done, total, run_id),
        )


def _flush(run_id: int, results: list[dict]) -> None:
    with database._LOCK, database._connect() as conn:
        conn.executemany(
            "UPDATE analytics_candidates "
            "SET ai_verdict=?, ai_reasoning=? "
            "WHERE run_id=? AND row_idx=? AND asin=?",
            [
                (r["ai_verdict"], r["ai_reasoning"], run_id, r["row_idx"], r["asin"])
                for r in results
            ],
        )
