"""
Match feedback: a person reports one candidate ("same / different product, and why").
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import os
import subprocess
from contextlib import closing
from pathlib import Path
from typing import Any

from services import database

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Reason lists (edit here; the frontend loads them from /api/feedback/reasons)
# --------------------------------------------------------------------------- #

REASONS_DIFFERENT = [
    ("pack_size", "Different pack size / count"),
    ("size_volume", "Different size or volume"),
    ("variant", "Different variant (scent, flavor, color, lid type...)"),
    ("brand", "Different brand"),
    ("different_product", "Completely different product"),
    ("id_mismatch", "UPC / part number belongs to a different product"),
    ("other", "Other (explain in the note)"),
]
REASONS_SAME = [
    ("same_product_wording", "Same product, just worded differently"),
    ("vendor_title_unclear", "Vendor title is abbreviated or unclear"),
    ("amazon_title_messy", "Amazon title is messy or keyword-stuffed"),
    ("pack_equivalent", "Pack difference is fine (same product)"),
    ("other", "Other (explain in the note)"),
]
REASON_LABELS = {code: label for code, label in REASONS_DIFFERENT + REASONS_SAME}

_DDL = """
CREATE TABLE IF NOT EXISTS match_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
    kind TEXT NOT NULL,
    strength TEXT NOT NULL,
    run_id INTEGER, row_idx INTEGER, asin TEXT,
    run_name TEXT, vetting_mode TEXT,
    label INTEGER,
    reasons TEXT, note TEXT, correct_asin TEXT, reported_by TEXT,
    vendor_title TEXT, vendor_title_scored TEXT, vendor_brand TEXT,
    vendor_upc TEXT, vendor_mpn TEXT,
    amazon_title TEXT, amazon_brand TEXT, amazon_upc TEXT, amazon_mpn TEXT,
    amazon_pack TEXT, amazon_json TEXT,
    scoring_method TEXT, confidence REAL, verdict TEXT, scorer_verdict TEXT,
    scorer_reasons TEXT, rule TEXT, audit_json TEXT,
    code_version TEXT, flags_json TEXT, model_hash TEXT, scores_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_mf_run    ON match_feedback(run_id);
"""

_REC_COLS = [
    "kind", "strength", "run_id", "row_idx", "asin", "run_name", "vetting_mode",
    "label", "reasons", "note", "correct_asin", "reported_by",
    "vendor_title", "vendor_title_scored", "vendor_brand", "vendor_upc", "vendor_mpn",
    "amazon_title", "amazon_brand", "amazon_upc", "amazon_mpn", "amazon_pack", "amazon_json",
    "scoring_method", "confidence", "verdict", "scorer_verdict", "scorer_reasons",
    "rule", "audit_json", "code_version", "flags_json", "model_hash", "scores_json",
]


def ensure_table() -> None:
    with database._LOCK, database._connect() as conn:
        conn.executescript(_DDL)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(match_feedback)")}
        if "scores_json" not in cols:        # table was created by an earlier version
            conn.execute("ALTER TABLE match_feedback ADD COLUMN scores_json TEXT")


ensure_table()


# --------------------------------------------------------------------------- #
# Version stamps: which code / model / switches produced the matcher's answer
# --------------------------------------------------------------------------- #

_cache: dict[str, Any] = {}


def _code_version() -> str:
    if "code" in _cache:
        return _cache["code"]
    v = os.getenv("APP_VERSION", "").strip()
    if not v:
        try:
            out = subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=str(Path(__file__).resolve().parent.parent),
                capture_output=True, text=True, timeout=3,
            )
            v = out.stdout.strip() if out.returncode == 0 else ""
        except Exception:  # noqa: BLE001  (git missing on the VM is fine)
            v = ""
    _cache["code"] = v or "unknown"
    return _cache["code"]


def _model_hash() -> str:
    try:
        from services.analytics.matching_core.matching import classifier as clf
        p = Path(os.environ.get("CLASSIFIER_MODEL_PATH", clf.DEFAULT_MODEL_PATH))
        if not p.is_file():
            return ""
        key = (str(p), p.stat().st_mtime)
        if _cache.get("model_key") != key:
            _cache["model_key"] = key
            _cache["model_hash"] = hashlib.sha1(p.read_bytes()).hexdigest()[:10]
        return _cache["model_hash"]
    except Exception:  # noqa: BLE001
        return ""


def _flags_json() -> str:
    try:
        from services.analytics.matching_core.matching import flags
        return json.dumps({k: getattr(flags, k) for k in dir(flags) if k.isupper()})
    except Exception:  # noqa: BLE001
        return ""


# --------------------------------------------------------------------------- #
# Snapshot building
# --------------------------------------------------------------------------- #

_SELECT = """
SELECT c.row_idx, c.asin, c.verdict, c.confidence, c.review_status, c.sources, c.amz_pack,
       c.data_json AS cand_json,
       r.data_json AS src_json, r.extracted_json, r.query_agent_json
FROM analytics_candidates c
LEFT JOIN analytics_catalog_rows r ON r.run_id = c.run_id AND r.row_idx = c.row_idx
WHERE c.run_id = ? {extra}
"""

_AMZ_KEEP = (
    "asin", "title", "brand", "manufacturer", "upc", "ean", "gtin", "mpn", "attributes",
    "bullet_points", "description", "item_package_quantity", "number_of_items", "sales_rank",
)


def _j(text: Any, default: Any) -> Any:
    if not text:
        return default
    try:
        v = json.loads(text)
    except (ValueError, TypeError):
        return default
    return v if isinstance(v, type(default)) else default


def _fetch_rows(conn, run_id: int, extra: str = "", params: tuple = ()) -> list:
    return conn.execute(_SELECT.format(extra=extra), (run_id, *params)).fetchall()


def _run_info(conn, run_id: int) -> dict | None:
    r = conn.execute(
        "SELECT id, name, vetting_mode, scoring_method FROM analytics_runs WHERE id=?",
        (run_id,),
    ).fetchone()
    return dict(r) if r else None


def _slim_amazon(amz: dict) -> dict:
    out = {k: amz.get(k) for k in _AMZ_KEEP if amz.get(k) not in (None, "", [], {})}
    if isinstance(out.get("description"), str):
        out["description"] = out["description"][:1500]
    if isinstance(out.get("bullet_points"), list):
        out["bullet_points"] = [str(b)[:300] for b in out["bullet_points"][:6]]
    return out


def _slim_scores(scores: dict) -> dict:
    keep = ("confidence_score", "scoring_method", "scoring_verdict", "scorer_verdict",
            "signals", "reasons", "upc_match", "brand_confirmed")
    out = {k: scores[k] for k in keep if k in scores}
    ex = scores.get("explanation")
    if isinstance(ex, dict):
        out["explanation"] = {"summary": ex.get("summary", ""), "factors": ex.get("factors", [])}
    out.update({k: v for k, v in scores.items() if k.endswith("_mismatch") and v})
    return out


def _build_record(run: dict, row) -> dict:
    cd = _j(row["cand_json"], {})
    amz = cd.get("amazon") if isinstance(cd.get("amazon"), dict) else {}
    scores = cd.get("scores") if isinstance(cd.get("scores"), dict) else {}
    audit = cd.get("verification_audit") if isinstance(cd.get("verification_audit"), dict) else {}
    src = _j(row["src_json"], {})
    ex = _j(row["extracted_json"], {})
    qa = _j(row["query_agent_json"], {})

    # The title the scorers actually compared (same rule the approval auditor uses).
    title_scored = qa.get("comparison_title") or ex.get("normalized_title") or src.get("title") or ""
    reasons = scores.get("reasons") if isinstance(scores.get("reasons"), list) else []
    rule = next((str(r)[5:] for r in reasons if str(r).startswith("rule:")), "")

    return {
        "run_id": run["id"], "row_idx": row["row_idx"], "asin": row["asin"],
        "run_name": run.get("name") or "", "vetting_mode": run.get("vetting_mode") or "",
        "vendor_title": src.get("title", "") or "", "vendor_title_scored": title_scored,
        "vendor_brand": src.get("brand") or ex.get("brand") or "",
        "vendor_upc": src.get("upc", "") or "", "vendor_mpn": src.get("itemid", "") or "",
        "amazon_title": amz.get("title") or "",
        "amazon_brand": amz.get("brand") or amz.get("manufacturer") or "",
        "amazon_upc": amz.get("upc") or amz.get("ean") or amz.get("gtin") or "",
        "amazon_mpn": amz.get("mpn") or "",
        "amazon_pack": "" if row["amz_pack"] is None else str(row["amz_pack"]),
        "amazon_json": json.dumps(_slim_amazon(amz), default=str),
        "scoring_method": scores.get("scoring_method") or run.get("scoring_method") or "",
        "confidence": row["confidence"], "verdict": row["verdict"],
        "scorer_verdict": scores.get("scorer_verdict") or scores.get("scoring_verdict") or "",
        "scorer_reasons": json.dumps(reasons, default=str), "rule": rule,
        "audit_json": json.dumps(audit, default=str) if audit else "",
        "code_version": _code_version(), "flags_json": _flags_json(), "model_hash": _model_hash(),
        "scores_json": json.dumps(_slim_scores(scores), default=str),
    }


def _insert(conn, rec: dict):
    cols = _REC_COLS
    return conn.execute(
        f"INSERT INTO match_feedback({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        [rec.get(c) for c in cols],
    )


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #

def record_report(*, run_id: int, row_idx: int, asin: str, label: int | None,
                  reasons: list[str], note: str, correct_asin: str, reported_by: str) -> int:
    with closing(database._connect()) as conn:
        run = _run_info(conn, run_id)
        rows = _fetch_rows(conn, run_id, "AND c.row_idx=? AND c.asin=?", (row_idx, asin))
    if not run or not rows:
        raise LookupError("That candidate no longer exists in this run.")
    rec = _build_record(run, rows[0])
    rec.update(kind="report", strength="report", label=label, reasons=json.dumps(reasons),
               note=note, correct_asin=correct_asin, reported_by=reported_by)
    with database._LOCK, closing(database._connect()) as conn:
        with conn:
            return int(_insert(conn, rec).lastrowid)


# --------------------------------------------------------------------------- #
# Developer access: one CSV with everything
# --------------------------------------------------------------------------- #

_CSV_COLS = [
    "id", "created_at", "kind", "strength", "run_id", "run_name", "row", "asin",
    "label", "reasons", "note", "correct_asin", "reported_by",
    "vendor_title", "vendor_title_scored", "vendor_brand", "vendor_upc", "vendor_mpn",
    "amazon_title", "amazon_brand", "amazon_upc", "amazon_mpn", "amazon_pack",
    "scoring_method", "confidence", "verdict", "scorer_verdict", "rule", "scorer_reasons",
    "matcher_explanation", "vetting_mode", "code_version", "model_hash", "flags_json",
]


def export_csv() -> str:
    with closing(database._connect()) as conn:
        rows = conn.execute("SELECT * FROM match_feedback ORDER BY id DESC").fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(_CSV_COLS)
    for r in rows:
        d = dict(r)
        d["row"] = (d.get("row_idx") or 0) + 1
        d["label"] = {1: "same", 0: "different"}.get(d.get("label"), "")
        d["reasons"] = "; ".join(REASON_LABELS.get(c, c) for c in _j(d.get("reasons"), []))
        sc = _j(d.get("scores_json"), {})
        ex = sc.get("explanation") if isinstance(sc.get("explanation"), dict) else {}
        parts = [ex.get("summary", "")] + [str(f) for f in (ex.get("factors") or [])]
        mism = [k.replace("_mismatch", "") for k, v in sc.items() if k.endswith("_mismatch") and v]
        if mism:
            parts.append("mismatch: " + ", ".join(mism))
        d["matcher_explanation"] = " | ".join(p for p in parts if p)
        w.writerow([d.get(c, "") for c in _CSV_COLS])
    return buf.getvalue()