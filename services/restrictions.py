"""
Amazon SP-API Listings Restrictions v2021-08-01 client.

Adapted from asin-scraper/scripts/restrictions.py to use catalog-verifier's
shared SP-API infrastructure (auth, rate limiter, credential loader).

Public API:
  get_restrictions_api()  -> RestrictionsAPI  (cached singleton)
  classify(asin, raw)     -> {"asin", "status", "reasons"}
"""
from __future__ import annotations

import logging
import time
from functools import lru_cache

import requests

from .spapi.auth import LWATokenManager
from .spapi.config import load_sp_api_credentials
from .spapi.rate_limiter import LIMITERS

log = logging.getLogger(__name__)

ENDPOINT       = "https://sellingpartnerapi-na.amazon.com"
MARKETPLACE_ID = "ATVPDKIKX0DER"   # US — overridden by creds.marketplace_id

# ── Status constants ──────────────────────────────────────────────────────────
STATUS_CAN_SELL       = "CAN_SELL"
STATUS_NEEDS_APPROVAL = "NEEDS_APPROVAL"
STATUS_RESTRICTED     = "RESTRICTED"

# Reason types where the seller CAN still apply for approval
APPROVABLE_TYPES = {
    "APPROVAL_REQUIRED",
    "BRAND_RESTRICTION",
    "INVOICE_REQUIRED",
    "CATEGORY_RESTRICTION",
    "TRANSPARENCY_REQUIRED",
}

APPROVAL_HINTS: dict[str, str] = {
    "TRANSPARENCY_REQUIRED": (
        "Transparency codes required — enrol in the Transparency programme "
        "and apply a unique code to every unit before shipping."
    ),
    "HAZMAT_RESTRICTION": (
        "Hazmat/dangerous goods review required. Upload a safety data sheet "
        "(SDS) via the Manage Dangerous Goods page."
    ),
    "IP_RESTRICTION": (
        "IP complaint or Brand Registry block. "
        "Contact the brand owner or Seller Support to resolve."
    ),
}


# ── API client ────────────────────────────────────────────────────────────────

class RestrictionsAPI:
    """Wraps the SP-API Listings Restrictions v2021-08-01 endpoint."""

    def __init__(
        self,
        token_manager: LWATokenManager,
        marketplace_id: str = MARKETPLACE_ID,
    ):
        self.tokens = token_manager
        self.marketplace_id = marketplace_id

    def _headers(self) -> dict:
        return {
            "x-amz-access-token": self.tokens.get_token(),
            "Content-Type": "application/json",
        }

    def _get(self, path: str, params: dict, max_retries: int = 5) -> requests.Response:
        operation = "getListingsRestrictions"
        limiter = LIMITERS.get(operation)

        for attempt in range(max_retries):
            if limiter:
                limiter.acquire()

            try:
                resp = requests.get(
                    f"{ENDPOINT}{path}",
                    headers=self._headers(),
                    params=params,
                    timeout=25,
                )
            except requests.RequestException as exc:
                # Transient network error — retry with backoff rather than turning
                # the ASIN into an ERROR row (common during large bulk runs).
                wait = min(30, 2 ** attempt)
                log.warning("[net] %s on %s (attempt %d): %s. Backing off %ss.",
                            type(exc).__name__, operation, attempt + 1, str(exc)[:80], wait)
                time.sleep(wait)
                continue

            if resp.status_code == 200:
                return resp

            if resp.status_code == 429:
                wait = min(30, 2 ** attempt)
                log.warning(
                    "[429] Throttled on %s (attempt %d). Backing off %ss.",
                    operation, attempt + 1, wait,
                )
                time.sleep(wait)
                continue

            if resp.status_code in (500, 502, 503, 504):
                wait = min(30, 2 ** attempt)
                log.warning("[%d] server error on %s (attempt %d). Backing off %ss.",
                            resp.status_code, operation, attempt + 1, wait)
                time.sleep(wait)
                continue

            if resp.status_code == 403:
                raise PermissionError(
                    "403 on getListingsRestrictions — check that your SP-API app "
                    "has the 'Listings' role enabled."
                )

            if resp.status_code == 400:
                raise ValueError(
                    f"400 Bad Request on getListingsRestrictions: {resp.text[:200]}"
                )

            resp.raise_for_status()

        raise RuntimeError(
            f"Max retries ({max_retries}) exceeded for getListingsRestrictions"
        )

    def get_restrictions(
        self,
        asin: str,
        seller_id: str,
        condition_type: str = "new_new",
    ) -> dict:
        """
        Returns the raw restrictions JSON for one ASIN.

        An empty `restrictions` list means the seller can list freely.
        Non-empty means restricted; check `reasons` for details.
        """
        params = {
            "asin":           asin,
            "conditionType":  condition_type,
            "sellerId":       seller_id,
            "marketplaceIds": self.marketplace_id,
        }
        resp = self._get("/listings/2021-08-01/restrictions", params)
        return resp.json()


# ── Result classification ─────────────────────────────────────────────────────

def _detect_reason_type(message: str) -> str:
    msg = message.lower()
    if "transparency" in msg:
        return "TRANSPARENCY_REQUIRED"
    if "invoice" in msg:
        return "INVOICE_REQUIRED"
    if "approval" in msg or "permission" in msg or "ungated" in msg:
        return "APPROVAL_REQUIRED"
    if "brand" in msg:
        return "BRAND_RESTRICTION"
    if "category" in msg:
        return "CATEGORY_RESTRICTION"
    if "hazmat" in msg or "dangerous" in msg:
        return "HAZMAT_RESTRICTION"
    if "ip" in msg or "intellectual property" in msg:
        return "IP_RESTRICTION"
    return "RESTRICTION"


# Amazon's own `reasonCode` on each reason (confirmed live, 2026-09-04: sampled
# real restrictions responses) is the authoritative signal for whether a reason
# is genuinely approvable -- e.g. "APPROVAL_REQUIRED" always came with a real
# `links` entry (a working Seller Central request URL), while "NOT_ELIGIBLE"
# always came with `links: []` and message text like "we are currently not
# accepting applications" -- a hard block, not something the seller can act on.
# Prefer this over guessing from free-text when Amazon gives us the code.
_REASON_CODE_TYPE: dict[str, str] = {
    "APPROVAL_REQUIRED": "APPROVAL_REQUIRED",
    "NOT_ELIGIBLE":       "NOT_ELIGIBLE",
    # Found via a 103-ASIN live sample (2026-09-04): "ASIN does not exist in
    # this marketplace" -- a dead/delisted ASIN, not a brand/category gate.
    # Still correctly resolves to RESTRICTED (can_request=False, no links) but
    # deserves its own label so it doesn't read like a normal brand block.
    "ASIN_NOT_FOUND":     "ASIN_NOT_FOUND",
}


def classify(asin: str, raw_response: dict) -> dict:
    """
    Convert a raw restrictions API response into a structured result dict.

    Returns:
        {
          "asin":    str,
          "status":  "CAN_SELL" | "NEEDS_APPROVAL" | "RESTRICTED",
          "reasons": [
            {
              "type":         str,
              "code":         str,   # Amazon's raw reasonCode, e.g. "NOT_ELIGIBLE"
              "message":      str,
              "hint":         str,
              "can_request":  bool,
              "approval_url": str | None,
            }
          ]
        }
    """
    restrictions = raw_response.get("restrictions", [])

    if not restrictions:
        return {"asin": asin, "status": STATUS_CAN_SELL, "reasons": []}

    parsed_reasons: list[dict] = []
    any_approvable = False

    for restriction in restrictions:
        for reason in restriction.get("reasons", []):
            message = reason.get("message", "")
            code    = reason.get("reasonCode", "")
            links   = reason.get("links", [])

            # BUG FIX (2026-09-04): this used to require `link.get("verb") ==
            # "REQUEST_APPROVAL"`, but Amazon's real `verb` field is the HTTP
            # method ("GET"), never that string -- so `can_request` was DEAD
            # CODE, always False, for every ASIN ever checked. The real signal
            # is simply whether Amazon returned a link at all: it only does so
            # when a self-serve approval action genuinely exists right now.
            approval_url: str | None = links[0].get("resource") if links else None
            can_request = bool(links)

            # Prefer Amazon's own reasonCode (authoritative); fall back to the
            # message-keyword heuristic only for codes we haven't catalogued.
            reason_type = _REASON_CODE_TYPE.get(code) or _detect_reason_type(message)

            # BUG FIX (2026-09-04): status used to be driven by
            # `can_request OR reason_type in APPROVABLE_TYPES` -- since
            # can_request was always False (above) and free-text like "brand
            # restriction" was mis-typed as approvable even for a NOT_ELIGIBLE/
            # "not accepting applications" hard block, this silently turned
            # real RESTRICTED items into NEEDS_APPROVAL (reported by user:
            # B002SV32VI shows RESTRICTED on RevSeller, we showed NEEDS_APPROVAL
            # -- raw response was reasonCode=NOT_ELIGIBLE, links=[]). Now driven
            # solely by the actual presence of a working request link.
            if can_request:
                any_approvable = True

            # Fallback: construct SC approval URL only for reason TYPES that are
            # approvable by nature (still useful if Amazon's API omits the link
            # for an otherwise-legitimate approvable reason) -- never for a
            # reason Amazon already told us has no request path.
            if not approval_url and reason_type in APPROVABLE_TYPES:
                approval_url = (
                    f"https://sellercentral.amazon.com/hz/approvalrequest/"
                    f"restrictions/approve?asin={asin}&marketplaceId={MARKETPLACE_ID}"
                )

            parsed_reasons.append({
                "type":         reason_type,
                "code":         code,
                "message":      message,
                "hint":         APPROVAL_HINTS.get(reason_type, ""),
                "can_request":  can_request,
                "approval_url": approval_url,
            })

    status = STATUS_NEEDS_APPROVAL if any_approvable else STATUS_RESTRICTED
    # DOG (dead/delisted) -- confirmed live (2026-09-04): an ASIN_NOT_FOUND reason
    # 404s on the catalog API too, the same signal storage_fees.py/generic_check.py
    # already use for DOG. Surfaced as its own flag so a deactivated ASIN isn't
    # lumped in with a real brand/category block under RESTRICTED.
    dog = any(r["code"] == "ASIN_NOT_FOUND" for r in parsed_reasons)
    return {"asin": asin, "status": status, "reasons": parsed_reasons, "dog": dog}


# ── Cached singleton factory ──────────────────────────────────────────────────

@lru_cache(maxsize=1)
def get_restrictions_api() -> RestrictionsAPI:
    """
    Build (or return a cached) RestrictionsAPI using the environment credentials.
    Raises RuntimeError if SP-API credentials are not configured.
    """
    creds = load_sp_api_credentials()
    token_mgr = LWATokenManager(
        client_id=creds.client_id,
        client_secret=creds.client_secret,
        refresh_token=creds.refresh_token,
    )
    return RestrictionsAPI(token_mgr, creds.marketplace_id)
