from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Literal

from mcp_server.app import mcp
from mcp_server.client import AppClientError, app_client
from mcp_server.history_db import explain_saved_pair

_METHODS = {"UPC", "ItemID", "Title"}
_MAPPING_KEYS = {"upc", "itemid", "title", "search_title", "brand"}
_ASIN_RE = re.compile(r"^[A-Z0-9]{10}$")


def _positive_id(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AppClientError(f"{name} must be a positive integer.")
    return value


def _catalog_path(file_path: str) -> Path:
    value = file_path.strip()
    if not value:
        raise AppClientError("Provide the full local catalog file path.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise AppClientError("Provide an absolute path to the catalog file.")
    if path.suffix.lower() not in {".csv", ".tsv", ".xlsx", ".xlsm"}:
        raise AppClientError("Catalog file must be CSV, TSV, XLSX, or XLSM.")
    return path


def _settings_fields(
    settings: dict[str, Any],
    *,
    name: str = "",
    estimate: bool = False,
) -> dict[str, str]:
    if not isinstance(settings, dict):
        raise AppClientError("settings must be an object.")
    allowed = {
        "header_row", "mapping", "brand", "search_methods", "pages_per_title",
        "sheet_name", "use_query_agent", "use_llm_verifier", "ai_clean_titles",
        "marketplace", "max_rank", "min_rank", "vetting_mode", "scoring_method",
        "use_ai_reader", "passthrough_cols",
    }
    extra = set(settings) - allowed
    if extra:
        raise AppClientError(f"Unknown settings: {', '.join(sorted(extra))}.")

    header_row = settings.get("header_row")
    if isinstance(header_row, bool) or not isinstance(header_row, int) or header_row < 0:
        raise AppClientError("settings.header_row must be a zero-based non-negative integer.")

    mapping = settings.get("mapping")
    if not isinstance(mapping, dict) or not mapping:
        raise AppClientError("settings.mapping must map catalog fields to zero-based column indexes.")
    invalid_keys = set(mapping) - _MAPPING_KEYS
    if invalid_keys:
        raise AppClientError(f"Unknown mapping fields: {', '.join(sorted(invalid_keys))}.")
    normalized_mapping: dict[str, str] = {}
    for key, value in mapping.items():
        if value in ("", None):
            continue
        if isinstance(value, bool) or not isinstance(value, (str, int)):
            raise AppClientError(f"Column index for mapping.{key} must be a non-negative integer.")
        try:
            column = int(value)
        except ValueError as exc:
            raise AppClientError(f"Column index for mapping.{key} must be a non-negative integer.") from exc
        if column < 0:
            raise AppClientError(f"Column index for mapping.{key} must be a non-negative integer.")
        normalized_mapping[key] = str(column)
    if not any(key in normalized_mapping for key in ("upc", "itemid", "title")):
        raise AppClientError("Map at least one of UPC, Item ID, or Title.")

    methods = settings.get("search_methods")
    if not isinstance(methods, list) or not methods:
        raise AppClientError("settings.search_methods must be a non-empty list of UPC, ItemID, and/or Title.")
    if any(not isinstance(method, str) or method not in _METHODS for method in methods):
        raise AppClientError("Search methods must be UPC, ItemID, and/or Title.")
    if len(set(methods)) != len(methods):
        raise AppClientError("Search methods must not contain duplicates.")

    pages = settings.get("pages_per_title", 0)
    if isinstance(pages, bool) or not isinstance(pages, int) or pages < 0 or pages > 100:
        raise AppClientError("settings.pages_per_title must be an integer from 0 to 100.")
    use_ai_reader = settings.get("use_ai_reader", False)
    if not isinstance(use_ai_reader, bool):
        raise AppClientError("settings.use_ai_reader must be true or false.")
    fields = {
        "header_row": str(header_row),
        "mapping": json.dumps(normalized_mapping),
        "brand": str(settings.get("brand") or ""),
        "search_methods": json.dumps(methods),
        "pages_per_title": str(pages),
        "sheet_name": str(settings.get("sheet_name") or ""),
    }

    for key in ("use_query_agent", "use_llm_verifier", "ai_clean_titles"):
        value = settings.get(key, False)
        if not isinstance(value, bool):
            raise AppClientError(f"settings.{key} must be true or false.")
        fields[key] = str(value).lower()

    if estimate:
        return fields

    for key in ("max_rank", "min_rank"):
        value = settings.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AppClientError(f"settings.{key} must be a non-negative integer.")
        fields[key] = str(value)
    for key, default, choices in (
        ("marketplace", "US", None),
        ("vetting_mode", "cpg", {"cpg", "medical"}),
        ("scoring_method", "weighted", {"weighted", "cascade", "classifier"}),
    ):
        value = settings.get(key, default)
        if not isinstance(value, str) or not value.strip():
            raise AppClientError(f"settings.{key} must be a non-empty string.")
        value = value.strip()
        if choices and value not in choices:
            raise AppClientError(f"settings.{key} must be one of {', '.join(sorted(choices))}.")
        fields[key] = value
    fields["use_ai_reader"] = str(use_ai_reader).lower()

    passthrough = settings.get("passthrough_cols", [])
    if not isinstance(passthrough, list) or any(not isinstance(item, str) for item in passthrough):
        raise AppClientError("settings.passthrough_cols must be a list of column names.")
    fields["passthrough_cols"] = json.dumps(passthrough)
    fields["name"] = name.strip() or "Catalog Verifier MCP run"
    return fields


def _run_summary(run: dict[str, Any]) -> dict[str, Any]:
    return {
        key: run.get(key)
        for key in (
            "id", "name", "status", "progress_phase", "progress_done",
            "progress_total", "total_catalog_items", "total_candidates_found",
            "verified_count", "review_count", "not_approved_count",
            "marketplace", "created_at", "updated_at",
        )
    }


async def _estimate(path: Path, fields: dict[str, str]) -> dict[str, Any]:
    result = await app_client.post_file(
        "/api/analytics/estimate", path, fields, timeout=180,
    )
    if not isinstance(result.get("rows"), int) or not isinstance(result.get("total"), dict):
        raise AppClientError("Offer Analysis returned an invalid estimate.")
    return result


@mcp.tool()
async def list_runs(limit: int = 10) -> dict[str, Any]:
    """List recent Offer Analysis runs with their status, progress, and result counts. This uses the safe runs-list API, not the detail endpoint."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 50:
        raise AppClientError("limit must be an integer from 1 to 50.")
    result = await app_client.get("/api/analytics/runs")
    if not isinstance(result, list) or any(not isinstance(run, dict) for run in result):
        raise AppClientError("Offer Analysis returned an invalid runs list.")
    return {"runs": [_run_summary(run) for run in result[:limit]], "count": min(len(result), limit)}


@mcp.tool()
async def run_status(run_id: int) -> dict[str, Any]:
    """Check status and progress for one Offer Analysis run without loading or repairing its candidate details."""
    run_id = _positive_id(run_id, "run_id")
    result = await app_client.get("/api/analytics/runs")
    if not isinstance(result, list) or any(not isinstance(run, dict) for run in result):
        raise AppClientError("Offer Analysis returned an invalid runs list.")
    run = next((item for item in result if item.get("id") == run_id), None)
    if run is None:
        raise AppClientError(f"Offer Analysis run {run_id} was not found.")
    summary = _run_summary(run)
    done, total = summary.get("progress_done"), summary.get("progress_total")
    summary["progress_percent"] = (
        round(100 * done / total, 1)
        if isinstance(done, (int, float)) and isinstance(total, (int, float)) and total > 0
        else None
    )
    return summary


@mcp.tool()
async def explain_pair(run_id: int, row: int, asin: str) -> dict[str, Any]:
    """Read the saved scoring explanation for one Offer Analysis candidate without changing or rescoring it."""
    run_id = _positive_id(run_id, "run_id")
    if isinstance(row, bool) or not isinstance(row, int) or row < 1:
        raise AppClientError("row must be a positive, 1-based catalog row number.")
    asin = asin.strip().upper()
    if not _ASIN_RE.fullmatch(asin):
        raise AppClientError("asin must be exactly 10 letters or digits.")
    try:
        return await asyncio.to_thread(explain_saved_pair, run_id, row, asin)
    except (FileNotFoundError, LookupError, ValueError, RuntimeError) as exc:
        raise AppClientError(str(exc)) from exc


@mcp.tool()
async def estimate_run(file_path: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Estimate Offer Analysis time and AI cost for a local catalog file. It parses the file but makes no Amazon calls and creates no run."""
    path = _catalog_path(file_path)
    fields = _settings_fields(settings, estimate=True)
    result = await _estimate(path, fields)
    if settings.get("use_ai_reader"):
        result["warnings"] = [
            "The AI title reader is enabled for the run, but the app's estimate endpoint does not include that stage in its time/cost estimate."
        ]
    return result


@mcp.tool()
async def start_run(
    file_path: str,
    settings: dict[str, Any],
    confirm: bool = False,
) -> dict[str, Any]:
    """Estimate and start an Offer Analysis run from a local catalog. First call with confirm=false to review the estimate; call again with confirm=true to start SP-API/AI work."""
    path = _catalog_path(file_path)
    fields = _settings_fields(settings, name=path.stem, estimate=False)
    estimate_fields = _settings_fields(settings, estimate=True)
    estimate = await _estimate(path, estimate_fields)
    response: dict[str, Any] = {
        "estimate": estimate,
        "confirmation_required": not confirm,
    }
    if settings.get("use_ai_reader"):
        response["warning"] = (
            "The AI title reader is enabled, but is not included in the app's estimate."
        )
    if not confirm:
        response["message"] = (
            "No run was started. Review the estimate, then call start_run again with confirm=true "
            "to begin Amazon searches and any selected AI stages."
        )
        return response

    app_status = await app_client.get("/api/analytics/status")
    if not isinstance(app_status, dict) or app_status.get("sp_api_configured") is not True:
        raise AppClientError("Offer Analysis cannot start because SP-API is not configured in the app.")
    result = await app_client.post_file(
        "/api/analytics/runs", path, fields, timeout=180,
    )
    if not result.get("run_id"):
        raise AppClientError("Offer Analysis did not return a run ID.")
    response.update({"confirmation_required": False, "started": True, "run": result})
    return response


@mcp.tool()
async def export_run(
    run_id: int,
    confirm: bool = False,
    output_dir: str = "",
) -> dict[str, Any]:
    """Download a run's Excel workbook to the local computer. Requires confirm=true because it creates a local file."""
    run_id = _positive_id(run_id, "run_id")
    target_dir = (
        Path(output_dir).expanduser()
        if output_dir.strip()
        else Path.home() / "Downloads" / "Catalog Verifier"
    )
    if not target_dir.is_absolute():
        raise AppClientError("output_dir must be an absolute path.")
    if not confirm:
        return {
            "confirmation_required": True,
            "run_id": run_id,
            "output_file": str((target_dir / f"analytics_run_{run_id}.xlsx").resolve()),
            "message": "No file was downloaded. Call export_run again with confirm=true to save the Excel workbook.",
        }
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        output_file = (target_dir / f"analytics_run_{run_id}.xlsx").resolve()
        content = await app_client.download(f"/api/analytics/runs/{run_id}/export", timeout=180)
        if not content:
            raise AppClientError("Offer Analysis returned an empty export.")
        if not content.startswith(b"PK"):
            raise AppClientError("Offer Analysis returned an invalid Excel workbook.")
        await asyncio.to_thread(output_file.write_bytes, content)
    except OSError as exc:
        raise AppClientError(f"Could not save the export file to {target_dir}.") from exc
    return {"confirmation_required": False, "run_id": run_id, "file_path": str(output_file)}


@mcp.tool()
async def report_match(
    run_id: int,
    row: int,
    asin: str,
    label: Literal["same", "different"],
    reasons: list[str] | None = None,
    note: str = "",
    correct_asin: str = "",
    reported_by: str = "",
    confirm: bool = False,
) -> dict[str, Any]:
    """Report a wrong or missed Offer Analysis match, using the same feedback endpoint as the app's report button. row is the 1-based catalog row number; requires confirm=true to submit."""
    run_id = _positive_id(run_id, "run_id")
    if isinstance(row, bool) or not isinstance(row, int) or row < 1:
        raise AppClientError("row must be a positive, 1-based catalog row number.")
    asin = asin.strip().upper()
    if not _ASIN_RE.fullmatch(asin):
        raise AppClientError("asin must be exactly 10 letters or digits.")
    if label not in ("same", "different"):
        raise AppClientError("label must be 'same' or 'different'.")
    note = note.strip()
    if len(note) > 1000:
        raise AppClientError("note must be 1000 characters or fewer.")
    correct_asin = correct_asin.strip().upper()
    if correct_asin and not _ASIN_RE.fullmatch(correct_asin):
        raise AppClientError("correct_asin must be exactly 10 letters or digits.")
    reported_by = reported_by.strip()
    if len(reported_by) > 60:
        raise AppClientError("reported_by must be 60 characters or fewer.")
    if reasons is None:
        reasons = []
    if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
        raise AppClientError("reasons must be a list of reason codes.")

    available = await app_client.get("/api/feedback/reasons")
    if not isinstance(available, dict) or not isinstance(available.get(label), list):
        raise AppClientError("Feedback API returned an invalid reason list.")
    valid_codes = {
        item.get("code") for item in available[label]
        if isinstance(item, dict) and isinstance(item.get("code"), str)
    }
    if any(code not in valid_codes for code in reasons):
        raise AppClientError(f"Choose reason codes listed for label '{label}'.")
    if not reasons and not note:
        raise AppClientError("Provide at least one reason or a note.")
    payload = {
        "run_id": run_id,
        "row_idx": row - 1,
        "asin": asin,
        "label": label,
        "reasons": reasons,
        "note": note,
        "correct_asin": correct_asin,
        "reported_by": reported_by,
    }
    if not confirm:
        return {
            "confirmation_required": True,
            "report": payload,
            "message": "No feedback was submitted. Call report_match again with confirm=true to send this report.",
        }
    result = await app_client.post("/api/feedback/report", payload)
    return {"confirmation_required": False, "submitted": True, **result}
