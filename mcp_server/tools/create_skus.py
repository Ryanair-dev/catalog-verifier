from __future__ import annotations

import asyncio
import base64
import binascii
import re
from pathlib import Path
from typing import Any

from mcp_server.app import mcp
from mcp_server.mcp_client import AppClientError, app_client

_FIELDS = {
    "ignore", "asin", "upc", "mpn", "name", "amzpack", "brand", "purchaser", "sourcer",
}
_OPTIONS = {
    "header_row", "sheet_name", "col_map", "batch_type", "create_by", "company",
    "create", "brand_mode", "single_brand", "single_mfr", "single_prefix",
    "single_purchaser", "single_sourcer", "brand_overrides", "purchaser", "sourcer",
    "clean_titles", "edits",
}
_SESSION_RE = re.compile(r"^[a-f0-9]{32}$")


def _file_path(value: str) -> Path:
    if not value.strip():
        raise AppClientError("Provide the absolute path to the catalog file.")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise AppClientError("Provide an absolute path to the catalog file.")
    if path.suffix.lower() not in {".xlsx", ".xlsm", ".xls", ".csv", ".tsv"}:
        raise AppClientError("Create SKUs accepts XLSX, XLSM, XLS, CSV, or TSV files.")
    return path


def _validate_options(options: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(options, dict):
        raise AppClientError("options must be an object.")
    extra = set(options) - _OPTIONS
    if extra:
        raise AppClientError(f"Unknown Create SKUs options: {', '.join(sorted(extra))}.")
    header_row = options.get("header_row", 1)
    if isinstance(header_row, bool) or not isinstance(header_row, int) or header_row < 1:
        raise AppClientError("options.header_row must be a positive, 1-based row number.")
    sheet_name = options.get("sheet_name", "")
    if not isinstance(sheet_name, str):
        raise AppClientError("options.sheet_name must be a string.")
    col_map = options.get("col_map")
    if not isinstance(col_map, dict) or not col_map:
        raise AppClientError("options.col_map must map each input header to an output field.")
    if any(not isinstance(key, str) or not isinstance(value, str) for key, value in col_map.items()):
        raise AppClientError("options.col_map must contain string header names and field names.")
    invalid = set(col_map.values()) - _FIELDS
    if invalid:
        raise AppClientError(f"Unknown column fields: {', '.join(sorted(invalid))}.")
    mapped_fields = [value for value in col_map.values() if value != "ignore"]
    if len(mapped_fields) != len(set(mapped_fields)):
        raise AppClientError("Map each output field from at most one input column.")

    batch_type = options.get("batch_type", "CPG")
    create_by = options.get("create_by", "UPC")
    company = options.get("company", "Ford Medical")
    brand_mode = options.get("brand_mode", "single")
    if batch_type not in {"CPG", "Medical"}:
        raise AppClientError("options.batch_type must be CPG or Medical.")
    if create_by not in {"UPC", "MPN"}:
        raise AppClientError("options.create_by must be UPC or MPN.")
    if company not in {"Ford Medical", "Turba"}:
        raise AppClientError("options.company must be Ford Medical or Turba.")
    if brand_mode not in {"single", "column"}:
        raise AppClientError("options.brand_mode must be single or column.")
    create = options.get("create", {"main": True, "shadow": True, "kit": True})
    if not isinstance(create, dict) or set(create) - {"main", "shadow", "kit"}:
        raise AppClientError("options.create may contain only main, shadow, and kit.")
    if any(not isinstance(value, bool) for value in create.values()):
        raise AppClientError("Every options.create value must be true or false.")
    clean_titles = options.get("clean_titles", False)
    if not isinstance(clean_titles, bool):
        raise AppClientError("options.clean_titles must be true or false.")
    for key in ("brand_overrides", "edits"):
        if key in options and not isinstance(options[key], dict):
            raise AppClientError(f"options.{key} must be an object.")
    return {
        **options,
        "header_row": header_row,
        "sheet_name": sheet_name,
        "batch_type": batch_type,
        "create_by": create_by,
        "company": company,
        "brand_mode": brand_mode,
        "create": create,
        "clean_titles": clean_titles,
    }


def _output_directory(value: str) -> Path:
    directory = (
        Path(value).expanduser()
        if value.strip()
        else Path.home() / "Downloads" / "Catalog Verifier" / "Create SKUs"
    )
    if not directory.is_absolute():
        raise AppClientError("output_dir must be an absolute path.")
    return directory.resolve()


@mcp.tool()
async def create_skus(
    file_path: str = "",
    options: dict[str, Any] | None = None,
    session_token: str = "",
    confirm: bool = False,
    output_dir: str = "",
) -> dict[str, Any]:
    """Prepare SellerCloud import files from a local catalog. First call previews the file and mapping; call again with the returned session_token and confirm=true to generate and save files. It does not write to SellerCloud."""
    opts = _validate_options(options or {})
    directory = _output_directory(output_dir)

    if not session_token:
        if confirm:
            raise AppClientError("Preview the catalog first and pass its session_token before confirming.")
        path = _file_path(file_path)
        preview = await app_client.post_file(
            "/api/create-po/preview", path, {}, timeout=180,
        )
        token = preview.get("token")
        headers = preview.get("headers")
        if not isinstance(token, str) or not isinstance(headers, list):
            raise AppClientError("Create SKUs returned an invalid file preview.")
        if opts["header_row"] != 1 or (
            opts["sheet_name"] and opts["sheet_name"] != preview.get("active_sheet")
        ):
            preview = await app_client.post(
                "/api/create-po/reparse",
                {
                    "token": token,
                    "header_row": opts["header_row"],
                    "sheet_name": opts["sheet_name"] or preview.get("active_sheet", ""),
                },
            )
            headers = preview.get("headers")
            if not isinstance(headers, list):
                raise AppClientError("Create SKUs could not read the selected sheet/header.")
        unknown_headers = set(opts["col_map"]) - set(headers)
        if unknown_headers:
            raise AppClientError(
                f"Mapping contains headers not found in the selected sheet: {', '.join(sorted(unknown_headers))}."
            )
        return {
            "confirmation_required": True,
            "session_token": token,
            "filename": preview.get("filename"),
            "row_count": preview.get("row_count"),
            "headers": headers,
            "sample_rows": preview.get("rows", [])[:5],
            "options": {
                "batch_type": opts["batch_type"],
                "create_by": opts["create_by"],
                "company": opts["company"],
                "create": opts["create"],
                "clean_titles": opts["clean_titles"],
            },
            "warning": (
                "Confirming runs catalog/brand resolution and creates SellerCloud import files. "
                "AI title cleanup is disabled unless options.clean_titles=true. No SellerCloud write occurs."
            ),
            "message": "No files were generated. Review the file, columns, and options; then confirm with this session_token.",
        }

    if not _SESSION_RE.fullmatch(session_token):
        raise AppClientError("session_token is invalid; preview the file again.")
    if not confirm:
        return {
            "confirmation_required": True,
            "session_token": session_token,
            "message": "No files were generated. Pass confirm=true after reviewing the preview.",
        }

    config = {
        "token": session_token,
        "batch_type": opts["batch_type"],
        "create_by": opts["create_by"],
        "company": opts["company"],
        "create": opts["create"],
        "col_map": opts["col_map"],
        "brand_mode": opts["brand_mode"],
        "single_brand": opts.get("single_brand", ""),
        "single_mfr": opts.get("single_mfr", ""),
        "single_prefix": opts.get("single_prefix", ""),
        "single_purchaser": opts.get("single_purchaser", ""),
        "single_sourcer": opts.get("single_sourcer", ""),
        "brand_overrides": opts.get("brand_overrides", {}),
        "purchaser": opts.get("purchaser", {"mode": "value", "value": ""}),
        "sourcer": opts.get("sourcer", {"mode": "value", "value": ""}),
    }
    generated = await app_client.post("/api/create-po/generate", config, timeout=300)
    export = await app_client.post(
        "/api/create-po/export",
        {
            "token": session_token,
            "edits": opts.get("edits", {}),
            "clean_titles": opts["clean_titles"],
        },
        timeout=300,
    )
    files = export.get("files")
    if not isinstance(files, list) or not files:
        raise AppClientError("Create SKUs did not return any import files.")
    saved_files = []
    try:
        directory.mkdir(parents=True, exist_ok=True)
        for item in files:
            if not isinstance(item, dict):
                raise AppClientError("Create SKUs returned an invalid file record.")
            name, encoded = item.get("name"), item.get("b64")
            if not isinstance(name, str) or Path(name).name != name or not name.lower().endswith(".xlsx"):
                raise AppClientError("Create SKUs returned an unsafe or invalid filename.")
            if not isinstance(encoded, str):
                raise AppClientError("Create SKUs returned invalid file contents.")
            try:
                content = base64.b64decode(encoded, validate=True)
            except (ValueError, binascii.Error) as exc:
                raise AppClientError("Create SKUs returned invalid file contents.") from exc
            if not content.startswith(b"PK"):
                raise AppClientError(f"Create SKUs returned an invalid workbook: {name}.")
            target = (directory / name).resolve()
            if target.parent != directory:
                raise AppClientError("Create SKUs returned an unsafe filename.")
            await asyncio.to_thread(target.write_bytes, content)
            saved_files.append(str(target))
    except OSError as exc:
        raise AppClientError(f"Could not save Create SKUs files to {directory}.") from exc
    return {
        "confirmation_required": False,
        "export_id": export.get("export_id"),
        "export_date": export.get("export_date"),
        "counts": generated.get("counts"),
        "files": saved_files,
        "sellercloud_write_performed": False,
    }
