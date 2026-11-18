"""
Walmart Marketplace API OAuth2 token manager (client_credentials flow).

Unlike SP-API's LWA (a refresh token exchanged for a 1hr access token),
Walmart's token is requested directly from the Client ID/Secret pair via
HTTP Basic Auth, and is valid for only 900s (15 min) — so this manager
must be willing to re-auth far more often than the SP-API one.

Docs: https://developer.walmart.com/us-marketplace/docs/oauth-authentication
      https://developer.walmart.com/us-marketplace/reference/tokenapi
"""
from __future__ import annotations

import base64
import time
import uuid

import requests

PROD_TOKEN_URL = "https://marketplace.walmartapis.com/v3/token"
SANDBOX_TOKEN_URL = "https://sandbox.walmartapis.com/v3/token"

SVC_NAME = "Walmart Marketplace"


class WalmartTokenManager:
    """Caches the 900s Walmart access token, refreshing 60s before expiry."""

    def __init__(self, client_id: str, client_secret: str, production: bool = True):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = PROD_TOKEN_URL if production else SANDBOX_TOKEN_URL

        self._access_token: str | None = None
        self._expires_at: float = 0

    def get_token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        return self._refresh()

    def _refresh(self) -> str:
        basic = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        resp = requests.post(
            self.token_url,
            headers={
                "Authorization": f"Basic {basic}",
                "Accept": "application/json",
                "WM_SVC.NAME": SVC_NAME,
                "WM_QOS.CORRELATION_ID": str(uuid.uuid4()),
                "Content-Type": "application/x-www-form-urlencoded",
            },
            data={"grant_type": "client_credentials"},
            timeout=15,
        )
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise RuntimeError(
                f"Walmart token request failed [{resp.status_code}]: {resp.text[:300]}"
            ) from e

        data = resp.json()
        self._access_token = data["access_token"]
        self._expires_at = time.time() + int(data.get("expires_in", 900))
        return self._access_token

    @property
    def is_valid(self) -> bool:
        return bool(self._access_token) and time.time() < self._expires_at - 60
