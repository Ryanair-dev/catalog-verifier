from __future__ import annotations

import base64
from io import BytesIO
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from openpyxl import Workbook

from mcp_server.client import AppClientError
from mcp_server.jobs import poll_job
from mcp_server.tools import brand_analytics, create_skus, po_analytics, seller_analytics, vendor_offers


class BrandAnalyticsToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_brand_runs_returns_a_bounded_summary(self) -> None:
        client = type("FakeClient", (), {
            "get": AsyncMock(return_value={
                "runs": [{"id": 31, "name": "Acme", "status": "Complete"}],
            }),
        })()
        with patch.object(brand_analytics, "app_client", client):
            result = await brand_analytics.brand_runs(limit=1)
        self.assertEqual(len(result["runs"]), 1)
        self.assertEqual(result["runs"][0]["id"], 31)
        self.assertEqual(result["runs"][0]["name"], "Acme")
        self.assertEqual(result["runs"][0]["status"], "Complete")
        client.get.assert_awaited_once_with("/api/brand-analytics/runs")

    async def test_brand_run_reads_requested_run_and_page(self) -> None:
        response = {"run": {"id": 31, "name": "Acme"}, "items": [], "total_items": 0}
        client = type("FakeClient", (), {"get": AsyncMock(return_value=response)})()
        with patch.object(brand_analytics, "app_client", client):
            result = await brand_analytics.brand_run(31, limit=25, offset=10)
        self.assertEqual(result, response)
        client.get.assert_awaited_once_with(
            "/api/brand-analytics/runs/31",
            params={"limit": 25, "offset": 10},
        )

    async def test_start_brand_search_explains_cost_is_unavailable_without_discovery(self) -> None:
        client = type("FakeClient", (), {
            "post": AsyncMock(),
        })()
        with patch.object(brand_analytics, "app_client", client):
            result = await brand_analytics.start_brand_search("Acme")
        self.assertTrue(result["confirmation_required"])
        self.assertIn("cannot estimate", result["message"])
        client.post.assert_not_awaited()

    async def test_start_brand_search_uses_existing_discovery_and_run_apis(self) -> None:
        client = type("FakeClient", (), {
            "post": AsyncMock(return_value={
                "result": {"sub_brands": ["Acme", "Acme Kids"]},
            }),
        })()
        with patch.object(brand_analytics, "app_client", client):
            discovery = await brand_analytics.start_brand_search("Acme", confirm=True)
            self.assertTrue(discovery["confirmation_required"])
            self.assertEqual(client.post.await_args.args[0], "/api/brand-analytics/discover")

            client.post.return_value = {"cached": False, "run_id": 31}
            result = await brand_analytics.start_brand_search(
                "Acme", confirm=True, search_terms=["Acme", "Acme Kids"],
            )
        self.assertEqual(result["started"]["run_id"], 31)
        self.assertEqual(client.post.await_args.args[0], "/api/brand-analytics/runs")
        self.assertEqual(
            client.post.await_args.args[1]["search_terms"],
            ["Acme", "Acme Kids"],
        )

    async def test_brand_run_caps_page_size(self) -> None:
        with self.assertRaisesRegex(AppClientError, "1 to 100"):
            await brand_analytics.brand_run(2, limit=500)


class SellerAndPoToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_seller_analysis_requires_confirmation(self) -> None:
        client = type("FakeClient", (), {
            "post": AsyncMock(side_effect=[
                {"seller_name": "Acme", "asin_count": 500},
                {"job_id": "job-1"},
            ])
        })()
        with patch.object(seller_analytics, "app_client", client):
            preview = await seller_analytics.seller_analysis("seller-1")
            self.assertTrue(preview["confirmation_required"])
            result = await seller_analytics.seller_analysis("seller-1", confirm=True)
        self.assertEqual(result["job_id"], "job-1")
        self.assertEqual(client.post.await_args_list[1].args[0], "/api/seller-analytics/start")

    async def test_analyze_po_requires_confirmation_and_starts_job(self) -> None:
        client = type("FakeClient", (), {
            "post": AsyncMock(return_value={"job_id": "po-1", "po_numbers": [51563, 51564]}),
        })()
        with patch.object(po_analytics, "app_client", client):
            preview = await po_analytics.analyze_po("51563")
            self.assertTrue(preview["confirmation_required"])
            self.assertEqual(preview["po_numbers"], "51563")
            client.post.assert_not_awaited()
            started = await po_analytics.analyze_po("51563, 51564", confirm=True)
        self.assertEqual(started["po_numbers"], [51563, 51564])
        self.assertEqual(client.post.await_args.args[1]["po_numbers"], "51563, 51564")

    async def test_po_watchlist_reads_existing_app_endpoint(self) -> None:
        response = {"watchlist": [{"po_number": 51563, "status": "waiting"}]}
        client = type("FakeClient", (), {"get": AsyncMock(return_value=response)})()
        with patch.object(po_analytics, "app_client", client):
            result = await po_analytics.po_watchlist()
        self.assertEqual(result, response)
        client.get.assert_awaited_once_with("/api/po-analytics/automation/watchlist")

    async def test_po_pnl_requests_each_app_supported_bucket_without_aggregating(self) -> None:
        client = type("FakeClient", (), {
            "get": AsyncMock(side_effect=[
                {"pos": [{"po_number": 1}]},
                {"pos": [{"po_number": 2}]},
            ])
        })()
        with patch.object(po_analytics, "app_client", client):
            result = await po_analytics.po_pnl(bucket="all")
        self.assertEqual(result["active"], {"pos": [{"po_number": 1}]})
        self.assertEqual(result["sold_out"], {"pos": [{"po_number": 2}]})
        self.assertEqual(
            [call.kwargs["params"]["bucket"] for call in client.get.await_args_list],
            ["active", "archive"],
        )


class CreateSkusToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_requires_confirmation_then_saves_generated_files(self) -> None:
        workbook = b"PK\x03\x04test"
        encoded = base64.b64encode(workbook).decode("ascii")
        options = {
            "col_map": {"UPC": "upc", "Title": "name"},
            "create_by": "UPC",
        }
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "catalog.csv"
            path.write_text("UPC,Title\n1,Widget\n", encoding="utf-8")
            client = type("FakeClient", (), {
                "post_file": AsyncMock(return_value={
                    "token": "a" * 32,
                    "headers": ["UPC", "Title"],
                    "filename": "catalog.csv",
                    "row_count": 1,
                    "rows": [{"UPC": "1", "Title": "Widget"}],
                    "active_sheet": "",
                }),
                "post": AsyncMock(side_effect=[
                    {"counts": {"total": 1}, "groups": []},
                    {"export_id": "ABC123", "export_date": "2026-11-05", "files": [
                        {"name": "Bulk_ABC123.xlsx", "b64": encoded},
                    ]},
                ]),
            })()
            with patch.object(create_skus, "app_client", client):
                preview = await create_skus.create_skus(str(path), options)
                self.assertTrue(preview["confirmation_required"])
                self.assertEqual(client.post.await_count, 0)
                result = await create_skus.create_skus(
                    options=options,
                    session_token=preview["session_token"],
                    confirm=True,
                    output_dir=temp_dir,
                )
            self.assertTrue(Path(result["files"][0]).exists())
            self.assertFalse(result["sellercloud_write_performed"])

    async def test_create_skus_rejects_bad_options(self) -> None:
        with self.assertRaisesRegex(AppClientError, "col_map"):
            await create_skus.create_skus(options={})


class VendorOffersToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_cheapest_offer_reads_the_app_generated_lookup_workbook(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Price Check"
        sheet.append(["Input", "Type", "Found?", "Cheapest Vendor", "Cheapest Cost"])
        sheet.append(["012345678905", "UPC", "found", "Vendor B", 4.0])
        buffer = BytesIO()
        workbook.save(buffer)
        client = type("FakeClient", (), {
            "post_download": AsyncMock(return_value=buffer.getvalue()),
        })()
        with patch.object(vendor_offers, "app_client", client):
            result = await vendor_offers.cheapest_offer(upc="012345678905")
        self.assertEqual(result["price_check"]["Cheapest Vendor"], "Vendor B")
        self.assertEqual(result["price_check"]["Cheapest Cost"], 4.0)
        self.assertEqual(
            client.post_download.await_args.args[0],
            "/api/vendor-offers/lookup-export",
        )

    async def test_export_offers_requires_confirmation_and_saves(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = type("FakeClient", (), {
                "post_download": AsyncMock(return_value=b"PK\x03\x04workbook"),
            })()
            with patch.object(vendor_offers, "app_client", client):
                preview = await vendor_offers.export_offers(output_dir=temp_dir)
                self.assertTrue(preview["confirmation_required"])
                client.post_download.assert_not_awaited()
                result = await vendor_offers.export_offers(
                    confirm=True, output_dir=temp_dir, upcs=["012345678905"],
                )
            self.assertTrue(Path(result["file_path"]).exists())


class LongJobStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_completed_po_job_saves_xlsx(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = type("FakeClient", (), {
                "get": AsyncMock(return_value={
                    "status": "complete", "done": 4, "total": 4, "filename": "po.xlsx",
                }),
                "download": AsyncMock(return_value=b"PK\x03\x04workbook"),
            })()
            with (
                patch("mcp_server.jobs.app_client", client),
                patch("mcp_server.jobs._download_directory", return_value=Path(temp_dir)),
            ):
                result = await poll_job("po-123", "po_analytics")
            self.assertTrue(result["file"].endswith(".xlsx"))
            self.assertEqual(Path(result["file"]).read_bytes(), b"PK\x03\x04workbook")


if __name__ == "__main__":
    unittest.main()
