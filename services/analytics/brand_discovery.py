"""
AI sub-brand discovery.

Given a brand or manufacturer name, asks Claude (or GPT-4o-mini fallback) to
enumerate known sub-brands and aliases. Returns a structured dict.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from services.ai_recheck import _extract_json

log = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are a consumer goods brand intelligence assistant. "
    "Given a brand or manufacturer name, return a JSON object with these fields:\n"
    "  entity       : the canonical name (cleaned/corrected spelling)\n"
    "  entity_type  : 'brand' or 'manufacturer'\n"
    "  sub_brands   : JSON array of distinct sub-brand names (strings)\n"
    "  aliases      : JSON array of alternate spellings / abbreviations\n"
    "  confidence   : 'high', 'medium', or 'low'\n\n"
    "Rules:\n"
    "- For a MANUFACTURER, sub_brands should list all consumer-facing brand names it owns.\n"
    "- For a BRAND, sub_brands should list product lines or sub-brands under it.\n"
    "- Include the entity itself in sub_brands when it's also a searchable brand name.\n"
    "- Return only the raw JSON object — no markdown, no prose."
)


def _is_anthropic(client: Any) -> bool:
    return client is not None and hasattr(client, "messages") and not hasattr(client, "chat")


def discover_brands(name: str, entity_type: str, client: Any) -> dict:
    """
    Ask AI to return sub-brands and aliases for a given brand/manufacturer.
    Returns a dict with entity, entity_type, sub_brands, aliases, confidence.
    Falls back to a minimal stub if AI is unavailable.
    """
    if client is None:
        return {
            "entity": name,
            "entity_type": entity_type,
            "sub_brands": [name],
            "aliases": [],
            "confidence": "low",
            "error": "No AI client available",
        }

    user_msg = json.dumps({"name": name, "entity_type": entity_type})

    try:
        if _is_anthropic(client):
            resp = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=512,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
                temperature=0,
            )
            raw = resp.content[0].text if resp.content else "{}"
        else:
            resp = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg},
                ],
                max_tokens=512,
                temperature=0,
            )
            raw = resp.choices[0].message.content or "{}"

        parsed = _extract_json(raw)
    except Exception as exc:
        log.warning("brand_discovery failed for %r: %s", name, exc)
        return {
            "entity": name,
            "entity_type": entity_type,
            "sub_brands": [name],
            "aliases": [],
            "confidence": "low",
            "error": str(exc),
        }

    result = {
        "entity":      str(parsed.get("entity") or name).strip(),
        "entity_type": str(parsed.get("entity_type") or entity_type).strip(),
        "sub_brands":  [str(s).strip() for s in (parsed.get("sub_brands") or []) if s],
        "aliases":     [str(a).strip() for a in (parsed.get("aliases") or []) if a],
        "confidence":  str(parsed.get("confidence") or "medium").strip(),
    }
    if not result["sub_brands"]:
        result["sub_brands"] = [name]
    return result
