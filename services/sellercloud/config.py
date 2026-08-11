"""
SellerCloud REST API credential loading.

Mirrors services/spapi/config.py: reads from the process environment only
(main.py loads `.env` via python-dotenv at startup, so `os.getenv` picks up
every key). The base URL defaults to the `tt` tenant so only username +
password are strictly required.

Env vars:
  SELLERCLOUD_USERNAME            (required)  API user, e.g. someone@fordmed.com
  SELLERCLOUD_PASSWORD            (required)  API user password
  SELLERCLOUD_API_BASE           (optional)  defaults to the tt tenant below
  SELLERCLOUD_DEFAULT_COMPANY_ID (optional)  default CompanyId for shadow imports
"""
from __future__ import annotations

import os
from dataclasses import dataclass

# FordMed's server is `fml` on the Delta platform (login: fml.delta.sellercloud.com).
# basePath is /rest; API paths add /api/...  (`tt` in SellerCloud's docs is only an example.)
DEFAULT_BASE_URL = "https://fml.api.sellercloud.com/rest"


@dataclass(frozen=True)
class SellerCloudCredentials:
    base_url: str
    username: str
    password: str
    default_company_id: int | None


def _first_env(*names: str) -> str | None:
    for n in names:
        v = os.getenv(n)
        if v:
            return v.strip()
    return None


def load_sellercloud_credentials() -> SellerCloudCredentials:
    """Read SellerCloud creds from the environment. Raises RuntimeError if
    username or password is missing."""
    username = _first_env("SELLERCLOUD_USERNAME", "SELLERCLOUD_USER", "SC_USERNAME")
    password = _first_env("SELLERCLOUD_PASSWORD", "SC_PASSWORD")
    base_url = (
        _first_env("SELLERCLOUD_API_BASE", "SELLERCLOUD_BASE_URL", "SC_API_BASE")
        or DEFAULT_BASE_URL
    ).rstrip("/")
    company_raw = _first_env("SELLERCLOUD_DEFAULT_COMPANY_ID", "SC_COMPANY_ID")

    missing = [
        n for n, v in [
            ("SELLERCLOUD_USERNAME", username),
            ("SELLERCLOUD_PASSWORD", password),
        ] if not v
    ]
    if missing:
        raise RuntimeError(
            "SellerCloud credentials are not configured. Missing env var(s): "
            + ", ".join(missing)
            + ". Add them to the .env file at the project root and restart."
        )

    company_id: int | None = None
    if company_raw and company_raw.isdigit():
        company_id = int(company_raw)

    return SellerCloudCredentials(
        base_url=base_url,
        username=username,   # type: ignore[arg-type]
        password=password,   # type: ignore[arg-type]
        default_company_id=company_id,
    )


def sellercloud_configured() -> bool:
    """Non-raising check — for the UI / status endpoints."""
    try:
        load_sellercloud_credentials()
        return True
    except RuntimeError:
        return False
