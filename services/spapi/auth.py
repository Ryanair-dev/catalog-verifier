"""
LWA (Login With Amazon) access-token manager.

Ported verbatim from asin-scraper/scripts/auth.py — no behavioural
changes. Tokens are cached and refreshed 60s before expiry so requests
never race an expired token. All SP-API endpoints require the token
string this class hands out via `get_token()`.
"""
from __future__ import annotations

import time
import requests


class LWATokenManager:
    """
    Manages Login With Amazon (LWA) access tokens.

    Tokens expire in 3600s. This class caches the token and
    refreshes it automatically 60s before expiry so no request
    ever hits the API with a stale token.
    """

    TOKEN_URL = "https://api.amazon.com/auth/o2/token"

    def __init__(self, client_id: str, client_secret: str, refresh_token: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.refresh_token = refresh_token

        self._access_token: str | None = None
        self._expires_at: float = 0

    def get_token(self) -> str:
        """Return a valid access token, refreshing if within 60s of expiry."""
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        return self._refresh()

    def _refresh(self) -> str:
        resp = requests.post(
            self.TOKEN_URL,
            data={
                "grant_type":    "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id":     self.client_id,
                "client_secret": self.client_secret,
            },
            timeout=10,
        )
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise RuntimeError(
                f"LWA token refresh failed [{resp.status_code}]: {resp.text[:200]}"
            ) from e

        data = resp.json()
        self._access_token = data["access_token"]
        self._expires_at = time.time() + data["expires_in"]
        return self._access_token

    @property
    def is_valid(self) -> bool:
        return bool(self._access_token) and time.time() < self._expires_at - 60
