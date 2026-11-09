from __future__ import annotations

import os
import asyncio
from pathlib import Path
from typing import Any

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")


class AppClientError(Exception):
    """A readable error returned to the MCP client."""


class AppClient:
    def __init__(
        self,
        base_url: str | None = None,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("CV_API_BASE") or "http://localhost:8000").rstrip("/")
        self.timeout = timeout
        self.transport = transport

    async def _request(
        self,
        method: str,
        path: str,
        *,
        payload: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        headers: dict[str, str] = {}
        api_token = os.getenv("CATALOG_VERIFIER_API_TOKEN")
        if api_token:
            headers["X-CV-Token"] = api_token

        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=timeout or self.timeout,
                headers=headers,
                transport=self.transport,
            ) as client:
                response = await client.request(
                    method, path, json=payload, params=params, data=data, files=files,
                )
        except httpx.ConnectError as exc:
            raise AppClientError(
                f"The Catalog Verifier app is not running at {self.base_url}. "
                "Start it, then try again."
            ) from exc
        except httpx.TimeoutException as exc:
            raise AppClientError(
                f"The Catalog Verifier app at {self.base_url} did not respond in time."
            ) from exc
        except httpx.RequestError as exc:
            raise AppClientError(
                f"Could not reach the Catalog Verifier app at {self.base_url}."
            ) from exc

        self._raise_for_status(response, path)
        return response

    @staticmethod
    def _raise_for_status(response: httpx.Response, path: str) -> None:
        if response.status_code == 404:
            raise AppClientError(f"Catalog Verifier could not find the requested resource at {path}.")
        if response.status_code in (401, 403):
            raise AppClientError(
                "Catalog Verifier rejected the request. Check CATALOG_VERIFIER_API_TOKEN "
                "for the MCP server."
            )
        if response.status_code == 400:
            try:
                detail = response.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            message = str(detail).strip() if detail else "The request was invalid."
            raise AppClientError(f"Catalog Verifier rejected the request: {message[:300]}")
        if response.status_code in (413, 422):
            try:
                detail = response.json().get("detail")
            except (ValueError, AttributeError):
                detail = None
            message = str(detail).strip() if detail else "The request was invalid."
            raise AppClientError(f"Catalog Verifier rejected the request: {message[:300]}")
        if response.status_code >= 500:
            raise AppClientError(
                f"Catalog Verifier returned an internal error (HTTP {response.status_code})."
            )
        if response.is_error:
            raise AppClientError(
                f"Catalog Verifier rejected the request (HTTP {response.status_code})."
            )

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            data = response.json()
        except ValueError as exc:
            raise AppClientError("Catalog Verifier returned an invalid response.") from exc
        if not isinstance(data, dict):
            raise AppClientError("Catalog Verifier returned an unexpected response shape.")
        return data

    async def post(
        self, path: str, payload: dict[str, Any], *, timeout: float | None = None,
    ) -> dict[str, Any]:
        response = await self._request("POST", path, payload=payload, timeout=timeout)
        return self._json_object(response)

    async def get(
        self,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any] | list[Any]:
        response = await self._request("GET", path, params=params, timeout=timeout)
        try:
            data = response.json()
        except ValueError as exc:
            raise AppClientError("Catalog Verifier returned an invalid response.") from exc
        if not isinstance(data, (dict, list)):
            raise AppClientError("Catalog Verifier returned an unexpected response shape.")
        return data

    async def download(self, path: str, *, timeout: float | None = None) -> bytes:
        response = await self._request("GET", path, timeout=timeout)
        return response.content

    async def post_download(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        timeout: float | None = None,
    ) -> bytes:
        response = await self._request("POST", path, payload=payload, timeout=timeout)
        return response.content

    async def post_file(
        self,
        path: str,
        file_path: Path,
        fields: dict[str, str],
        *,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        max_mb_raw = os.getenv("CATALOG_VERIFIER_MAX_UPLOAD_MB", "25")
        try:
            max_bytes = max(1, int(max_mb_raw)) * 1024 * 1024
        except ValueError:
            max_bytes = 25 * 1024 * 1024
        try:
            size = await asyncio.to_thread(file_path.stat)
        except OSError as exc:
            raise AppClientError(f"Could not read the file at {file_path}.") from exc
        if not file_path.is_file():
            raise AppClientError(f"The file path is not a regular file: {file_path}.")
        if size.st_size > max_bytes:
            raise AppClientError(
                f"File is too large ({size.st_size} bytes). App limit is {max_bytes // (1024 * 1024)} MB."
            )
        try:
            content = await asyncio.to_thread(file_path.read_bytes)
        except OSError as exc:
            raise AppClientError(f"Could not read the file at {file_path}.") from exc
        if len(content) > max_bytes:
            raise AppClientError(
                f"File is too large ({len(content)} bytes). App limit is {max_bytes // (1024 * 1024)} MB."
            )
        response = await self._request(
            "POST",
            path,
            data=fields,
            files={
                "catalog_file": (
                    file_path.name,
                    content,
                    "application/octet-stream",
                )
            },
            timeout=timeout,
        )
        return self._json_object(response)


app_client = AppClient()
