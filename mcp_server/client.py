from __future__ import annotations

import os
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
        timeout: float = 15.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or os.getenv("CV_API_BASE") or "http://localhost:8000").rstrip("/")
        self.timeout = timeout
        self.transport = transport

    async def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        headers: dict[str, str] = {}
        api_token = os.getenv("CATALOG_VERIFIER_API_TOKEN")
        if api_token:
            headers["X-CV-Token"] = api_token

        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                timeout=self.timeout,
                headers=headers,
                transport=self.transport,
            ) as client:
                response = await client.post(path, json=payload)
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

        if response.status_code == 404:
            raise AppClientError(f"Catalog Verifier has no API endpoint at {path}.")
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
        if response.status_code >= 500:
            raise AppClientError(
                f"Catalog Verifier returned an internal error (HTTP {response.status_code})."
            )
        if response.is_error:
            raise AppClientError(
                f"Catalog Verifier rejected the request (HTTP {response.status_code})."
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise AppClientError("Catalog Verifier returned an invalid response.") from exc
        if not isinstance(data, dict):
            raise AppClientError("Catalog Verifier returned an unexpected response shape.")
        return data


app_client = AppClient()
