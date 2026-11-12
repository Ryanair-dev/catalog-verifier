from __future__ import annotations

from collections import Counter
from typing import Any


MAX_ITEMS = 10
VERDICTS = ("verified", "review", "not_approved")


def format_quick_search_result(payload: dict[str, Any], searched_for: str) -> dict[str, Any]:
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("Quick Search returned an unexpected candidates field.")

    total = payload.get("total")
    if not isinstance(total, int) or total < 0:
        raise ValueError("Quick Search returned an invalid candidate count.")

    counts: Counter[str] = Counter()
    items: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("Quick Search returned an invalid candidate.")

        verdict = str(candidate.get("verdict") or "unknown")
        counts[verdict] += 1
        if len(items) >= MAX_ITEMS:
            continue

        item = {
            "asin": candidate.get("asin") or "",
            "title": candidate.get("title") or "",
            "brand": candidate.get("brand") or "",
            "confidence": candidate.get("confidence"),
            "verdict": verdict,
            "bsr": candidate.get("sales_rank"),
            "price": candidate.get("list_price"),
            "storage_fee": candidate.get("storage_fee_offpeak"),
            "storage_fee_peak": candidate.get("storage_fee_peak"),
        }
        if item["asin"]:
            item["amazon_url"] = f"https://www.amazon.com/dp/{item['asin']}"
        items.append(item)

    counts_by_verdict = {verdict: counts[verdict] for verdict in VERDICTS}
    for verdict, count in counts.items():
        if verdict not in counts_by_verdict:
            counts_by_verdict[verdict] = count

    if items:
        best = items[0]
        summary = (
            f"{total} candidate{'s' if total != 1 else ''} found for {searched_for}; "
            f"best: {best['asin']} ({best['verdict']}, {best['confidence']}%)."
        )
    else:
        summary = f"No candidates found for {searched_for}."

    return {
        "summary": summary,
        "counts": counts_by_verdict,
        "items": items,
    }
