from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field


class Offer(BaseModel):
    """A vendor offer. Any identifier field might be missing."""

    offer_id: str
    title: str | None = None
    brand: str | None = None
    upc: str | None = None
    mpn: str | None = None
    raw_text: str | None = None            
    attributes: dict[str, str] = Field(default_factory=dict)

    def available_identifiers(self) -> list[str]:
        """Tells us which identifier-based retrieval strategies are even possible for this offer"""
        out: list[str] = []
        if self.upc:
            out.append("upc")
        if self.mpn:
            out.append("mpn")
        if self.title or self.raw_text:
            out.append("keyword")
        return out


class Candidate(BaseModel):
    """An Amazon listing returned by retrieval, before it's matched/scored."""

    asin: str
    title: str | None = None
    brand: str | None = None
    upc: str | None = None
    mpn: str | None = None
    attributes: dict[str, list[str]] = Field(default_factory=dict)  # multi-value
    bullet_points: list[str] = Field(default_factory=list)
    description: str | None = None
    found_via: str = ""                    # "upc" | "mpn" | "keyword"


class Verdict(str, Enum):
    VERIFIED = "verified"
    REVIEW = "review"
    REJECTED = "rejected"


class MatchResult(BaseModel):
    """The outcome of scoring one offer against one candidate."""

    offer_id: str
    asin: str | None
    confidence: float
    verdict: Verdict
    signals: dict[str, float] = Field(default_factory=dict)   # per-signal breakdown
    reasons: list[str] = Field(default_factory=list)          # human-readable why
    used_agent: bool = False               # did the verification agent touch this?


class LabeledPair(BaseModel):
    """One hand-labeled offer↔ASIN pair — the ground truth the eval runs against."""

    offer_id: str
    asin: str
    is_match: bool                         # the human judgment
    note: str = ""                         # optional: why, or edge-case flag