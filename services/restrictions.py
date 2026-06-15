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

            resp = requests.get(
                f"{ENDPOINT}{path}",
                headers=self._headers(),
                params=params,
                timeout=15,
            )

            if resp.status_code == 200:
                return resp

            if resp.status_code == 429:
                wait = 2 ** attempt
                log.warning(
                    "[429] Throttled on %s (attempt %d). Backing off %ss.",
                    operation, attempt + 1, wait,
                )
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
            links   = reason.get("links", [])

            approval_url: str | None = None
            can_request = False
            for link in links:
                if link.get("verb") == "REQUEST_APPROVAL":
                    can_request  = True
                    approval_url = link.get("resource")
                    break

            reason_type = _detect_reason_type(message)

            if can_request or reason_type in APPROVABLE_TYPES:
                any_approvable = True

            # Fallback: construct SC approval URL if API didn't return one
            if not approval_url and reason_type in APPROVABLE_TYPES:
                approval_url = (
                    f"https://sellercentral.amazon.com/hz/approvalrequest/"
                    f"restrictions/approve?asin={asin}&marketplaceId={MARKETPLACE_ID}"
                )

            parsed_reasons.append({
                "type":         reason_type,
                "message":      message,
                "hint":         APPROVAL_HINTS.get(reason_type, ""),
                "can_request":  can_request,
                "approval_url": approval_url,
            })

    status = STATUS_NEEDS_APPROVAL if any_approvable else STATUS_RESTRICTED
    return {"asin": asin, "status": status, "reasons": parsed_reasons}


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
