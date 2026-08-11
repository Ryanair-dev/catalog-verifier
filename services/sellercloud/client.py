"""
SellerCloud REST client — auth + catalog reads.

Covers the read paths the Create-PO generator needs (catalog search,
manufacturers, brands). Creation endpoints (POST /api/Products,
/api/Catalog/Imports/Shadows|Kits) will be added once the live probe
confirms the open questions in create_po_design.md §6.

Design notes:
- Token via SellerCloudTokenManager (Authorization: Bearer, injected per request).
- 429 handling with exponential backoff (honours Retry-After when present).
- 401 forces one token refresh + retry (guards against clock skew / early expiry).
- Catalog search filter names are the EXACT ASP.NET model-bound query keys from
  the swagger (`model.sKU`, `model.uPC`, ... — note the lowercased first letter).
- The factory is process-cached so the token is shared across requests.
"""
from __future__ import annotations

import logging
import time
from functools import lru_cache
from typing import Iterator
from urllib.parse import urlparse, parse_qs

import requests

from .auth import SellerCloudTokenManager
from .config import load_sellercloud_credentials

log = logging.getLogger(__name__)


def _catalog_filter_params(
    *,
    sku: str | list[str] | None = None,
    upc: str | None = None,
    keyword: str | None = None,
    brand_ids: list[int] | None = None,
    display_shadows: int | None = None,   # 0 non-shadow, 1 shadow-only, 2 shadow-parent
    selected_kits: int | None = None,     # 0..4 (see swagger)
    active_status: int | None = None,     # 0 no, 1 yes, -1 all
    company_id: list[int] | None = None,
) -> dict:
    """Map friendly kwargs to the exact `model.*` query keys from the spec."""
    p: dict = {}
    if sku is not None:
        p["model.sKU"] = ",".join(sku) if isinstance(sku, list) else sku
    if upc is not None:
        p["model.uPC"] = upc
    if keyword is not None:
        p["model.keyword"] = keyword
    if brand_ids:
        p["model.brandIds"] = brand_ids          # requests repeats the key per value
    if company_id:
        p["model.companyID"] = company_id
    if display_shadows is not None:
        p["model.displayShadows"] = display_shadows
    if selected_kits is not None:
        p["model.selectedKits"] = selected_kits
    if active_status is not None:
        p["model.activeStatus"] = active_status
    return p


class SellerCloudClient:
    def __init__(self, token_manager: SellerCloudTokenManager, base_url: str):
        self.tokens = token_manager
        self.base_url = base_url.rstrip("/")
        self._session = requests.Session()

    # ── internal ─────────────────────────────────────────────────────────
    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.tokens.get_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict | None = None,
        json: dict | None = None,
        max_retries: int = 5,
    ) -> requests.Response:
        url = f"{self.base_url}{path}"
        did_reauth = False

        for attempt in range(max_retries):
            resp = self._session.request(
                method, url,
                headers=self._headers(),
                params=params,
                json=json,
                timeout=30,
            )

            if resp.status_code == 200:
                return resp

            if resp.status_code == 401 and not did_reauth:
                # token rejected despite local validity — force one refresh.
                did_reauth = True
                self.tokens.invalidate()
                continue

            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    wait = int(retry_after)
                else:
                    wait = min(2 ** attempt, 30)
                log.warning("[429] SellerCloud throttled %s (attempt %d). Waiting %ss",
                            path, attempt + 1, wait)
                time.sleep(wait)
                continue

            if resp.status_code in (400, 403):
                raise RuntimeError(
                    f"{resp.status_code} on {method} {path}: {resp.text[:300]}"
                )

            resp.raise_for_status()

        raise RuntimeError(f"Max retries ({max_retries}) exceeded for {method} {path}")

    # ── catalog reads ────────────────────────────────────────────────────
    def search_catalog(self, *, page_number: int = 1, page_size: int = 50, **filters) -> dict:
        """One page. Returns {'Items': [...], 'TotalResults': N}."""
        params = {"model.pageNumber": page_number, "model.pageSize": page_size}
        params.update(_catalog_filter_params(**filters))
        return self._request("GET", "/api/Catalog", params=params).json()

    def iter_catalog(self, *, page_size: int = 100, max_pages: int | None = None, **filters) -> Iterator[dict]:
        """Yield every product across all pages for the given filters."""
        page = 1
        while True:
            data = self.search_catalog(page_number=page, page_size=page_size, **filters)
            items = data.get("Items") or []
            for it in items:
                yield it
            total = data.get("TotalResults") or 0
            if not items or page * page_size >= total:
                break
            if max_pages and page >= max_pages:
                break
            page += 1

    # ── settings lookups ─────────────────────────────────────────────────
    def get_manufacturers(self) -> object:
        """Manufacturer id <-> name map source. Shape confirmed by the probe."""
        return self._request("GET", "/api/Settings/Manufacturers").json()

    def get_brands(self) -> object:
        return self._request("GET", "/api/Settings/Brands").json()

    # ── custom export + queued jobs (bulk catalog pull) ──────────────────
    @staticmethod
    def _job_id_from_export_response(data: dict) -> int:
        jid = data.get("ID")
        if jid:
            return int(jid)
        link = str(data.get("QueuedJobLink") or "")
        # e.g. "/queued-jobs/queued-job-details.aspx?id=2206923"
        qs = parse_qs(urlparse(link).query)
        if qs.get("id"):
            digits = "".join(ch for ch in qs["id"][0] if ch.isdigit())
            if digits:
                return int(digits)
        digits = "".join(ch for ch in link if ch.isdigit())
        if digits:
            return int(digits)
        raise RuntimeError(f"no job id in export response: {str(data)[:200]}")

    def create_custom_export(
        self,
        columns: list[str],
        *,
        file_format: int = 0,               # 0=TAB, 1=CSV, 2=Excel
        product_ids: list[str] | None = None,   # None/[] => whole catalog
        sort_by: str = "",
    ) -> int:
        """Queue a custom catalog export; returns the queued-job id."""
        body = {
            "Columns": [{"OriginalName": c, "DisplayName": c} for c in columns],
            "FileFormat": file_format,
            "SortBy": sort_by,
            "ProductIds": product_ids or [],
        }
        data = self._request("POST", "/api/Catalog/Exports/Custom", json=body).json()
        return self._job_id_from_export_response(data)

    def get_job(self, job_id: int) -> dict:
        return self._request("GET", f"/api/QueuedJobs/{job_id}").json()

    def download_job_output(self, job_id: int) -> bytes:
        return self._request(
            "GET", "/api/QueuedJobs/OutputFile", params={"id": job_id}
        ).content

    def wait_for_job(self, job_id: int, *, timeout: float = 1800, poll: float = 4.0) -> dict:
        """Poll a queued job until its output file is ready; returns the job dto.

        QueuedJob `Basic.Status` is a numeric enum (3 = Completed, observed). The
        definitive "export ready" signal is a non-empty `OutputFile`, so we key on
        that and fall back to status==3."""
        deadline = time.time() + timeout
        while True:
            job = self.get_job(job_id)
            basic = job.get("Basic") or {}
            status = str(basic.get("Status") or "")
            if job.get("OutputFile") or status == "3":
                return job
            err = basic.get("ErrorMessage")
            if err:
                raise RuntimeError(f"job {job_id} failed (status {status}): {err}")
            if time.time() > deadline:
                raise TimeoutError(f"job {job_id} not ready after {timeout}s (status={status})")
            time.sleep(poll)


@lru_cache(maxsize=1)
def get_sellercloud_client() -> SellerCloudClient:
    """Build (or return the cached) SellerCloudClient. Raises RuntimeError if
    creds are not configured — callers should turn that into a 503/warning."""
    creds = load_sellercloud_credentials()
    token_mgr = SellerCloudTokenManager(
        base_url=creds.base_url,
        username=creds.username,
        password=creds.password,
    )
    return SellerCloudClient(token_mgr, creds.base_url)
