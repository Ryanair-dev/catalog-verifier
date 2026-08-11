"""
SellerCloud access-token manager.

Mirrors services/spapi/auth.py (LWATokenManager). SellerCloud issues a
bearer token from POST {base}/api/token with a JSON body {Username, Password};
the response carries access_token + expires_in (seconds). We cache the token
and refresh 60s before expiry so no request races an expired token.
"""
from __future__ import annotations

import time
import requests


class SellerCloudTokenManager:
    """Manages the SellerCloud bearer token. `get_token()` always returns a
    valid token, refreshing automatically within 60s of expiry."""

    def __init__(self, base_url: str, username: str, password: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password

        self._access_token: str | None = None
        self._expires_at: float = 0.0

    def get_token(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        return self._refresh()

    def invalidate(self) -> None:
        """Drop the cached token so the next get_token() forces a refresh.
        Used when the server returns 401 despite a locally-valid token."""
        self._access_token = None
        self._expires_at = 0.0

    def _refresh(self) -> str:
        resp = requests.post(
            f"{self.base_url}/api/token",
            json={"Username": self.username, "Password": self.password},
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        try:
            resp.raise_for_status()
        except requests.HTTPError as e:
            raise RuntimeError(
                f"SellerCloud token request failed [{resp.status_code}]: "
                f"{resp.text[:200]}"
            ) from e

        data = resp.json()
        token = data.get("access_token")
        if not token:
            raise RuntimeError(
                f"SellerCloud token response had no access_token: {str(data)[:200]}"
            )
        self._access_token = token
        # expires_in is seconds; default to 1h if the field is absent.
        self._expires_at = time.time() + float(data.get("expires_in", 3600))
        return token

    @property
    def is_valid(self) -> bool:
        return bool(self._access_token) and time.time() < self._expires_at - 60
