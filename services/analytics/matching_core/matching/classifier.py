"""Feature extraction and scoring for the trained product-match classifier."""
from __future__ import annotations

import os
import pickle
from functools import lru_cache
from pathlib import Path
from typing import Any

from rapidfuzz import fuzz

from services.analytics.matching_core.matching.attributes import (
    apparel_size_mismatch,
    linear_size_mismatch,
    size_mismatch,
)
from services.analytics.matching_core.matching.normalize import (
    base_mpn,
    brands_match,
    normalize_upc,
    upc_matches,
)
from services.analytics.matching_core.models import Candidate, MatchResult, Offer, Verdict

DEFAULT_MODEL_PATH = (
    Path(__file__).resolve().parents[1] / "classifier.pkl"
)


def extract_features(row: dict[str, Any]) -> dict[str, float | int]:
    """Build classifier features from one serialized offer/candidate pair."""
    offer = Offer(**row["offer"])
    candidate = Candidate(**row["candidate"])

    upc_hit = bool(
        offer.upc and candidate.upc
        and upc_matches(normalize_upc(offer.upc), normalize_upc(candidate.upc))
    )
    mpn_hit = bool(
        offer.mpn and candidate.mpn
        and base_mpn(offer.mpn) == base_mpn(candidate.mpn)
    )
    brand_agrees = bool(
        offer.brand and candidate.brand and brands_match(offer.brand, candidate.brand)
    )
    brand_disagrees = bool(
        offer.brand and candidate.brand
        and not brands_match(offer.brand, candidate.brand)
        and fuzz.ratio(offer.brand.lower(), candidate.brand.lower()) < 70
    )

    source_title = offer.title or offer.raw_text or ""
    candidate_title = candidate.title or ""
    title_ratio = (
        float(fuzz.token_set_ratio(source_title.lower(), candidate_title.lower()))
        if source_title and candidate_title
        else 0.0
    )
    size_mm = bool(size_mismatch(offer, candidate))
    apparel_mm = bool(apparel_size_mismatch(offer, candidate))
    linear_mm = bool(linear_size_mismatch(offer, candidate))

    return {
        "upc_hit": int(upc_hit),
        "mpn_hit": int(mpn_hit),
        "upc_present": int(bool(offer.upc and candidate.upc)),
        "mpn_present": int(bool(offer.mpn and candidate.mpn)),
        "brand_agrees": int(brand_agrees),
        "brand_disagrees": int(brand_disagrees),
        "title_ratio": title_ratio,
        "title_ratio_sq": (title_ratio / 100) ** 2 * 100,
        "size_mismatch": int(size_mm),
        "apparel_mismatch": int(apparel_mm),
        "linear_mismatch": int(linear_mm),
        "any_mismatch": int(size_mm or apparel_mm or linear_mm),
        "upc_hit_x_title": upc_hit * title_ratio,
        "mpn_hit_x_title": mpn_hit * title_ratio,
        "brand_x_title": brand_agrees * title_ratio,
    }


@lru_cache(maxsize=4)
def _load_model(model_path: Path) -> dict[str, Any]:
    if not model_path.is_file():
        raise FileNotFoundError(f"Classifier model not found: {model_path}")
    with model_path.open("rb") as model_file:
        model: dict[str, Any] = pickle.load(model_file)
    if "clf" not in model or "feature_names" not in model:
        raise ValueError(f"Invalid classifier model file: {model_path}")
    return model


def score(offer: Offer, candidate: Candidate) -> MatchResult:
    model_path = Path(os.environ.get("CLASSIFIER_MODEL_PATH", DEFAULT_MODEL_PATH))
    model = _load_model(model_path)
    features = extract_features({
        "offer": offer.model_dump(),
        "candidate": candidate.model_dump(),
    })
    classifier = model["clf"]
    feature_names = model["feature_names"]
    probability = float(classifier.predict_proba(
        [[features[name] for name in feature_names]]
    )[0][1])
    confidence = round(probability * 100, 1)
    verdict = (
        Verdict.VERIFIED if probability >= 0.5
        else Verdict.REVIEW if probability >= 0.2
        else Verdict.REJECTED
    )

    return MatchResult(
        offer_id=offer.offer_id,
        asin=candidate.asin,
        confidence=confidence,
        verdict=verdict,
        signals={"match_probability": probability},
        reasons=["rule:classifier"],
    )
