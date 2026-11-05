from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import httpx

from mcp_server.client import AppClient, AppClientError
from mcp_server.tools import asin_research


class FindAsinsTests(unittest.IsolatedAsyncioTestCase):
    async def test_returns_small_shaped_result_and_counts_all_candidates(self) -> None:
        candidates = [
            {
                "asin": f"B00000000{index}",
                "title": f"Product {index}",
                "brand": "Acme",
                "confidence": 95.0 - index,
                "verdict": "verified" if index < 8 else "review",
                "sales_rank": index + 1,
                "list_price": 12.5,
                "storage_fee_offpeak": 0.4,
                "storage_fee_peak": 0.8,
                "_raw": {"secret": "must not be returned"},
            }
            for index in range(12)
        ]
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(200, json={"candidates": candidates, "total": 12})

        client = AppClient(transport=httpx.MockTransport(handler))
        with patch.object(asin_research, "app_client", client):
            result = await asin_research.find_asins(upc="012345678905", scoring_method="cascade")

        self.assertEqual(len(result["items"]), 10)
        self.assertEqual(result["counts"], {"verified": 8, "review": 4, "not_approved": 0})
        self.assertEqual(result["items"][0]["amazon_url"], "https://www.amazon.com/dp/B000000000")
        self.assertEqual(result["items"][0]["storage_fee"], 0.4)
        self.assertNotIn("_raw", result["items"][0])
        self.assertIn("12 candidates found", result["summary"])
        self.assertEqual(requests[0].url.path, "/api/analytics/quick-search")
        self.assertEqual(
            json.loads(await requests[0].aread()),
            {
                "upc": "012345678905",
                "itemid": "",
                "title": "",
                "brand": "",
                "scoring_method": "cascade",
                "vetting_mode": "cpg",
            },
        )

    async def test_empty_result(self) -> None:
        client = AppClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json={"candidates": [], "total": 0})
            )
        )
        with patch.object(asin_research, "app_client", client):
            result = await asin_research.find_asins(title="No matching product")

        self.assertEqual(result["items"], [])
        self.assertEqual(result["counts"]["verified"], 0)
        self.assertEqual(result["summary"], "No candidates found for title No matching product.")

    async def test_missing_search_identifier_is_rejected(self) -> None:
        with self.assertRaisesRegex(AppClientError, "Provide at least one"):
            await asin_research.find_asins(brand="Acme")

    async def test_app_not_running_is_clear(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        client = AppClient(base_url="http://localhost:8123", transport=httpx.MockTransport(handler))
        with patch.object(asin_research, "app_client", client):
            with self.assertRaisesRegex(AppClientError, "app is not running at http://localhost:8123"):
                await asin_research.find_asins(upc="012345678905")

    async def test_404_is_clear(self) -> None:
        client = AppClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(404))
        )
        with patch.object(asin_research, "app_client", client):
            with self.assertRaisesRegex(AppClientError, "could not find the requested resource"):
                await asin_research.find_asins(upc="012345678905")


if __name__ == "__main__":
    unittest.main()
