"""
SP-API credential loading.

Replaces asin-scraper/scripts/config.py. The original module had a
hardcoded fallback `.env` path (`/home/azureuser/projects/keepa-data/.env`)
which is machine-specific and fails silently everywhere else. This
version reads from the process environment only — the FastAPI app loads
`.env` via python-dotenv at startup (main.py), so `os.getenv` picks up
every key we need.

We accept BOTH naming schemes in use across the project:

  asin-scraper style:    AMZ_CLIENT_ID,    AMZ_CLIENT_SECRET,    REFRESH_TOKEN,        AMZ_SELLER_ID
  SP-API docs style:     SP_API_CLIENT_ID, SP_API_CLIENT_SECRET, SP_API_REFRESH_TOKEN, SP_API_SELLER_ID
  spec / Cowork style:   LWA_APP_ID,       LWA_CLIENT_SECRET,    REFRESH_TOKEN,        SELLER_ID

If both are set, the asin-scraper names win (matches existing .env files
in the wild). MARKETPLACE_ID falls back to NA if unset.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class SPAPICredentials:
    client_id: str
    client_secret: str
    refresh_token: str
    seller_id: str | None
    marketplace_id: str


def _first_env(*names: str) -> str | None:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return None


def load_sp_api_credentials() -> SPAPICredentials:
    """
    Read SP-API creds from the environment and return a dataclass.
    Raises RuntimeError if any of the three required values is missing.
    """
    client_id = _first_env("AMZ_CLIENT_ID", "SP_API_CLIENT_ID", "LWA_APP_ID")
    client_secret = _first_env(
        "AMZ_CLIENT_SECRET", "SP_API_CLIENT_SECRET", "LWA_CLIENT_SECRET",
    )
    refresh_token = _first_env("REFRESH_TOKEN", "SP_API_REFRESH_TOKEN")
    seller_id = _first_env("AMZ_SELLER_ID", "SP_API_SELLER_ID", "SELLER_ID")
    marketplace = _first_env("MARKETPLACE_ID", "SP_API_MARKETPLACE_ID") or "ATVPDKIKX0DER"

    missing = [
        n for n, v in [
            ("AMZ_CLIENT_ID / SP_API_CLIENT_ID / LWA_APP_ID", client_id),
            ("AMZ_CLIENT_SECRET / SP_API_CLIENT_SECRET / LWA_CLIENT_SECRET", client_secret),
            ("REFRESH_TOKEN / SP_API_REFRESH_TOKEN", refresh_token),
        ] if not v
    ]
    if missing:
        raise RuntimeError(
            "SP-API credentials are not configured. Missing env var(s): "
            + ", ".join(missing)
            + ". Add them to the .env file at the project root and restart."
        )

    return SPAPICredentials(
        client_id     = client_id,
        client_secret = client_secret,
        refresh_token = refresh_token,
        seller_id     = seller_id,
        marketplace_id = marketplace,
    )


def sp_api_configured() -> bool:
    """Non-raising check — useful for the UI to show a gentle warning."""
    try:
        load_sp_api_credentials()
        return True
    except RuntimeError:
        return False
