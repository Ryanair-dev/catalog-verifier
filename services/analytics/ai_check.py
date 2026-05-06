"""
Analytics AI Check — GPT-4o-mini batch verification of analytics candidates.

For each source catalog row, groups all its scored candidates and sends them
(up to BATCH_SIZE per OpenAI call) asking GPT-4o-mini to decide which ones
actually match the vendor item.  Results are stored as ai_verdict /
ai_reasoning on analytics_candidates and are shown as badge overlays in the UI
without changing the scored verdict.

Progress is tracked via three columns on analytics_runs:
  ai_check_status  TEXT  — null | "Running" | "Done" | "Error: ..."
  ai_check_done    INT   — candidates processed so far
  ai_check_total   INT   — total candidates to process
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import defaultdict

from services import database

log = logging.getLogger(__name__)

BATCH_SIZE   = 10   # candidates per OpenAI call
MAX_WORKERS  = 4    # parallel OpenAI calls

# Cost/latency constants matching ai_recheck.py conventions
AVG_INPUT_TOKENS  = 350   # slightly more than single-row because we send multiple candidates
AVG_OUTPUT_TOKENS = 100
AVG_LATENCY_MS    = 900

PRICING = {
    "gpt-4o-mini": {"input": 0.00015, "output": 0.00060},
    "gpt-4o":      {"input": 0.00250, "output": 0.01000},
}


# --------------------------------------------------------------------------- #
# Cost estimation
# --------------------------------------------------------------------------- #

def estimate_cost(candidate_count: int, model: str = "gpt-4o-mini") -> dict:
    batches   = (candidate_count + BATCH_SIZE - 1) // BATCH_SIZE
    price     = PRICING.get(model) or PRICING["gpt-4o-mini"]
    in_tok    = batches * AVG_INPUT_TOKENS
    out_tok   = batches * AVG_OUTPUT_TOKENS
    cost_usd  = (in_tok / 1000) * price["input"] + (out_tok / 1000) * price["output"]
    duration  = batches * AVG_LATENCY_MS / MAX_WORKERS   # rough parallel estimate
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
    "For each candidate decide if it is the SAME physical product as the vendor item.\n\n"
    "Rules:\n"
    "- Brand: determine brand from the Amazon TITLE first — the structured Brand field is "
    "often wrong due to Amazon data quality issues. If the title clearly contains the vendor "
    "brand, treat it as a brand match even if the Brand field says something else.\n"
    "- Product type must match (shampoo ≠ conditioner)\n"
    "- Size must match within 10% (8 oz ≠ 16 oz; 8 oz ≈ 236 ml is fine). "
    "When the Amazon title lists a size alongside a pack count (e.g. '3.25 oz (2 Pack)'), "
    "the size is PER UNIT — compare it directly to the vendor size, do NOT multiply by pack count.\n"
    "- Pack differences (single vs multipack) → 'uncertain', not reject\n"
    "- Scent/flavor differences → reject only when BOTH sides specify it AND they conflict\n\n"
    "Respond ONLY with a JSON object:\n"
    '{"results": [{"asin":"...","verdict":"approve|reject|uncertain",'
    '"confidence":"high|medium|low","reasoning":"<25 words"}]}\n'
    "One entry per candidate, IN THE SAME ORDER as the input."
)


def _fmt_candidate(i: int, c: dict) -> str:
    amz   = c.get("amazon") or {}
    attrs = amz.get("attributes") or {}
    lines = [
        f"[{i + 1}] ASIN: {c['asin']}",
        f"    Title: {amz.get('title') or '-'}",
        f"    Brand: {amz.get('brand') or amz.get('manufacturer') or '-'}",
    ]
    if attrs.get("size"):  lines.append(f"    Size: {attrs['size']}")
    if attrs.get("color"): lines.append(f"    Color: {attrs['color']}")
    return "\n".join(lines)


def _build_user_msg(source: dict, batch: list[dict]) -> str:
    cands = "\n\n".join(_fmt_candidate(i, c) for i, c in enumerate(batch))
    return json.dumps({
        "source": {
            "title": source.get("title") or "-",
            "brand": source.get("brand") or "-",
            "upc":   source.get("upc")   or "-",
        },
        "candidates": cands,
    }, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# Single-batch OpenAI call
# --------------------------------------------------------------------------- #

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
            # Pad/trim to match batch length
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
                log.warning("[ai_check] batch failed: %s", exc)
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


def start_ai_check(run_id: int, verdict_filter: str = "review") -> None:
    """Launch AI check in a background daemon thread. No-op if already running."""
    if _RUNNING.get(run_id):
        return
    t = threading.Thread(
        target=_pipeline,
        args=(run_id, verdict_filter),
        daemon=True,
    )
    t.start()


def is_running(run_id: int) -> bool:
    return bool(_RUNNING.get(run_id))


def _pipeline(run_id: int, verdict_filter: str) -> None:
    from services.ai_recheck import make_client
    _RUNNING[run_id] = True
    try:
        client = make_client()
        if not client:
            _set_status(run_id, "Error: OPENAI_API_KEY not set", 0, 0)
            return

        # ---- load data -------------------------------------------------- #
        with database._connect() as conn:
            conn.row_factory = sqlite3.Row

            cat_map: dict[int, dict] = {}
            for r in conn.execute(
                "SELECT row_idx, data_json FROM analytics_catalog_rows WHERE run_id=?",
                (run_id,),
            ).fetchall():
                try:
                    cat_map[r["row_idx"]] = json.loads(r["data_json"] or "{}")
                except Exception:
                    pass

            q      = ("SELECT row_idx, asin, data_json FROM analytics_candidates "
                      "WHERE run_id=?")
            params: list = [run_id]
            if verdict_filter:
                q += " AND verdict=?"
                params.append(verdict_filter)
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
            })

        total = sum(len(v) for v in by_row.values())
        _set_status(run_id, "Running", 0, total)

        # ---- build work units (source_row, batch) ----------------------- #
        work: list[tuple[dict, list[dict]]] = []
        for row_idx, candidates in by_row.items():
            source = cat_map.get(row_idx) or {}
            for i in range(0, len(candidates), BATCH_SIZE):
                work.append((source, candidates[i:i + BATCH_SIZE]))

        # ---- parallel execution ----------------------------------------- #
        done      = 0
        all_results: list[dict] = []

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = {
                pool.submit(_call_openai, client, src, batch): batch
                for src, batch in work
            }
            for fut in as_completed(futures):
                batch_results = fut.result()
                all_results.extend(batch_results)
                done += len(futures[fut])
                _set_status(run_id, "Running", done, total)

        _flush(run_id, all_results)
        _set_status(run_id, "Done", total, total)

    except Exception as exc:
        log.exception("[ai_check] pipeline error: %s", exc)
        _set_status(run_id, f"Error: {exc}", 0, 0)
    finally:
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
