"""
Walmart Offer Analysis — the Walmart equivalent of the Amazon "Offer
Analysis" ROI calculator, adapted to what Walmart's public API actually
exposes.

Key difference from the Amazon flow: Walmart has NO live per-item fee
endpoint (confirmed by research — the Price/Item Management APIs are
seller-scoped, post-listing tools; the only fee-bearing endpoint,
Price Incentives, only covers items already in your own enrolled
catalog). Referral fees are only published as a static rate card by
category, so — same as this app's existing Amazon referral-fee formula
(`=IF(price>10, 15%, 8%)`) — we approximate with a static table here
rather than pretending a live lookup exists.

Per the user (2026-09-28): "just pull out the buybox and all the fees,
just to understand the ROI... if only price is available, we can
subtract the bb from the cost from all the fees." So the model is:

    net_profit = walmart_price - vendor_cost - referral_fee - other_fees
    roi        = net_profit / vendor_cost           (0 if vendor_cost <= 0)
    margin     = net_profit / walmart_price          (0 if walmart_price <= 0)

`referral_fee` is looked up from `REFERRAL_RATE_TABLE` by category (case-
insensitive substring match against Walmart's published category names);
falls back to `DEFAULT_REFERRAL_RATE` when the category is unknown/blank.
`other_fees` (e.g. WFS pick/pack/storage) has no confirmed public rate
source yet — it's a caller-supplied override, defaulting to 0, until a
real rate card is sourced and wired in here.
"""
from __future__ import annotations

from dataclasses import dataclass

# A representative slice of Walmart's published referral-fee schedule
# (marketplacelearn.walmart.com "Referral fee schedule for contract
# categories") — NOT exhaustive, NOT queryable live. Extend as needed;
# unknown categories fall back to DEFAULT_REFERRAL_RATE.
REFERRAL_RATE_TABLE: dict[str, float] = {
    "apparel": 0.15,
    "jewelry": 0.20,
    "computers": 0.06,
    "electronics": 0.08,
    "baby": 0.15,
    "health": 0.15,
    "beauty": 0.15,
    "personal care": 0.15,
    "home": 0.15,
    "grocery": 0.15,
    "office": 0.15,
    "sporting goods": 0.15,
    "toys": 0.15,
}
DEFAULT_REFERRAL_RATE = 0.15


def referral_rate_for(category: str) -> float:
    cat = (category or "").strip().lower()
    if not cat:
        return DEFAULT_REFERRAL_RATE
    for key, rate in REFERRAL_RATE_TABLE.items():
        if key in cat:
            return rate
    return DEFAULT_REFERRAL_RATE


@dataclass
class OfferAnalysisResult:
    walmart_price: float | None
    vendor_cost: float
    category: str
    referral_rate: float
    referral_fee: float
    other_fees: float
    net_profit: float | None
    roi: float | None
    margin: float | None
    note: str = ""


def compute_offer_analysis(
    *,
    walmart_price: float | None,
    vendor_cost: float,
    category: str = "",
    other_fees: float = 0.0,
) -> OfferAnalysisResult:
    if walmart_price is None:
        return OfferAnalysisResult(
            walmart_price=None,
            vendor_cost=vendor_cost,
            category=category,
            referral_rate=0.0,
            referral_fee=0.0,
            other_fees=other_fees,
            net_profit=None,
            roi=None,
            margin=None,
            note="No Walmart price found for this item.",
        )

    rate = referral_rate_for(category)
    referral_fee = round(walmart_price * rate, 4)
    net_profit = round(walmart_price - vendor_cost - referral_fee - other_fees, 4)
    roi = round(net_profit / vendor_cost, 4) if vendor_cost > 0 else None
    margin = round(net_profit / walmart_price, 4) if walmart_price > 0 else None

    note = "" if category else "No category supplied — used default referral rate (15%)."

    return OfferAnalysisResult(
        walmart_price=walmart_price,
        vendor_cost=vendor_cost,
        category=category,
        referral_rate=rate,
        referral_fee=referral_fee,
        other_fees=other_fees,
        net_profit=net_profit,
        roi=roi,
        margin=margin,
        note=note,
    )
