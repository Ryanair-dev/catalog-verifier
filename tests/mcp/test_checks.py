from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mcp_server.client import AppClientError
from mcp_server.jobs import poll_job
from mcp_server.tools import checks


class CheckAsinsTests(unittest.IsolatedAsyncioTestCase):
    async def test_enforces_100_asin_limit(self) -> None:
        with self.assertRaisesRegex(AppClientError, "Max 100"):
            await checks.check_asins(asins=[f"B{i:09d}" for i in range(101)], what=["generic"])

    async def test_starts_selected_jobs_and_returns_ids(self) -> None:
        starts: list[tuple[str, dict]] = []

        async def post(path: str, payload: dict) -> dict:
            starts.append((path, payload))
            kind = path.split("/")[2]
            return {"job_id": f"{kind}-job-1", "total": len(payload["asins"])}

        async def poll(job_id: str, kind: str, **kwargs) -> dict:
            return {
                "summary": f"{kind} running",
                "job_id": job_id,
                "kind": kind,
                "status": "running",
                "done": 0,
                "total": 2,
                "items": [],
            }

        client = type("FakeClient", (), {"post": AsyncMock(side_effect=post)})()
        with (
            patch.object(checks, "app_client", client),
            patch.object(checks, "poll_job", AsyncMock(side_effect=poll)),
            patch.object(checks.asyncio, "sleep", AsyncMock()),
        ):
            result = await checks.check_asins(
                asins=["B000000001", "B000000002"],
                what=["eligibility", "storage"],
                store="turba",
            )

        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[0][1]["store"], "turba")
        self.assertEqual([item["job_id"] for item in result["items"]], [
            "eligibility-job-1", "storage-fees-job-1",
        ])

    async def test_rejects_malformed_asin(self) -> None:
        with self.assertRaisesRegex(AppClientError, "10 letters or digits"):
            await checks.check_asins(asins=["not-an-asin"], what=["storage"])


class CheckBrandTests(unittest.IsolatedAsyncioTestCase):
    async def test_starts_brand_job_with_sampling_and_returns_job_result(self) -> None:
        client = type("FakeClient", (), {
            "post": AsyncMock(return_value={"job_id": "brand-job-1", "total": 2})
        })()
        poll = {
            "summary": "brand check running",
            "job_id": "brand-job-1",
            "kind": "brand_check",
            "status": "running",
            "done": 0,
            "total": 2,
            "items": [],
        }
        with (
            patch.object(checks, "app_client", client),
            patch.object(checks, "poll_job", AsyncMock(return_value=poll)),
            patch.object(checks.asyncio, "sleep", AsyncMock()),
        ):
            result = await checks.check_brand(
                brands=["Acme", "Example"], sample_pct=20, sample_size=5,
            )

        self.assertEqual(client.post.await_args.args[0], "/api/eligibility/check-brand/start")
        self.assertEqual(client.post.await_args.args[1], {
            "brands": ["Acme", "Example"], "sample_size": 5, "sample_pct": 20,
        })
        self.assertEqual(result["items"][0]["job_id"], "brand-job-1")

    async def test_rejects_sample_percentage_outside_range(self) -> None:
        with self.assertRaisesRegex(AppClientError, "sample_pct"):
            await checks.check_brand(brands=["Acme"], sample_pct=101)


class JobStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_saves_finished_asin_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = type("FakeClient", (), {
                "get": AsyncMock(return_value={
                    "job_id": "abc-123",
                    "status": "complete",
                    "done": 1,
                    "total": 1,
                    "results": [{"asin": "B000000001", "status": "CAN_SELL"}],
                }),
                "download": AsyncMock(return_value=b"asin,status\r\nB000000001,CAN_SELL\r\n"),
            })()
            with (
                patch("mcp_server.jobs.app_client", client),
                patch("mcp_server.jobs._download_directory", return_value=Path(temp_dir)),
            ):
                result = await poll_job("abc-123", "eligibility")

            self.assertEqual(Path(result["file"]).read_bytes(), b"asin,status\r\nB000000001,CAN_SELL\r\n")
            self.assertEqual(result["counts"], {"CAN_SELL": 1})
            self.assertEqual(client.download.await_count, 1)

    async def test_rejects_invalid_job_id(self) -> None:
        with self.assertRaisesRegex(AppClientError, "invalid characters"):
            await poll_job("../outside", "eligibility")
