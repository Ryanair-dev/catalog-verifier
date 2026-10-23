"""LLM query fallback for vendor titles after Catalog Verifier retrieval fails."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

from anthropic import Anthropic

from services.analytics.matching_core.agents.query_prompts import (
    ASSESS_RESULTS,
    ASSESS_SYSTEM,
    BUILD_QUERY_SYSTEM,
    PROMPT_VERSION,
)
from services.analytics.matching_core.models import Candidate, Offer
from services.analytics.runner import normalize_amazon_item

log = logging.getLogger(__name__)
MODEL = "claude-sonnet-4-6"
MAX_ITERATIONS = 2
MAX_CANDIDATES = 20
INCLUDED_DATA = "summaries,identifiers,attributes,salesRanks"
MAX_QUERY_WORDS = 15


def _parse_json(text: str) -> dict[str, Any]:
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(cleaned)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}


def _valid_query(query: str) -> bool:
    return bool(query) and len(query.split()) <= MAX_QUERY_WORDS


def _offer_text(offer: Offer) -> str:
    parts = [offer.brand, offer.title or offer.raw_text]
    if offer.mpn:
        parts.append(f"(vendor part number: {offer.mpn})")
    return " ".join(part for part in parts if part)


class QueryAgent:
    def __init__(self, client: Any, api: Any, max_iterations: int = MAX_ITERATIONS):
        self.client = client
        self.api = api
        self.max_iterations = max_iterations

    def _call(
        self, system: str, user: str, stats: dict[str, Any], action: str,
    ) -> dict[str, Any]:
        started = time.monotonic()
        response = self.client.messages.create(
            model=MODEL,
            max_tokens=400,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        elapsed = time.monotonic() - started
        stats["seconds_llm"] += elapsed
        stats["llm_calls"] += 1
        stats["tokens_in"] += response.usage.input_tokens
        stats["tokens_out"] += response.usage.output_tokens
        stats["llm_steps"].append({
            "action": action,
            "seconds": round(elapsed, 3),
            "tokens_in": response.usage.input_tokens,
            "tokens_out": response.usage.output_tokens,
        })
        stats["trace"].append({
            "step": action,
            "seconds": round(elapsed, 3),
            "tokens_in": response.usage.input_tokens,
            "tokens_out": response.usage.output_tokens,
        })
        return _parse_json(response.content[0].text)

    def _build_query(self, offer: Offer, stats: dict[str, Any]) -> tuple[str, str]:
        text = _offer_text(offer)
        output = self._call(BUILD_QUERY_SYSTEM, text, stats, "build_query")
        query = str(output.get("search_query") or "").strip()
        comparison_title = str(output.get("comparison_title") or "").strip()
        if not _valid_query(query):
            query = (offer.title or offer.raw_text or "").strip()
            log.warning("Query agent returned no usable search query; using raw title")
            stats["trace"].append({
                "step": "query_built",
                "detail": "model returned no usable query; falling back to the raw title",
                "search_query": query,
                "comparison_title": comparison_title,
                "fallback": True,
            })
        else:
            stats["trace"].append({
                "step": "query_built",
                "detail": f'search: "{query}" | for scoring: "{comparison_title}"',
                "search_query": query,
                "comparison_title": comparison_title,
                "reasoning": output.get("reasoning", ""),
            })
        return query, comparison_title

    def _search(self, query: str, stats: dict[str, Any]) -> list[dict[str, Any]]:
        if not query:
            return []
        stats["api_calls"] += 1
        started = time.monotonic()
        try:
            response = self.api.search_by_keywords(
                query, page_size=MAX_CANDIDATES, included_data=INCLUDED_DATA,
            )
            items = response if isinstance(response, list) else (response or {}).get("items") or []
            normalized = [
                normalize_amazon_item(item) for item in items if item.get("asin")
            ]
            detail = f'"{query}" returned {len(normalized)} listing(s)'
            stats["trace"].append({
                "step": "searched",
                "detail": detail,
                "query": query,
                "n_results": len(normalized),
            })
            return normalized
        except Exception as exc:
            detail = f"search failed: {type(exc).__name__}: {exc}"
            log.warning("Query-agent search failed for %r: %s", query, detail)
            stats["trace"].append({
                "step": "searched",
                "detail": detail,
                "query": query,
                "error": True,
            })
            return []
        finally:
            elapsed = time.monotonic() - started
            stats["seconds_search"] += elapsed
            stats["search_steps"].append({
                "query": query,
                "seconds": round(elapsed, 3),
                "candidates": len(normalized) if "normalized" in locals() else 0,
            })
            if stats["trace"] and stats["trace"][-1].get("step") == "searched":
                stats["trace"][-1]["seconds"] = round(elapsed, 3)

    def _assess(
        self, offer: Offer, query: str, candidates: list[dict[str, Any]],
        stats: dict[str, Any],
    ) -> dict[str, Any]:
        listing = "\n".join(
            f"- {candidate.get('title') or ''}" for candidate in candidates[:10]
        ) or "(nothing returned)"
        output = self._call(
            ASSESS_SYSTEM,
            ASSESS_RESULTS.format(
                offer=_offer_text(offer),
                query=query,
                results=listing,
            ),
            stats,
            "assess_results",
        )
        stats["trace"].append({
            "step": "assessed",
            "detail": f"{output.get('verdict', 'no verdict')} - {output.get('reasoning', '')}",
            "verdict": output.get("verdict"),
        })
        return output

    def run(self, offer: Offer) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        stats: dict[str, Any] = {
            "used_agent": True,
            "model": MODEL,
            "prompt_version": PROMPT_VERSION,
            "queries_tried": [],
            "llm_calls": 0,
            "api_calls": 0,
            "tokens_in": 0,
            "tokens_out": 0,
            "seconds_llm": 0.0,
            "seconds_search": 0.0,
            "llm_steps": [],
            "search_steps": [],
            "trace": [],
        }
        started = time.monotonic()
        candidates: list[dict[str, Any]] = []
        query = ""
        try:
            query, comparison_title = self._build_query(offer, stats)
            stats["comparison_title"] = comparison_title
            seen = {query}
            for iteration in range(self.max_iterations):
                stats["queries_tried"].append(query)
                candidates = self._search(query, stats)
                if iteration == self.max_iterations - 1:
                    stats["candidates"] = candidates
                    stats["search_query"] = query
                    stats["gave_up"] = not candidates
                    stats["trace"].append({
                        "step": "gave_up" if not candidates else "accepted",
                        "detail": (
                            "nothing found on the final attempt"
                            if not candidates else
                            f"keeping {len(candidates)} listing(s) after "
                            f"{iteration + 1} attempt(s)"
                        ),
                        "n_results": len(candidates),
                    })
                    break
                assessment = self._assess(offer, query, candidates, stats)
                if assessment.get("verdict") == "good" and candidates:
                    stats["candidates"] = candidates
                    stats["search_query"] = query
                    stats["trace"].append({
                        "step": "accepted",
                        "detail": f"keeping {len(candidates)} listing(s)",
                        "n_results": len(candidates),
                    })
                    break
                next_query = str(assessment.get("next_query") or "").strip()
                if not _valid_query(next_query) or next_query in seen:
                    stats["candidates"] = candidates
                    stats["search_query"] = query
                    stats["gave_up"] = not candidates
                    stats["trace"].append({
                        "step": "gave_up",
                        "detail": "no new query proposed; stopping rather than looping",
                    })
                    break
                seen.add(next_query)
                stats["trace"].append({
                    "step": "retry",
                    "detail": f'retrying with "{next_query}"',
                    "attempt": iteration + 2,
                    "query": next_query,
                })
                query = next_query
        except Exception as exc:
            stats["error"] = f"{type(exc).__name__}: {exc}"[:500]
            stats["trace"].append({
                "step": "error",
                "detail": stats["error"],
            })
            log.exception("Query agent failed for offer %s", offer.offer_id)

        stats.setdefault("search_query", query)
        stats["iterations"] = len(stats["queries_tried"])
        stats.setdefault("gave_up", not candidates)
        stats["seconds_total"] = round(time.monotonic() - started, 3)
        stats["seconds_search"] = round(stats["seconds_search"], 3)
        stats["seconds_llm"] = round(stats["seconds_llm"], 3)
        stats["tokens"] = stats["tokens_in"] + stats["tokens_out"]
        candidates = stats.pop("candidates", candidates)
        stats["candidates_found"] = len(candidates)
        stats["found_via"] = "agent"
        return candidates, stats


def create_query_agent(api: Any) -> QueryAgent:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is required to use the query agent")
    return QueryAgent(Anthropic(api_key=key, max_retries=3, timeout=30.0), api)
