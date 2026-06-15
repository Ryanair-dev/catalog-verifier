"""
AI Re-check — second-pass verdict suggestions.

Tries Anthropic Claude first (ANTHROPIC_API_KEY); falls back to OpenAI
(OPENAI_API_KEY) when Anthropic is unavailable.

Given a subset of rows already scored by the deterministic engine, asks the
AI to play devil's advocate: it sees the vendor title, the Amazon title, the
current verdict, and the per-signal breakdown, then returns a *suggested*
verdict plus a short reason.  The suggestion is shown alongside the original
verdict — nothing auto-applies.  The user still clicks accept/reject.

Cost model
----------
Claude Sonnet 4.6 (default):
    $3.00 per 1M input  tokens  = $0.003 per 1K
    $15.00 per 1M output tokens  = $0.015 per 1K

Claude Haiku 4.5:
    $0.80 per 1M input  tokens  = $0.0008 per 1K
    $4.00 per 1M output tokens  = $0.004 per 1K

OpenAI gpt-4o-mini (fallback):
    $0.150 per 1M input  tokens  = $0.00015 per 1K
    $0.600 per 1M output tokens  = $0.00060 per 1K
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Any

# USD per 1K tokens
PRICING: dict[str, dict[str, float]] = {
    # Claude models (primary)
    "claude-sonnet-4-6":         {"input": 0.003,   "output": 0.015},
    "claude-haiku-4-5-20251001": {"input": 0.0008,  "output": 0.004},
    # OpenAI models (fallback)
    "gpt-4o-mini": {"input": 0.00015, "output": 0.00060},
    "gpt-4o":      {"input": 0.00250, "output": 0.01000},
}

# Per-row averages, calibrated against the sample catalog.
AVG_INPUT_TOKENS  = 230
AVG_OUTPUT_TOKENS = 80

# Per-row wall-clock estimate. Used by the ETA helper on the client.
AVG_LATENCY_MS = 850

ALLOWED_MODELS = tuple(PRICING.keys())
DEFAULT_MODEL  = "claude-sonnet-4-6"

ALLOWED_BUCKETS = ("Approved", "Verified", "Review", "Not Approved")


# --------------------------------------------------------------------------- #
# Cost estimation
# --------------------------------------------------------------------------- #

def estimate_cost(row_count: int, model: str = DEFAULT_MODEL) -> dict:
    """Return an estimated token + USD cost for re-checking ``row_count`` rows."""
    price = PRICING.get(model) or PRICING[DEFAULT_MODEL]
    in_tokens  = row_count * AVG_INPUT_TOKENS
    out_tokens = row_count * AVG_OUTPUT_TOKENS
    cost_usd = (in_tokens / 1000) * price["input"] + (out_tokens / 1000) * price["output"]
    return {
        "row_count": row_count,
        "model": model,
        "input_tokens_est":  in_tokens,
        "output_tokens_est": out_tokens,
        "cost_usd_est": round(cost_usd, 4),
        "duration_ms_est": row_count * AVG_LATENCY_MS,
    }


# --------------------------------------------------------------------------- #
# Prompting
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = (
    "You are a CPG catalog quality auditor. Given a vendor product, the Amazon "
    "listing it was matched to, and the initial rule-based verdict, decide "
    "whether the match is correct. "
    "Respond ONLY with a raw JSON object (no markdown, no code blocks):\n"
    "  suggested_verdict : one of \"Approved\", \"Review\", \"Not Approved\", \"keep\".\n"
    "    Use \"keep\" if the initial verdict is clearly correct and nothing changes.\n"
    "  reason            : one short sentence (< 120 chars) explaining your call.\n"
    "  attributes        : object with any of these keys that you can confidently "
    "extract from the vendor title: product_type, size, pack_count, variant, form.\n"
    "Never invent data. Never return prose outside the JSON."
)


def _build_user_prompt(row: dict, amz: dict | None) -> str:
    vendor_title = (row.get("Title") or row.get("Vendor Title") or "").strip()
    vendor_brand = (row.get("Brand") or "").strip()
    vendor_upc   = str(row.get("UPC") or row.get("UPC/EAN") or "").strip()
    asin         = (row.get("ASIN")  or "").strip()
    current      = row.get("Verdict") or ""
    confidence   = row.get("Confidence")
    signals      = row.get("signals") or {}

    amz_title = ""
    amz_brand = ""
    amz_upc   = ""
    amz_pack  = row.get("amz_pack")
    if amz:
        for k in ("Title", "item_name", "Item Name", "Product Title"):
            if amz.get(k):
                amz_title = str(amz[k]); break
        for k in ("Brand", "brand", "Manufacturer", "manufacturer"):
            if amz.get(k):
                amz_brand = str(amz[k]); break
        for k in ("UPC", "upc", "EAN", "ean"):
            if amz.get(k):
                amz_upc = str(amz[k]); break

    sig_summary = {}
    for key in ("upc", "item_id", "brand", "title", "pack"):
        s = signals.get(key) or {}
        sig_summary[key] = {
            "matched": s.get("matched"),
            "score": s.get("score"),
            "detail": s.get("detail"),
        }

    payload = {
        "vendor": {
            "title": vendor_title, "brand": vendor_brand,
            "upc": vendor_upc, "asin": asin,
        },
        "amazon": {
            "title": amz_title, "brand": amz_brand,
            "upc": amz_upc, "pack_count": amz_pack,
        },
        "current": {
            "verdict": current,
            "confidence": confidence,
            "signals": sig_summary,
        },
    }
    return json.dumps(payload, ensure_ascii=False)


# --------------------------------------------------------------------------- #
# JSON extraction helper
# --------------------------------------------------------------------------- #

def _extract_json(text: str) -> dict:
    """Parse JSON from a model response that may contain markdown or prose."""
    text = text.strip()
    # Direct parse
    try:
        return json.loads(text)
    except Exception:
        pass
    # Strip markdown code fence
    m = re.search(r"```(?:json)?\s*([\s\S]+?)```", text)
    if m:
        try:
            return json.loads(m.group(1).strip())
        except Exception:
            pass
    # Find first {...}
    m = re.search(r"\{[\s\S]+\}", text)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return {}


# --------------------------------------------------------------------------- #
# Single-row re-check
# --------------------------------------------------------------------------- #

def _is_anthropic(client: Any) -> bool:
    """True when client is an Anthropic SDK instance."""
    return client is not None and hasattr(client, "messages") and not hasattr(client, "chat")


def recheck_row(row: dict, amz: dict | None, client: Any, model: str) -> dict:
    """Call AI once for a single row. Returns a structured dict — never raises."""
    if _is_anthropic(client):
        return _recheck_claude(row, amz, client, model)
    return _recheck_openai(row, amz, client, model)


def _recheck_claude(row: dict, amz: dict | None, client: Any, model: str) -> dict:
    started = time.time()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=256,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": _build_user_prompt(row, amz)}],
            temperature=0,
        )
        raw    = resp.content[0].text if resp.content else "{}"
        parsed = _extract_json(raw)
        in_t   = getattr(resp.usage, "input_tokens",  AVG_INPUT_TOKENS)
        out_t  = getattr(resp.usage, "output_tokens", AVG_OUTPUT_TOKENS)
    except Exception as exc:
        return {
            "ok": False, "error": str(exc),
            "input_tokens": 0, "output_tokens": 0,
            "duration_ms": int((time.time() - started) * 1000),
        }

    verdict = (parsed.get("suggested_verdict") or "keep").strip()
    if verdict not in (*ALLOWED_BUCKETS, "keep"):
        verdict = "keep"
    return {
        "ok": True,
        "suggested_verdict": verdict,
        "reason": (parsed.get("reason") or "").strip()[:240],
        "attributes": parsed.get("attributes") or {},
        "input_tokens":  int(in_t),
        "output_tokens": int(out_t),
        "duration_ms":   int((time.time() - started) * 1000),
    }


def _recheck_openai(row: dict, amz: dict | None, client: Any, model: str) -> dict:
    started = time.time()
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": _build_user_prompt(row, amz)},
            ],
            response_format={"type": "json_object"},
            temperature=0,
        )
        raw    = resp.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        usage  = getattr(resp, "usage", None)
        in_t   = getattr(usage, "prompt_tokens",     AVG_INPUT_TOKENS)  if usage else AVG_INPUT_TOKENS
        out_t  = getattr(usage, "completion_tokens", AVG_OUTPUT_TOKENS) if usage else AVG_OUTPUT_TOKENS
    except Exception as exc:
        return {
            "ok": False, "error": str(exc),
            "input_tokens": 0, "output_tokens": 0,
            "duration_ms": int((time.time() - started) * 1000),
        }

    verdict = (parsed.get("suggested_verdict") or "keep").strip()
    if verdict not in (*ALLOWED_BUCKETS, "keep"):
        verdict = "keep"
    return {
        "ok": True,
        "suggested_verdict": verdict,
        "reason": (parsed.get("reason") or "").strip()[:240],
        "attributes": parsed.get("attributes") or {},
        "input_tokens":  int(in_t),
        "output_tokens": int(out_t),
        "duration_ms":   int((time.time() - started) * 1000),
    }


# --------------------------------------------------------------------------- #
# Client bootstrap
# --------------------------------------------------------------------------- #

def make_client() -> Any:
    """Return an AI client — Anthropic preferred, OpenAI fallback — or None."""
    key = os.getenv("ANTHROPIC_API_KEY")
    if key:
        try:
            import anthropic
            return anthropic.Anthropic(api_key=key)
        except Exception:
            pass

    key = os.getenv("OPENAI_API_KEY")
    if key:
        try:
            from openai import OpenAI
            return OpenAI(api_key=key)
        except Exception:
            pass

    return None


def actual_cost(input_tokens: int, output_tokens: int, model: str) -> float:
    """Compute the real spend from actual token usage."""
    price = PRICING.get(model) or PRICING[DEFAULT_MODEL]
    return round(
        (input_tokens / 1000) * price["input"]
        + (output_tokens / 1000) * price["output"],
        4,
    )
