"""AI cleanup for Amazon candidate titles used during matching."""
from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

from services.analytics.matching_core.agents.keyword_prompts import AMAZON_TITLE_CLEANER_PROMPT

log = logging.getLogger(__name__)

BATCH_SIZE = 20
MAX_WORKERS = 3
MODEL = "claude-haiku-4-5-20251001"
PRICE_INPUT_PER_MILLION = 0.80
PRICE_OUTPUT_PER_MILLION = 4.00


def _batches(items: list[str], size: int):
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _clean_batch_with_usage(
    client,
    titles: list[str],
    run_id: int | None = None,
    require_usage: bool = True,
) -> tuple[list[str], int, int]:
    numbered = "\n".join(
        f"{i + 1}. {json.dumps({'title': title}, ensure_ascii=False)}"
        for i, title in enumerate(titles)
    )
    response = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=AMAZON_TITLE_CLEANER_PROMPT,
        messages=[{"role": "user", "content": numbered}],
    )
    usage = getattr(response, "usage", None)
    if usage is None and require_usage:
        raise ValueError("Amazon title cleaner response omitted token usage")
    tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
    tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
    if run_id is not None and usage is not None:
        _record_batch_usage(run_id, titles, tokens_in, tokens_out)

    raw = response.content[0].text if response.content else ""
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        fenced = re.search(r"```(?:json)?\s*([\s\S]+?)```", raw)
        if not fenced:
            raise ValueError("Amazon title cleaner returned invalid JSON")
        parsed = json.loads(fenced.group(1).strip())
    items = parsed if isinstance(parsed, list) else (
        parsed.get("results") or parsed.get("items") or []
        if isinstance(parsed, dict) else []
    )
    if len(items) != len(titles) or any(not isinstance(item, dict) for item in items):
        raise ValueError(
            f"Amazon title cleaner returned {len(items)} rows; expected {len(titles)}"
        )
    cleaned = [str(item.get("cleaned_title") or "").strip() for item in items]
    if any(not title for title in cleaned):
        raise ValueError("Amazon title cleaner omitted cleaned_title")
    return cleaned, tokens_in, tokens_out


def _record_batch_usage(run_id: int, titles: list[str], tokens_in: int, tokens_out: int) -> None:
    from services import database

    cost = (
        tokens_in * PRICE_INPUT_PER_MILLION
        + tokens_out * PRICE_OUTPUT_PER_MILLION
    ) / 1_000_000
    with database._LOCK, database._connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS amazon_title_clean_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                titles INTEGER NOT NULL,
                title_chars INTEGER NOT NULL,
                tokens_in INTEGER NOT NULL,
                tokens_out INTEGER NOT NULL,
                cost_usd REAL NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "INSERT INTO amazon_title_clean_usage "
            "(run_id, titles, title_chars, tokens_in, tokens_out, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, len(titles), sum(len(title) for title in titles),
             tokens_in, tokens_out, cost),
        )


def run_usage(run_id: int) -> dict[str, int | float]:
    from services import database

    with database._LOCK, database._connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS amazon_title_clean_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                titles INTEGER NOT NULL,
                title_chars INTEGER NOT NULL,
                tokens_in INTEGER NOT NULL,
                tokens_out INTEGER NOT NULL,
                cost_usd REAL NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        row = conn.execute(
            "SELECT COALESCE(SUM(titles), 0) AS titles, "
            "COALESCE(SUM(title_chars), 0) AS title_chars, "
            "COALESCE(SUM(tokens_in), 0) AS tokens_in, "
            "COALESCE(SUM(tokens_out), 0) AS tokens_out, "
            "COALESCE(SUM(cost_usd), 0) AS cost_usd "
            "FROM amazon_title_clean_usage WHERE run_id=?",
            (run_id,),
        ).fetchone()
    return {
        "titles": int(row["titles"]),
        "title_chars": int(row["title_chars"]),
        "tokens_in": int(row["tokens_in"]),
        "tokens_out": int(row["tokens_out"]),
        "cost_usd": round(float(row["cost_usd"]), 6),
    }


def _clean_batch(client, titles: list[str]) -> list[str]:
    return _clean_batch_with_usage(client, titles, require_usage=False)[0]


def clean_amazon_titles(titles: list[str], progress_cb=None, run_id: int | None = None) -> dict[str, str]:
    """Return original-title -> cleaned-title, preserving originals on failed batches."""
    titles = list(dict.fromkeys(title.strip() for title in titles if title.strip()))
    if not titles:
        return {}
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY is not configured")
    try:
        import anthropic
    except ImportError as exc:
        raise RuntimeError("The anthropic package is not installed") from exc

    client = anthropic.Anthropic(api_key=api_key)
    batches = list(_batches(titles, BATCH_SIZE))
    result: dict[str, str] = {}
    failures: list[Exception] = []
    done = 0
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(_clean_batch_with_usage, client, batch, run_id): batch
            for batch in batches
        }
        for future in as_completed(futures):
            batch = futures[future]
            try:
                cleaned, tokens_in, tokens_out = future.result()
                result.update(zip(batch, cleaned))
            except Exception as exc:
                failures.append(exc)
                log.exception("Amazon-title cleanup batch failed")
            done += len(batch)
            if progress_cb:
                progress_cb(done, len(titles))
    if failures:
        raise RuntimeError(
            f"Amazon-title cleanup failed for {len(failures)} batch(es): {failures[0]}"
        ) from failures[0]
    return result
