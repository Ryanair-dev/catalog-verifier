from __future__ import annotations

import asyncio
import logging
import re
import sys
import time
from typing import Any, Literal

from mcp_server.app import mcp
from mcp_server.mcp_client import AppClientError, app_client
from mcp_server.jobs import JobKind, poll_job

logger = logging.getLogger("catalog_verifier.mcp")
logging.basicConfig(level=logging.INFO, stream=sys.stderr)

AsinCheck = Literal["eligibility", "storage", "generic"]
Store = Literal["default", "turba"]
_CHECK_PATHS = {
    "eligibility": "/api/eligibility/check",
    "storage": "/api/storage-fees/check",
    "generic": "/api/generic-check/check",
}
_MAX_ASINS = 100
_MAX_BRANDS = 100
_MAX_BRAND_SAMPLE = 300


async def _start_asin_check(
    kind: AsinCheck, asins: list[str], store: Store,
) -> dict[str, Any]:
    body: dict[str, Any] = {"asins": asins}
    if kind == "eligibility":
        body["store"] = store
    started = await app_client.post(_CHECK_PATHS[kind], body)
    job_id = started.get("job_id")
    if not isinstance(job_id, str) or not job_id:
        raise AppClientError(f"{kind} check did not return a job ID.")
    return {"job_id": job_id, "total": started.get("total", len(asins))}


async def _brief_job(kind: JobKind, job_id: str) -> dict[str, Any]:
    await asyncio.sleep(0.25)
    try:
        return await poll_job(job_id, kind)
    except AppClientError as exc:
        return {
            "job_id": job_id,
            "kind": kind,
            "status": "status_unavailable",
            "error": str(exc),
            "next_step": f"Poll this job with job_status(job_id='{job_id}', kind='{kind}').",
        }


@mcp.tool()
async def check_asins(
    asins: list[str],
    what: list[AsinCheck] | None = None,
    store: Store = "default",
) -> dict[str, Any]:
    """Use when someone wants to check up to 100 ASINs for listing eligibility, FBA storage fees, or generic/DOG classification. Starts background jobs and returns their IDs; use job_status to check progress and get results."""
    started_at = time.perf_counter()
    ok = False
    selected = list(dict.fromkeys(
        what if what is not None else ["eligibility", "storage", "generic"]
    ))
    try:
        if not asins:
            raise AppClientError("Provide at least one ASIN.")
        if len(asins) > _MAX_ASINS:
            raise AppClientError(
                f"Max {_MAX_ASINS} ASINs per call; split larger lists into separate calls."
            )
        clean_asins = list(dict.fromkeys(asin.strip().upper() for asin in asins if asin.strip()))
        if not clean_asins:
            raise AppClientError("Provide at least one non-empty ASIN.")
        invalid = [asin for asin in clean_asins if not re.fullmatch(r"[A-Z0-9]{10}", asin)]
        if invalid:
            raise AppClientError(f"ASINs must be 10 letters or digits; invalid input: {invalid[0]!r}.")
        if not selected:
            raise AppClientError("Select at least one check: eligibility, storage, or generic.")
        if any(kind not in _CHECK_PATHS for kind in selected):
            raise AppClientError("Checks must be eligibility, storage, or generic.")

        async def start_one(kind: AsinCheck) -> dict[str, Any]:
            try:
                job = await _start_asin_check(kind, clean_asins, store)
                return {"kind": kind, **job}
            except AppClientError as exc:
                return {"kind": kind, "status": "start_failed", "error": str(exc)}

        starts = await asyncio.gather(*(start_one(kind) for kind in selected))
        polls = await asyncio.gather(*(
            _brief_job(result["kind"], result["job_id"])
            if result.get("job_id") else asyncio.sleep(0, result)
            for result in starts
        ))
        ok = any(result.get("job_id") for result in starts)
        succeeded = sum(bool(result.get("job_id")) for result in starts)
        summary = (
            f"Started {succeeded} of {len(selected)} requested checks for "
            f"{len(clean_asins)} ASINs."
        )
        return {"summary": summary, "items": polls}
    finally:
        logger.info(
            "tool=check_asins duration_ms=%d ok=%s asin_count=%d checks=%s",
            round((time.perf_counter() - started_at) * 1000),
            ok,
            len(asins),
            ",".join(selected),
        )


@mcp.tool()
async def check_brand(
    brands: list[str],
    sample_pct: float | None = None,
    sample_size: int = 8,
) -> dict[str, Any]:
    """Use when someone wants to know whether a brand is gated for their seller account. Starts a background check that samples live Amazon ASINs and reports CAN_SELL, NEEDS_APPROVAL, RESTRICTED, MIXED, or ALL_DOG. Use job_status for progress and results."""
    started_at = time.perf_counter()
    ok = False
    try:
        clean_brands = list(dict.fromkeys(brand.strip() for brand in brands if brand.strip()))
        if not clean_brands:
            raise AppClientError("Provide at least one brand name.")
        if len(clean_brands) > _MAX_BRANDS:
            raise AppClientError(f"Max {_MAX_BRANDS} brands per call.")
        if not 1 <= sample_size <= _MAX_BRAND_SAMPLE:
            raise AppClientError(f"sample_size must be between 1 and {_MAX_BRAND_SAMPLE}.")
        if sample_pct is not None and not 0 <= sample_pct <= 100:
            raise AppClientError("sample_pct must be between 0 and 100.")

        body: dict[str, Any] = {"brands": clean_brands, "sample_size": sample_size}
        if sample_pct is not None:
            body["sample_pct"] = sample_pct
        started = await app_client.post("/api/eligibility/check-brand/start", body)
        job_id = started.get("job_id")
        if not isinstance(job_id, str) or not job_id:
            raise AppClientError("Check Brand did not return a job ID.")
        result = await _brief_job("brand_check", job_id)
        result.setdefault("total", started.get("total", len(clean_brands)))
        ok = True
        return {
            "summary": f"Started Check Brand for {len(clean_brands)} brand(s).",
            "items": [result],
        }
    finally:
        logger.info(
            "tool=check_brand duration_ms=%d ok=%s brand_count=%d",
            round((time.perf_counter() - started_at) * 1000),
            ok,
            len(brands),
        )


@mcp.tool()
async def job_status(job_id: str, kind: JobKind) -> dict[str, Any]:
    """Check progress for ASIN/brand checks, Seller Analytics, or PO Analytics. Completed exports are saved in Downloads\\Catalog Verifier (or CV_MCP_DOWNLOAD_DIR)."""
    started_at = time.perf_counter()
    ok = False
    try:
        result = await poll_job(job_id, kind)
        ok = True
        return result
    finally:
        logger.info(
            "tool=job_status duration_ms=%d ok=%s kind=%s",
            round((time.perf_counter() - started_at) * 1000),
            ok,
            kind,
        )
