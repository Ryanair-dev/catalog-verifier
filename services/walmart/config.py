"""
Walmart Marketplace API credential loading.

Walmart's OAuth2 client_credentials flow uses two values — Client ID and
Client Secret (Seller Center -> API Integration -> API Key Management).
There's no separate refresh token like SP-API's LWA; both values are
combined into one Basic-Auth header when requesting a token, and the
token itself is short-lived (900s) so it must be re-requested often, not
cached for hours.

Env var names accept a couple of common spellings since the user's own
labels for these two values ("Token DI?" / "APIKEY?") don't match
Walmart's actual naming (Client ID / Client Secret).
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class WalmartCredentials:
    client_id: str
    client_secret: str
    production: bool


def _first_env(*names: str) -> str | None:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return None


def load_walmart_credentials() -> WalmartCredentials:
    client_id = _first_env("WALMART_CLIENT_ID", "WM_CLIENT_ID")
    client_secret = _first_env("WALMART_CLIENT_SECRET", "WM_CLIENT_SECRET")

    missing = [
        n for n, v in [
            ("WALMART_CLIENT_ID", client_id),
            ("WALMART_CLIENT_SECRET", client_secret),
        ] if not v
    ]
    if missing:
        raise RuntimeError(
            "Walmart API credentials are not configured. Missing env var(s): "
            + ", ".join(missing)
            + ". Add them to the .env file at the project root and restart."
        )

    # Default to production per the user's explicit choice (2026-09-28) —
    # "Production for first testing." Set WALMART_SANDBOX=1 to flip to sandbox.
    production = os.getenv("WALMART_SANDBOX", "").strip().lower() not in ("1", "true", "yes")

    return WalmartCredentials(
        client_id=client_id,
        client_secret=client_secret,
        production=production,
    )


def walmart_configured() -> bool:
    """Non-raising check — useful for the UI to show a gentle warning."""
    try:
        load_walmart_credentials()
        return True
    except RuntimeError:
        return False
