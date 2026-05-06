"""
Brand field extraction for analytics runs.

Before searching Amazon, runs GPT-4o-mini on each vendor product title to
extract structured fields: brand, product_type, model, size, pack_info.
These fields are stored per catalog row and used to:
  - Build more targeted Tier 3 keyword queries (brand + product_type)
  - Improve brand scoring in the matcher (extracted brand > raw catalog text)
  - Provide richer context to the AI verdict step

Mirrors AmazonAsinResearch1/app/services/brand_extractor.py but uses
OpenAI instead of Anthropic, and stores results in analytics_catalog_rows.
"""
from __future__ import annotations

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

log = logging.getLogger(__name__)

BATCH_SIZE = 20        # titles per GPT-4o-mini call
MAX_WORKERS = 4        # parallel calls

_SYSTEM = (
    "You are a CPG product data extractor. Given a numbered list of vendor "
    "product titles, extract structured fields for each one.\n\n"
    "For every title return:\n"
    "  brand        — the brand/manufacturer name (e.g. 'Old Spice', 'Dawn')\n"
    "  product_type — the product category (e.g. 'deodorant', 'dish soap')\n"
    "  model        — specific variant/model excluding brand and type "
    "(e.g. 'Classic Original Scent', 'Ultra Platinum'); null if none\n"
    "  size         — size/volume/weight string (e.g. '3.4 oz', '56 fl oz'); "
    "null if absent\n"
    "  pack_info    — pack count if listed (e.g. 'pack of 3'); null if single\n\n"
    "Respond ONLY with a JSON array — one object per title in the SAME ORDER:\n"
    '[{"brand":"...","product_type":"...","model":null,"size":"...","pack_info":null}]'
)


def _extract_batch(client, titles: list[str]) -> list[dict]:
    """
    Call GPT-4o-mini for a batch of titles.
    Returns a list of dicts (same length as titles), with empty dicts on error.
    """
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(titles))
    for attempt in range(3):
        try:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": _SYSTEM},
                    {"role": "user",   "content": numbered},
                ],
                response_format={"type": "json_object"},
                temperature=0,
            )
            raw = resp.choices[0].message.content or "{}"
            # Model sometimes wraps the array in {"results": [...]}
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                items = parsed
            else:
                items = (
                    parsed.get("results")
                    or parsed.get("items")
                    or parsed.get("data")
                    or []
                )
                if not isinstance(items, list):
                    items = []
            # Pad / trim to match batch length
            while len(items) < len(titles):
                items.append({})
            return [_normalise(items[i]) for i in range(len(titles))]
        except Exception as exc:
            if attempt < 2:
                time.sleep(2 ** attempt)
            else:
                log.warning("[brand_extractor] batch failed: %s", exc)
    return [{} for _ in titles]


def _normalise(raw: dict) -> dict:
    """Coerce raw GPT output to a clean, predictable shape."""
    def _s(v) -> str:
        return str(v).strip() if v else ""

    return {
        "brand":        _s(raw.get("brand")),
        "product_type": _s(raw.get("product_type")),
        "model":        _s(raw.get("model")),
        "size":         _s(raw.get("size")),
        "pack_info":    _s(raw.get("pack_info")),
    }


def extract_brands(
    rows: list,          # list of SourceRow
    run_id: int = 0,
    progress_cb=None,    # optional callable(done, total)
) -> dict[int, dict]:
    """
    Extract brand fields for all rows that have a title.
    Returns {row_idx: {brand, product_type, model, size, pack_info}}.
    Rows without a title are skipped (empty dict).
    """
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        log.info("[brand_extractor] OPENAI_API_KEY not set — skipping extraction")
        return {}

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
    except ImportError:
        log.warning("[brand_extractor] openai package not installed — skipping")
        return {}

    # Build ordered list of (row_idx, title) for rows that have a title
    items_to_extract = [(r.row_idx, r.title) for r in rows if r.title]
    total = len(items_to_extract)
    if not total:
        return {}

    result: dict[int, dict] = {}
    done = 0

    # Split into batches and submit in parallel
    batches = [
        items_to_extract[i:i + BATCH_SIZE]
        for i in range(0, total, BATCH_SIZE)
    ]

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        future_to_batch = {
            pool.submit(_extract_batch, client, [t for _, t in batch]): batch
            for batch in batches
        }
        for fut in as_completed(future_to_batch):
            batch = future_to_batch[fut]
            try:
                extracted_list = fut.result()
            except Exception as exc:
                log.warning("[brand_extractor] future error: %s", exc)
                extracted_list = [{} for _ in batch]
            for (row_idx, _), extracted in zip(batch, extracted_list):
                result[row_idx] = extracted
            done += len(batch)
            if progress_cb:
                progress_cb(done, total)

    log.info("[brand_extractor] extracted %d/%d rows for run %s", len(result), total, run_id)
    return result
