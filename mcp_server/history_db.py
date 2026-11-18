from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from services.database import DB_PATH


_MAX_MATCHES = 25
_SOURCE_FIELDS = ("upc", "itemid", "title", "brand")
_AMAZON_FIELDS = (
    "title", "brand", "sales_rank", "list_price", "storage_fee_offpeak",
    "storage_fee_peak",
)


def _database_path() -> Path:
    configured = os.getenv("CATALOG_VERIFIER_DB_PATH")
    return Path(configured).expanduser().resolve() if configured else DB_PATH


def _connect_readonly() -> sqlite3.Connection:
    path = _database_path()
    if not path.is_file():
        raise FileNotFoundError(
            f"Catalog Verifier database not found at {path}. "
            "Set CATALOG_VERIFIER_DB_PATH if the app uses another location."
        )
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def _decode_json(value: str | None, field: str) -> dict[str, Any]:
    try:
        result = json.loads(value or "{}")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Saved Offer Analysis {field} is not valid JSON.") from exc
    if not isinstance(result, dict):
        raise ValueError(f"Saved Offer Analysis {field} has an unexpected shape.")
    return result


def _shape_match(row: sqlite3.Row) -> dict[str, Any]:
    source = _decode_json(row["source_json"], "catalog row")
    candidate_json = row["candidate_json"]
    candidate = None
    if candidate_json is not None:
        saved = _decode_json(candidate_json, "candidate")
        scores = saved.get("scores")
        amazon = saved.get("amazon")
        candidate = {
            "asin": row["asin"],
            "verdict": row["verdict"],
            "confidence": row["confidence"],
            "review_status": row["review_status"],
            "sources": _decode_json_list(row["sources"], "candidate sources"),
            "scores": scores if isinstance(scores, dict) else {},
            "amazon": {
                key: amazon[key]
                for key in _AMAZON_FIELDS
                if isinstance(amazon, dict) and key in amazon
            },
        }
    return {
        "run_id": row["run_id"],
        "run_name": row["run_name"],
        "run_status": row["run_status"],
        "run_created_at": row["run_created_at"],
        "row": row["row_idx"] + 1,
        "catalog_item": {key: source.get(key) for key in _SOURCE_FIELDS},
        "candidate": candidate,
    }


def _decode_json_list(value: str | None, field: str) -> list[Any]:
    try:
        result = json.loads(value or "[]")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Saved Offer Analysis {field} is not valid JSON.") from exc
    if not isinstance(result, list):
        raise ValueError(f"Saved Offer Analysis {field} has an unexpected shape.")
    return result


_MATCH_SELECT = """
    SELECT r.id AS run_id, r.name AS run_name, r.status AS run_status,
           r.created_at AS run_created_at, cr.row_idx, cr.data_json AS source_json,
           c.asin, c.verdict, c.confidence, c.review_status, c.sources,
           c.data_json AS candidate_json
    FROM analytics_catalog_rows cr
    JOIN analytics_runs r ON r.id=cr.run_id
    LEFT JOIN analytics_candidates c
      ON c.run_id=cr.run_id AND c.row_idx=cr.row_idx
"""


def search_saved_matches(identifier: str, identifier_type: str) -> list[dict[str, Any]]:
    """Read historical Offer Analysis rows and candidates without modifying the DB."""
    identifier = identifier.strip()
    if not identifier:
        raise ValueError("Provide a search value.")
    if identifier_type == "asin":
        where = "upper(c.asin)=upper(?)"
    elif identifier_type == "upc":
        where = "lower(trim(json_extract(cr.data_json, '$.upc')))=lower(?)"
    elif identifier_type == "item_id":
        where = "lower(trim(json_extract(cr.data_json, '$.itemid')))=lower(?)"
    else:
        raise ValueError("identifier_type must be asin, upc, or item_id.")

    sql = (
        _MATCH_SELECT
        + f" WHERE {where}"
        + " ORDER BY r.created_at DESC, r.id DESC, cr.row_idx, c.confidence DESC"
        + f" LIMIT {_MAX_MATCHES}"
    )
    try:
        with closing(_connect_readonly()) as connection:
            rows = connection.execute(sql, (identifier,)).fetchall()
    except sqlite3.Error as exc:
        raise RuntimeError(f"Could not read saved Offer Analysis results: {exc}") from exc
    return [_shape_match(row) for row in rows]


def explain_saved_pair(run_id: int, row_number: int, asin: str) -> dict[str, Any]:
    """Return the saved scorer output for one candidate; never recompute a verdict."""
    if run_id < 1 or row_number < 1:
        raise ValueError("run_id and row must be positive integers.")
    asin = asin.strip().upper()
    if not asin:
        raise ValueError("Provide an ASIN.")
    sql = _MATCH_SELECT + """
        WHERE r.id=? AND cr.row_idx=? AND upper(c.asin)=?
        LIMIT 1
    """
    try:
        with closing(_connect_readonly()) as connection:
            row = connection.execute(sql, (run_id, row_number - 1, asin)).fetchone()
    except sqlite3.Error as exc:
        raise RuntimeError(f"Could not read the saved Offer Analysis result: {exc}") from exc
    if row is None:
        raise LookupError(
            f"No saved candidate found for run {run_id}, row {row_number}, ASIN {asin}."
        )
    match = _shape_match(row)
    return {
        "run_id": match["run_id"],
        "run_name": match["run_name"],
        "run_status": match["run_status"],
        "run_created_at": match["run_created_at"],
        "row": match["row"],
        "catalog_item": match["catalog_item"],
        "candidate": match["candidate"],
        "note": "This is the saved scorer output; no verdict was recalculated.",
    }
