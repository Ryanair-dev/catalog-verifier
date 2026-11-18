"""Approval auditor: checks the scorer's auto-approvals before they ship."""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Any

from anthropic import Anthropic

from services.analytics.matching_core.agents.verify_prompts import (
    AUDIT_SYSTEM,
    AUDIT_USER,
    VERIFY_PROMPT_VERSION,
)
from services.analytics.matching_core.models import Candidate, MatchResult, Offer, Verdict

MODEL = "claude-sonnet-4-6"
SKIP_RULES: frozenset[str] = frozenset()
log = logging.getLogger(__name__)


@dataclass
class AuditResult:
    offer_id: str = ""
    asin: str = ""
    flagged: bool = False
    reason: str = ""
    audited: bool = False
    original_verdict: str = ""
    original_confidence: float = 0.0
    rule: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    seconds: float = 0.0
    error: str = ""

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out

    def as_record(self) -> dict[str, Any]:
        return {
            "offer_id": self.offer_id,
            "asin": self.asin,
            "flagged": self.flagged,
            "reason": self.reason,
            "audited": self.audited,
            "original_verdict": self.original_verdict,
            "original_confidence": self.original_confidence,
            "rule": self.rule,
            "tokens": self.tokens,
            "seconds": round(self.seconds, 3),
            "error": self.error,
            "prompt_version": VERIFY_PROMPT_VERSION,
        }


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


def _describe(offer: Offer) -> str:
    parts = [offer.brand, offer.title or offer.raw_text]
    if offer.mpn:
        parts.append(f"part number {offer.mpn}")
    if offer.upc:
        parts.append(f"UPC {offer.upc}")
    return " | ".join(part for part in parts if part)


def _describe_candidate(candidate: Candidate) -> str:
    parts = [candidate.brand, candidate.title]
    if candidate.mpn:
        parts.append(f"part number {candidate.mpn}")
    if candidate.upc:
        parts.append(f"UPC {candidate.upc}")
    extra = {
        key: values[0]
        for key, values in (candidate.attributes or {}).items()
        if key in (
            "item_form", "material", "style", "special_feature",
            "variation_theme", "unit_count",
        ) and values
    }
    if extra:
        parts.append("; ".join(f"{key}={value}" for key, value in extra.items()))
    return " | ".join(part for part in parts if part)


def _rule_of(result: MatchResult) -> str:
    for reason in result.reasons:
        if reason.startswith("rule:"):
            return reason.removeprefix("rule:")
    return ", ".join(result.reasons) or "weighted score"


class ApprovalAuditor:
    def __init__(self, client: Any = None):
        self.client = client or Anthropic(
            api_key=os.getenv("ANTHROPIC_API_KEY"), max_retries=3, timeout=30.0,
        )

    def audit(
        self, offer: Offer, candidate: Candidate, result: MatchResult,
    ) -> AuditResult:
        rule = _rule_of(result)
        audit = AuditResult(
            offer_id=result.offer_id,
            asin=result.asin or "",
            original_verdict=result.verdict.value,
            original_confidence=result.confidence,
            rule=rule,
        )
        if result.verdict is not Verdict.VERIFIED or rule in SKIP_RULES:
            return audit

        message = AUDIT_USER.format(
            offer=_describe(offer),
            candidate=_describe_candidate(candidate),
            confidence=result.confidence,
            rule=rule,
            signals={
                key: value for key, value in (result.signals or {}).items() if value
            },
        )
        try:
            started = time.monotonic()
            response = self.client.messages.create(
                model=MODEL,
                max_tokens=200,
                system=AUDIT_SYSTEM,
                messages=[{"role": "user", "content": message}],
            )
            audit.seconds = time.monotonic() - started
            audit.tokens_in = response.usage.input_tokens
            audit.tokens_out = response.usage.output_tokens
            content = response.content[0].text if response.content else "{}"
            parsed = _parse_json(content)
            if parsed.get("verdict") not in {"confirm", "flag"}:
                audit.error = "Verifier returned an invalid verdict"
                log.warning(
                    "Approval audit returned an invalid verdict for ASIN %s",
                    candidate.asin,
                )
                return audit
            audit.flagged = parsed.get("verdict") == "flag"
            audit.reason = parsed.get("reason", "")
            audit.audited = True
        except Exception as exc:
            audit.error = f"{type(exc).__name__}: {exc}"
        return audit


def apply_audit(result: MatchResult, audit: AuditResult) -> MatchResult:
    if not audit.flagged:
        return result
    return MatchResult(
        offer_id=result.offer_id,
        asin=result.asin,
        confidence=min(result.confidence, 70.0),
        verdict=Verdict.REVIEW,
        signals=result.signals,
        reasons=[*result.reasons, f"audit_flagged: {audit.reason}"],
        used_agent=True,
    )


def create_approval_auditor() -> ApprovalAuditor:
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY is required to use the approval verifier")
    return ApprovalAuditor(Anthropic(api_key=key, max_retries=3, timeout=30.0))
