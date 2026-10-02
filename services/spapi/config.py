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


# Multiple SP-API "stores" (seller accounts) — added 2026-10-02 so Eligibility
# can check against either account, and the Analytics/Quick-Search catalog
# search can round-robin across both for roughly double the SP-API rate
# (see services/spapi/client.get_multi_store_catalog_api). Each store is its
# own LWA app registration (own client_id/secret/refresh_token) tied to a
# different Amazon seller account, so a second store genuinely needs its own
# full credential set, not just a different seller_id.
_STORES: dict[str, dict] = {
    "default": {
        "label": "Priority Pharmacy",
        "client_id":     ("AMZ_CLIENT_ID", "SP_API_CLIENT_ID", "LWA_APP_ID"),
        "client_secret": ("AMZ_CLIENT_SECRET", "SP_API_CLIENT_SECRET", "LWA_CLIENT_SECRET"),
        "refresh_token": ("REFRESH_TOKEN", "SP_API_REFRESH_TOKEN"),
        "seller_id":     ("AMZ_SELLER_ID", "SP_API_SELLER_ID", "SELLER_ID"),
    },
    "turba": {
        "label": "Turba",
        "client_id":     ("AMZ_CLIENT_TRB__CLIENT_ID", "AMZ_CLIENT_TRB_CLIENT_ID"),
        "client_secret": ("AMZ_CLIENT_TRB_SECRET",),
        "refresh_token": ("AMZ_CLIENT_TRB_REFRESH_TOKEN",),
        "seller_id":     ("AMZ_SELLER_ID_TRB", "AMZ_CLIENT_TRB_SELLER_ID"),
    },
}


def store_label(store: str) -> str:
    return (_STORES.get(store) or _STORES["default"])["label"]


def known_stores() -> list[str]:
    return list(_STORES.keys())


def load_sp_api_credentials(store: str = "default") -> SPAPICredentials:
    """
    Read SP-API creds from the environment and return a dataclass.
    Raises RuntimeError if any of the three required values is missing.

    `store` selects which seller account's credentials to load (see
    `_STORES` above) — defaults to the original single-store env var names
    ("default" = Priority Pharmacy) so every pre-existing caller that doesn't
    pass `store` keeps working exactly as before.
    """
    spec = _STORES.get(store)
    if spec is None:
        raise RuntimeError(f"Unknown SP-API store {store!r}. Known stores: {known_stores()}")

    client_id = _first_env(*spec["client_id"])
    client_secret = _first_env(*spec["client_secret"])
    refresh_token = _first_env(*spec["refresh_token"])
    seller_id = _first_env(*spec["seller_id"])
    marketplace = _first_env("MARKETPLACE_ID", "SP_API_MARKETPLACE_ID") or "ATVPDKIKX0DER"

    missing = [
        n for n, v in [
            ("/".join(spec["client_id"]), client_id),
            ("/".join(spec["client_secret"]), client_secret),
            ("/".join(spec["refresh_token"]), refresh_token),
        ] if not v
    ]
    if missing:
        raise RuntimeError(
            f"SP-API credentials for store {spec['label']!r} are not configured. "
            "Missing env var(s): " + ", ".join(missing)
            + ". Add them to the .env file at the project root and restart."
        )

    return SPAPICredentials(
        client_id     = client_id,
        client_secret = client_secret,
        refresh_token = refresh_token,
        seller_id     = seller_id,
        marketplace_id = marketplace,
    )


def sp_api_configured(store: str = "default") -> bool:
    """Non-raising check — useful for the UI to show a gentle warning."""
    try:
        load_sp_api_credentials(store)
        return True
    except RuntimeError:
        return False


def list_stores() -> list[dict]:
    """For the UI: every known store + whether it's usable right now.

    `seller_id_configured` is called out separately from `configured` because
    client/secret/refresh_token alone are enough for catalog SEARCH (public
    data, no seller scoping), but Eligibility (getListingsRestrictions) also
    needs a real seller_id for that specific store — a store can be
    "configured" (search works) but not yet have a seller_id (eligibility
    doesn't)."""
    out = []
    for key, spec in _STORES.items():
        try:
            creds = load_sp_api_credentials(key)
            out.append({
                "key": key, "label": spec["label"], "configured": True,
                "seller_id_configured": bool(creds.seller_id),
            })
        except RuntimeError:
            out.append({
                "key": key, "label": spec["label"], "configured": False,
                "seller_id_configured": False,
            })
    return out
