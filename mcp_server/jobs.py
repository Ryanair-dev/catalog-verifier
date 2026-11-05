from __future__ import annotations

import asyncio
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from mcp_server.client import AppClientError, app_client

JobKind = Literal[
    "eligibility", "storage", "generic", "brand_check",
    "seller_analytics", "po_analytics",
]

_JOB_ENDPOINTS = {
    "eligibility": ("/api/eligibility/jobs/{job_id}", "/api/eligibility/jobs/{job_id}/export"),
    "storage": ("/api/storage-fees/jobs/{job_id}", "/api/storage-fees/jobs/{job_id}/export"),
    "generic": ("/api/generic-check/jobs/{job_id}", "/api/generic-check/jobs/{job_id}/export"),
    "brand_check": ("/api/eligibility/check-brand/jobs/{job_id}", None),
    "seller_analytics": (
        "/api/seller-analytics/jobs/{job_id}",
        "/api/seller-analytics/jobs/{job_id}/download",
    ),
    "po_analytics": (
        "/api/po-analytics/jobs/{job_id}",
        "/api/po-analytics/jobs/{job_id}/download",
    ),
}
_EXPORT_EXTENSIONS = {
    "eligibility": ".csv",
    "storage": ".csv",
    "generic": ".csv",
    "seller_analytics": ".xlsx",
    "po_analytics": ".xlsx",
}
_RESULT_FIELDS = (
    "asin", "brand", "title", "verdict", "status", "dog", "matched",
    "qty_case", "qty_case_uom", "ea_case", "product_id", "size_tier",
    "fee_offpeak", "fee_peak", "length_in", "width_in", "height_in",
    "weight_lb", "generic_status", "detail", "summary", "resolved_as",
    "total_asins_in_catalog", "dog_skipped", "confidence",
)


def _download_directory() -> Path:
    configured = os.getenv("CV_MCP_DOWNLOAD_DIR")
    directory = (
        Path(configured).expanduser()
        if configured
        else Path.home() / "Downloads" / "Catalog Verifier"
    )
    return directory.resolve()


def _shape_results(results: Any) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if not isinstance(results, list):
        raise AppClientError("The job returned an invalid results list.")
    counts: Counter[str] = Counter()
    items: list[dict[str, Any]] = []
    for row in results:
        if not isinstance(row, dict):
            raise AppClientError("The job returned an invalid result row.")
        outcome = row.get("verdict") or row.get("status")
        if outcome:
            counts[str(outcome)] += 1
        if len(items) < 10:
            items.append({key: row[key] for key in _RESULT_FIELDS if key in row})
    return items, dict(counts)


async def poll_job(job_id: str, kind: JobKind, *, save_file: bool = True) -> dict[str, Any]:
    if kind not in _JOB_ENDPOINTS:
        raise AppClientError(
            "kind must be eligibility, storage, generic, brand_check, "
            "seller_analytics, or po_analytics."
        )
    if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", job_id):
        raise AppClientError("job_id contains invalid characters.")
    status_path, export_path = _JOB_ENDPOINTS[kind]
    status = await app_client.get(status_path.format(job_id=job_id))
    if not isinstance(status, dict):
        raise AppClientError("The job-status endpoint returned an unexpected response.")

    results, counts = _shape_results(status.get("results", []))
    done = status.get("done", 0)
    total = status.get("total", 0)
    state = str(status.get("status") or "unknown")
    summary = f"{kind} job {job_id} is {state} ({done}/{total} complete)."
    if counts:
        summary += " Results: " + ", ".join(f"{key}: {value}" for key, value in sorted(counts.items())) + "."

    output: dict[str, Any] = {
        "summary": summary,
        "job_id": job_id,
        "kind": kind,
        "status": state,
        "done": done,
        "total": total,
        "counts": counts,
        "items": results,
    }
    if status.get("error"):
        output["error"] = str(status["error"])[:300]

    if save_file and state in {"complete", "done"} and export_path:
        directory = _download_directory()
        file_path = directory / f"{kind}_{job_id}{_EXPORT_EXTENSIONS[kind]}"
        try:
            if not file_path.exists():
                content = await app_client.download(export_path.format(job_id=job_id))
                if not content:
                    raise AppClientError("The completed job export was empty.")
                await asyncio.to_thread(directory.mkdir, parents=True, exist_ok=True)
                await asyncio.to_thread(file_path.write_bytes, content)
            output["file"] = str(file_path)
        except AppClientError as exc:
            output["download_error"] = str(exc)
        except OSError as exc:
            raise AppClientError(f"Could not save the completed {kind} export.") from exc
    return output
