"""Adapt Catalog Verifier's normalized records to the matcher scoring core.
"""
from __future__ import annotations

import logging
import threading
from typing import Any

from services.analytics.matching_core.models import Candidate, MatchResult, Offer, Verdict
from services.analytics.matching_core.matching.cascade import score as score_cascade
from services.analytics.matching_core.matching.attribute_compare import compare_attributes
from services.analytics.matching_core.matching.classifier import (
    explain as explain_classifier,
    score as score_classifier,
)
from services.analytics.matching_core.matching.normalize import (
    brands_match,
    normalize_upc,
    upc_matches,
)
from services.analytics.matching_core.matching.scorer import score as score_weighted
from services.analytics.matching_core.matching.flags import unified_attrs_enabled
from services.analytics.matching_core.matching.variant_gate import (
    apply_gate,
    describe_gate,
    gate_for_pair,
)

import os as _ur_os
import re as _ur_re

from services.analytics.matching_core.matching.attr_extract import _tokens as _ur_tokens
from services.analytics.matching_core.matching.attr_extract import extract_amazon as _ur_extract_amazon
from services.analytics.matching_core.matching.attr_extract import extract_vendor as _ur_extract_vendor
from services.analytics.matching_core.matching.lexicon import gender_tokens as _ur_gender_tokens
from services.analytics.matching_core.matching.lexicon import get_lexicon as _ur_get_lexicon
from services.analytics.matching_core.matching.lexicon import variant_boxes_for as _ur_variant_boxes_for
from services.analytics.matching_core.matching.normalize import brands_match as _ur_brands_match
from services.analytics.matching_core.matching.variation import compare_boxes as _ur_compare_boxes

_log = logging.getLogger(__name__)
_logged_scorers: set[str] = set()
_log_lock = threading.Lock()

SCORING_METHODS = frozenset({"weighted", "cascade", "classifier"})
_SCORERS = {
    "weighted": score_weighted,
    "cascade": score_cascade,
    "classifier": score_classifier,
}

_OFFER_FIELDS = {
    "upc": ("UPC/EAN", "upc", "UPC", "EAN", "GTIN"),
    "mpn": ("Item ID", "itemid", "mpn", "MPN", "Part Number", "part_number"),
    "title": ("Vendor Title", "title", "Title", "Item Name", "item_name"),
    "brand": ("Brand", "brand"),
}
_CANDIDATE_FIELDS = {
    "title": ("Title", "title", "Item Name", "item_name", "Product Title"),
    "brand": ("Brand", "brand", "Manufacturer", "manufacturer"),
    "mpn": (
        "Product Codes: PartNumber", "Part Number", "part_number",
        "Model", "model_number", "MPN", "mpn",
    ),
    "upc": (
        "Product Codes: UPC", "Product Codes: EAN", "Product Codes: GTIN",
        "upc", "UPC", "ean", "EAN", "gtin", "GTIN", "identifier_value",
    ),
}
_ATTRIBUTE_FIELDS = {
    "color": ("Color", "color"),
    "size": ("Size", "size"),
    "material": ("Material", "material"),
    "scent": ("Scent", "scent", "Fragrance", "fragrance"),
    "flavor": ("Flavor", "flavor"),
}


def _first_value(record: dict[str, Any], fields: tuple[str, ...]) -> str:
    for field in fields:
        value = record.get(field)
        if isinstance(value, (list, tuple)):
            value = next((part for part in value if part not in (None, "")), "")
        if isinstance(value, dict):
            value = value.get("value") or ""
        text = str(value or "").strip()
        if text:
            return text.replace(";", "|").replace(",", "|").split("|", 1)[0].strip()
    return ""


def _mapped_attributes(
    record: dict[str, Any], aliases: dict[str, tuple[str, ...]],
) -> dict[str, str]:
    found: dict[str, str] = {}
    existing = record.get("attributes") or record.get("_attributes")
    if isinstance(existing, dict):
        for key, value in existing.items():
            if value not in (None, ""):
                found[str(key).strip().lower()] = str(value).strip()
    for canonical, fields in aliases.items():
        value = _first_value(record, fields)
        if value:
            found[canonical] = value
    return found


def build_export_pair(
    source: dict[str, Any],
    amazon: dict[str, Any],
    *,
    sources: list[str] | None = None,
) -> tuple[Offer, Candidate]:
    """Map Verification/Keepa export rows into the matcher's input models."""
    offer = Offer(
        offer_id=str(source.get("row_idx", "")),
        title=_first_value(source, _OFFER_FIELDS["title"]) or None,
        raw_text=_first_value(source, _OFFER_FIELDS["title"]) or None,
        brand=_first_value(source, _OFFER_FIELDS["brand"]) or None,
        upc=_first_value(source, _OFFER_FIELDS["upc"]) or None,
        mpn=_first_value(source, _OFFER_FIELDS["mpn"]) or None,
        attributes=_mapped_attributes(source, {}),
    )
    candidate = Candidate(
        asin=(
            _first_value(amazon, ("ASIN", "asin", "Asin"))
            or _first_value(source, ("ASIN", "asin", "Asin"))
        ),
        title=_first_value(amazon, _CANDIDATE_FIELDS["title"]) or None,
        brand=_first_value(amazon, _CANDIDATE_FIELDS["brand"]) or None,
        upc=_first_value(amazon, _CANDIDATE_FIELDS["upc"]) or None,
        mpn=_first_value(amazon, _CANDIDATE_FIELDS["mpn"]) or None,
        attributes={
            key: [value]
            for key, value in _mapped_attributes(amazon, _ATTRIBUTE_FIELDS).items()
        },
        bullet_points=[
            str(value).strip()
            for value in (amazon.get("bullet_point") or amazon.get("bullet_points") or [])
            if value not in (None, "")
        ] if isinstance(amazon.get("bullet_point") or amazon.get("bullet_points") or [], (list, tuple)) else [],
        description=_first_value(amazon, ("Description", "description", "product_description")) or None,
        found_via=",".join(sources or []),
    )
    return offer, candidate


def score_export_pair(
    source: dict[str, Any],
    amazon: dict[str, Any],
    *,
    method: str = "weighted",
    mode: str = "cpg",
    sources: list[str] | None = None,
) -> tuple[dict[str, Any], str]:
    """Score old-format catalog/Amazon rows with the selected matcher scorer."""
    if method not in SCORING_METHODS:
        raise ValueError(f"Unsupported scoring method: {method}")
    if not (
        _first_value(amazon, ("ASIN", "asin", "Asin"))
        or _first_value(source, ("ASIN", "asin", "Asin"))
    ):
        return {
            "confidence_score": 0.0,
            "scoring_method": method,
            "signals": {},
            "reasons": ["No candidate ASIN"],
            "upc_match": False,
            "brand_confirmed": False,
            "effective_pack": None,
        }, "not_approved"
    offer, candidate = build_export_pair(source, amazon, sources=sources)
    result = score_models(offer, candidate, method=method, mode=mode)
    return result_to_scores(result, method, offer, candidate), (
        "not_approved" if result.verdict is Verdict.REJECTED else result.verdict.value
    )


def score_pair(
    source: dict[str, Any],
    normalized: dict[str, Any],
    *,
    method: str = "weighted",
    mode: str = "cpg",
    extracted: dict[str, Any] | None = None,
    sources: list[str] | None = None,
) -> tuple[dict[str, Any], str]:
    """Score a retrieved Amazon item without changing Catalog Verifier retrieval."""
    offer, candidate = build_pair(
        source, normalized, extracted=extracted, sources=sources,
    )
    result = score_models(offer, candidate, method=method, mode=mode)
    # Shared variant check on the unified attribute boxes: same for weighted, cascade, classifier.
    barcode_ok = bool(
        offer.upc and candidate.upc
        and upc_matches(normalize_upc(offer.upc), normalize_upc(candidate.upc))
    )
    gate = (gate_for_pair(source, normalized, extracted, barcode_confirmed=barcode_ok)
            if unified_attrs_enabled() else None)
    if gate:
        result = apply_gate(result, gate)
    scores = result_to_scores(result, method, offer, candidate)
    if gate:
        scores["variant_check"] = {
            "hard": gate["hard"], "soft": gate["soft"], "boxes": gate["boxes"],
            "open": gate.get("open", []),
            "lexicon": gate.get("lexicon"),          # version of the word lists that produced this verdict
            "categories": gate.get("categories"),    # product categories used by the variant rules
        }
        note = describe_gate(gate)
        explanation = scores.get("explanation")
        if note and isinstance(explanation, dict):
            explanation["summary"] = f"{explanation.get('summary') or ''} {note}".strip()
    verdict = result.verdict.value
    return scores, "not_approved" if verdict == "rejected" else verdict


def build_pair(
    source: dict[str, Any],
    normalized: dict[str, Any],
    *,
    extracted: dict[str, Any] | None = None,
    sources: list[str] | None = None,
) -> tuple[Offer, Candidate]:
    extracted = extracted or {}
    offer = Offer(
        offer_id=str(source.get("row_idx", "")),
        title=str(source.get("title") or ""),
        raw_text=str(source.get("title") or ""),
        brand=str(source.get("brand") or extracted.get("brand") or "") or None,
        upc=str(source.get("upc") or "") or None,
        mpn=str(source.get("mpn") or source.get("itemid") or "") or None,
    )
    candidate_attrs = dict(normalized.get("attributes") or {})
    for pack_key in ("item_package_quantity", "number_of_items"):
        pack_value = normalized.get(pack_key)
        if pack_value not in (None, "", 0, "0"):
            candidate_attrs[pack_key] = pack_value
    candidate = Candidate(
        asin=str(normalized.get("asin") or ""),
        title=str(normalized.get("title") or ""),
        brand=str(normalized.get("brand") or normalized.get("manufacturer") or "") or None,
        upc=str(normalized.get("upc") or normalized.get("ean") or normalized.get("gtin") or "") or None,
        mpn=str(normalized.get("mpn") or "") or None,
        attributes={
            str(key): [str(value)]
            for key, value in candidate_attrs.items()
            if value is not None and str(value).strip()
        },
        bullet_points=[
            str(point) for point in (normalized.get("bullet_points") or [])
            if point is not None and str(point).strip()
        ],
        description=str(normalized.get("description") or "") or None,
        found_via=",".join(sources or []),
    )
    return offer, candidate



def score_models(offer: Offer, candidate: Candidate, *, method: str, mode: str = "cpg") -> MatchResult:
    if method not in SCORING_METHODS:
        raise ValueError(f"Unsupported scoring method: {method}")
    with _log_lock:
        if method not in _logged_scorers:
            _logged_scorers.add(method)
            _log.info(
                "[matching-core] scorer invoked: method=%s mode=%s",
                method,
                mode,
            )
    scorer = _SCORERS[method]
    return scorer(offer, candidate, mode=mode) if method == "cascade" else scorer(offer, candidate)


def result_to_scores(
    result: MatchResult, method: str, offer: Offer, candidate: Candidate,
) -> dict[str, Any]:
    signals = dict(result.signals)
    reasons = list(result.reasons)
    confidence = float(result.confidence)
    mismatches = [
        reason.removesuffix("_mismatch").replace("_", " ")
        for reason in reasons
        if reason.endswith("_mismatch")
    ]
    explanation: dict[str, Any]
    if method == "weighted":
        names = (("upc", "UPC"), ("mpn", "part number"), ("brand", "brand"), ("title", "title"))
        used = [(label, signals[key]) for key, label in names if key in signals]
        missing = [label for key, label in names if key not in signals]
        total = sum(value for _, value in used)
        summary = (
            "Weighted: " + " + ".join(f"{label} {value:.1f}" for label, value in used)
            + f" = {total:.1f} (points out of 100)."
        )
        if missing:
            summary += (
                f" Not compared (missing on one side): {', '.join(missing)}; "
                "their weight was shared by the others."
            )
        if mismatches:
            summary += f" Hard mismatch cap applied ({', '.join(mismatches)}); final score {confidence:.1f}."
        elif signals.get("upc", 0) > 0 and confidence > total + 0.05:
            summary += (
                f" UPC match: score raised to {confidence:.1f} "
                "(a UPC match is worth at least 90, or 100 with the same brand)."
            )
        elif "upc" not in signals and "mpn" not in signals and confidence < total - 0.05:
            summary += (
                f" No UPC or part number to compare, so the score is limited to {confidence:.1f} "
                "(never approved on brand and title alone)."
            )
        explanation = {
            "summary": summary,
            "factors": [f"{label}: {value:.1f} points" for label, value in used]
                       + ([f"Hard mismatch: {', '.join(mismatches)}"] if mismatches else []),
        }
    elif method == "cascade":
        rule = next(
            (reason.removeprefix("rule:") for reason in reasons if reason.startswith("rule:")),
            "fallback_fuzzy",
        )
        blocked_by_mismatch = rule.endswith("+blocked_by_mismatch")
        if blocked_by_mismatch:
            rule = rule.removesuffix("+blocked_by_mismatch")
        rule_labels = {
            "1_brand+upc": "Rule 1: brand + UPC",
            "1_brand+mpn": "Rule 1: brand + MPN",
            "2_upc+1attr": "Rule 2: UPC + at least one matching attribute",
            "2_mpn+1attr": "Rule 2: MPN + at least one matching attribute",
            "2b_upc_alone": "Rule 2b: UPC alone (nothing contradicts it)",
            "3_brand+all_attrs": "Rule 3: brand + all comparable attributes",
            "4_all_attrs+fuzzy70": "Rule 4: all comparable attributes + title similarity",
            "5_fuzzy72": "Rule 5: title similarity",
            "6_all_attrs": "Rule 6: all comparable attributes",
            "7_mpn_in_amz_text": "Rule 7: MPN found in Amazon text",
            "7_mpn_in_amz_text+1attr": "Rule 7: MPN in Amazon text + matching attribute",
            "fallback_fuzzy": "Fallback: half of fuzzy title similarity (capped at 60)",
        }
        attributes = compare_attributes(offer, candidate)
        attr_labels = {"volume": "Volume", "color": "Color", "size": "Apparel size", "count": "Bundle count"}
        own_check = " (scorer's own title check)"
        attr_factors = [
            f"{attr_labels[name]}{own_check}: {value.value}"
            for name, value in attributes.items()
        ]
        summary = f"{rule_labels.get(rule, rule)} produced {confidence:.1f}."
        if blocked_by_mismatch:
            summary += f" A hard attribute mismatch capped the score at {confidence:.1f}."
        explanation = {
            "summary": summary,
            "factors": [
                f"Brand agreement: {'yes' if signals.get('brand') else 'no'}",
                f"UPC match: {'yes' if signals.get('upc') else 'no'}",
                f"MPN match: {'yes' if signals.get('mpn') else 'no'}",
                f"Title similarity: {signals.get('fuzzy', 0):.1f}%",
                *attr_factors,
            ],
        }
    else:
        classifier_explanation = explain_classifier(offer, candidate)
        probability = float(signals.get("match_probability", confidence / 100))
        summary = (
            f"Classifier predicted {probability * 100:.1f}% match probability "
            f"({result.verdict.value}). This is a model probability, not a weighted point sum."
        )
        explanation = {
            "summary": summary,
            "factors": classifier_explanation["input_factors"],
            "global_importance": classifier_explanation["global_importance"],
        }

    scores: dict[str, Any] = {
        "confidence_score": confidence,
        "scoring_method": method,
        "scoring_verdict": "not_approved" if result.verdict is Verdict.REJECTED else result.verdict.value,
        "signals": signals,
        "reasons": reasons,
        "explanation": explanation,
        "upc_match": bool(
            offer.upc and candidate.upc
            and upc_matches(normalize_upc(offer.upc), normalize_upc(candidate.upc))
        ),
        "brand_confirmed": brands_match(offer.brand, candidate.brand),
        "effective_pack": None,
    }
    for key in (
        "size_mismatch", "apparel_size_mismatch", "linear_size_mismatch",
        "count_mismatch", "color_mismatch", "brand_mismatch",
    ):
        scores[key] = any(reason == key for reason in reasons)
    scores["size_mismatch"] = scores["size_mismatch"] or any(
        reason == "volume_mismatch" for reason in reasons
    )
    return scores

_UR_STATED = ("color", "size", "finish", "type", "flavor", "scent", "gender", "count")
_UR_BY_CATEGORY = {"color", "size", "flavor", "scent"}
_UR_CONTRADICT = ("color", "size", "finish", "type", "flavor", "scent", "gender", "shade_code", "material",
                  "volume_ml", "weight_g", "units", "model")
_UR_NOTE = ("color", "finish", "size", "type", "flavor", "scent", "gender", "count", "model", "style")
_UR_SOURCE = {"vendor_column": "vendor file", "ai": "AI", "title": "title", "amazon_field": "Amazon field",
              "ai_title": "AI-expanded title"}
_UR_KIND_BOX = {"apparel size": "size", "color": "color", "colour": "color", "gender": "gender", "scent": "scent",
                "shade": "shade_code"}
_UR_COLUMN_HINT = {"color": r"colou?r", "size": r"size", "flavor": r"flavou?r", "scent": r"scent|fragrance",
                   "gender": r"gender|department|target", "finish": r"finish"}
_UR_NOISE = {"small", "medium", "large", "extra", "x", "xs", "xl", "xxl", "xxxl", "xxs", "oz", "ounce", "ounces", "fl",
             "ml", "l", "liter", "liters", "lb", "lbs", "pound", "pounds", "kg", "g", "gram", "grams", "gal", "gallon",
             "count", "ct", "pack", "packs", "pk", "pcs", "pc", "piece", "pieces", "pair", "pairs", "each", "ea", "roll",
             "rolls", "tube", "tubes", "case", "box", "bundle", "size", "in", "inch", "inches"}
_UR_DROP_CACHE: dict = {}


def _ur_on(name):
    return _ur_os.getenv(name, "1").strip().lower() not in ("0", "false", "off", "no")


def _ur_drop_words():
    lex = _ur_get_lexicon()
    key = getattr(lex, "version", "")
    if key not in _UR_DROP_CACHE:
        words = set(lex.colors) | set(getattr(lex, "tones", ())) | set(_ur_gender_tokens(in_field=True))
        words |= set(lex.sizes) | set(getattr(lex, "line_filler", ())) | _UR_NOISE
        for table in (getattr(lex, "finishes", {}), getattr(lex, "types", {})):
            for phrase in table:
                words |= set(phrase.split())
        _UR_DROP_CACHE[key] = frozenset(words)
    return _UR_DROP_CACHE[key]


def _ur_related(vendor_title, vendor_brand, listing_title):
    """At least half of the vendor title's own words (brand, colors, sizes, units, numbers removed) are in the listing."""
    drop, brand_words = _ur_drop_words(), set(_ur_tokens(vendor_brand))
    core = {t for t in _ur_tokens(vendor_title)
            if len(t) >= 2 and t not in drop and t not in brand_words and not _ur_re.search(r"\d", t)}
    if not core:
        return False
    listing = set(_ur_tokens(listing_title))

    def present(t):
        return t in listing or any(len(t) >= 4 and len(x) >= 4 and (x.startswith(t) or t.startswith(x)) for x in listing)

    return sum(1 for t in core if present(t)) / len(core) >= 0.5


def _ur_scorer_kinds(scores):
    """The kinds of mismatch the scorer itself reported: ['apparel size', 'color'] ..."""
    expl = scores.get("explanation")
    text = str(expl.get("summary") if isinstance(expl, dict) else expl or "")
    kinds = [k.strip().lower() for m in _ur_re.finditer(r"mismatch cap applied \(([^)]*)\)", text, _ur_re.I)
             for k in m.group(1).split(",") if k.strip()]
    for r in scores.get("reasons") or []:
        r = str(r).lower()
        if "mismatch" in r or "conflict" in r:
            kinds.append(r.replace("_mismatch", "").replace("_conflict", "").replace("_", " ").strip())
    return kinds


def _ur_column_states(raw, box):
    """True when the vendor file has a column for this attribute with a value in it (even if the reader missed it)."""
    hint = _UR_COLUMN_HINT.get(box)
    if not hint:
        return False
    for key, value in (raw or {}).items():
        text = str(value if value is not None else "").strip().lower()
        if _ur_re.search(hint, str(key), _ur_re.I) and text not in ("", "nan", "none", "n/a", "na", "-"):
            return True
    return False


def _ur_listing_states(fields, box):
    """Does the listing really state this attribute?  A count of 1 and the gender 'unisex' say nothing."""
    entry = fields.get(box)
    if not entry:
        return False
    value = entry.get("value")
    if box == "count":
        try:
            return float(value) > 1
        except (TypeError, ValueError):
            return False
    if box == "gender":
        return str(value or "").strip().lower() not in ("unisex", "")
    return True


def _ur_shown(entry):
    return f"{entry['value']} ({_UR_SOURCE.get(entry.get('source'), entry.get('source') or '?')})"


def _ur_apply(scores, verdict, source, normalized, extracted, barcode_ok):
    vendor, listing = _ur_extract_vendor(source, extracted), _ur_extract_amazon(normalized)
    vf, af = vendor["fields"], listing["fields"]
    show = lambda f: ", ".join(f"{b.replace('_', ' ')} {_ur_shown(f[b])}" for b in _UR_NOTE if b in f) or "nothing stated"
    columns = ", ".join(str(k) for k in (source.get("raw") or {}))[:150]
    sentence = (f"Attributes - vendor row: {show(vf)}; listing: {show(af)}. Vendor file columns: {columns or 'none'}."
                if _ur_on("MATCH_ATTR_NOTES") else "")
    if _ur_on("MATCH_UNSTATED_REVIEW") and verdict in ("verified", "not_approved"):
        allowed = _ur_variant_boxes_for(listing["identity"].get("category") or vendor["identity"].get("category"))
        unstated = [b for b in _UR_STATED if _ur_listing_states(af, b) and b not in vf
                    and (b not in _UR_BY_CATEGORY or allowed is None or b in allowed)]
        boxes = _ur_compare_boxes(vf, af) if unstated else {}
        explicit = any(boxes.get(b) == "mismatch" for b in _UR_CONTRADICT) or boxes.get("model") == "match"
        # the vendor file has a column for it with a value: the vendor row DOES state it (the reader just missed it)
        in_file = any(_ur_column_states(source.get("raw"), b) for b in unstated)
        move = None
        if unstated and not explicit and not in_file:
            if verdict == "verified" and not barcode_ok:
                move = "demote"
            elif verdict == "not_approved":
                kinds = _ur_scorer_kinds(scores)
                # the scorer's own mismatch is overridden only if it is about something the vendor row does not state
                overridable = all(k in _UR_KIND_BOX and _UR_KIND_BOX[k] not in vf for k in kinds)
                vb = str(source.get("brand") or (extracted or {}).get("brand") or "")
                lb = str(normalized.get("brand") or normalized.get("manufacturer") or "")
                if (not overridable or (vb and lb and not _ur_brands_match(vb, lb))
                        or not _ur_related(source.get("title"), vb, normalized.get("title"))):
                    move = None
                else:
                    move = "rescue"
        if move:
            conf = float(scores.get("confidence_score") or 0)
            scores["confidence_score"] = min(conf, 89.0) if move == "demote" else max(conf, 55.0)
            if "verdict" in scores:
                scores["verdict"] = "review"
            if isinstance(scores.get("reasons"), list):
                scores["reasons"].append("variant_not_stated")
            verdict = "review"
            names = ", ".join(b.replace("_", " ") for b in unstated)
            sentence = (f"{sentence} Needs review: the vendor row does not state {names}, the listing does "
                        f"({', '.join(b.replace('_', ' ') + ' ' + _ur_shown(af[b]) for b in unstated)}), "
                        f"and nothing stated on both sides differs.").strip()
    expl = scores.get("explanation")
    if sentence and isinstance(expl, dict):
        expl["summary"] = f"{expl.get('summary') or ''} {sentence}".strip()
    return scores, verdict


def _score_pair_with_unstated_rule(
    source: dict[str, Any],
    normalized: dict[str, Any],
    *,
    method: str = "weighted",
    mode: str = "cpg",
    extracted: dict[str, Any] | None = None,
    sources: list[str] | None = None,
) -> tuple[dict[str, Any], str]:
    scores, verdict = _score_pair_scorer_only(
        source, normalized, method=method, mode=mode, extracted=extracted, sources=sources)
    try:
        offer, candidate = build_pair(source, normalized, extracted=extracted, sources=sources)
        barcode_ok = bool(offer.upc and candidate.upc
                          and upc_matches(normalize_upc(offer.upc), normalize_upc(candidate.upc)))
        scores, verdict = _ur_apply(scores, verdict, source, normalized, extracted, barcode_ok)
    except Exception:  # a problem here must never break scoring
        pass
    return scores, verdict


_score_pair_scorer_only = score_pair
score_pair = _score_pair_with_unstated_rule

FILE_VERSION = "2026-10-23 adapter v3.1 (count 1 / unisex = not stated; unused MATCH_SCORER_ATTRS removed)"