from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx

from mcp_server.client import AppClient, AppClientError
from mcp_server import history_db
from mcp_server.tools import matching


_SETTINGS = {
    "header_row": 0,
    "mapping": {"upc": 0, "title": 1},
    "search_methods": ["UPC", "Title"],
}
_ESTIMATE = {
    "rows": 2,
    "stages": [],
    "total": {"low": 10, "mid": 20, "high": 30},
    "cost": {"mid": 0.01, "high": 0.02},
}


class MatchingValidationTests(unittest.TestCase):
    def test_builds_multipart_settings(self) -> None:
        fields = matching._settings_fields(_SETTINGS)
        self.assertEqual(fields["header_row"], "0")
        self.assertEqual(json.loads(fields["mapping"]), {"upc": "0", "title": "1"})
        self.assertEqual(json.loads(fields["search_methods"]), ["UPC", "Title"])
        self.assertEqual(fields["name"], "Catalog Verifier MCP run")

    def test_rejects_bad_mapping_or_search_methods(self) -> None:
        with self.assertRaisesRegex(AppClientError, "Map at least one"):
            matching._settings_fields({
                "header_row": 0, "mapping": {"brand": 0}, "search_methods": ["Title"],
            })
        with self.assertRaisesRegex(AppClientError, "Search methods"):
            matching._settings_fields({
                "header_row": 0, "mapping": {"title": 0}, "search_methods": ["Amazon"],
            })

    def test_requires_absolute_supported_file_path(self) -> None:
        with self.assertRaisesRegex(AppClientError, "absolute path"):
            matching._catalog_path("catalog.xlsx")
        with self.assertRaisesRegex(AppClientError, "CSV, TSV"):
            matching._catalog_path(str(Path.cwd() / "catalog.xls"))


class MatchingToolsTests(unittest.IsolatedAsyncioTestCase):
    async def test_list_runs_summarizes_safe_runs_endpoint(self) -> None:
        client = type("FakeClient", (), {
            "get": AsyncMock(return_value=[
                {"id": 8, "name": "new", "status": "Searching", "progress_done": 2, "progress_total": 4},
                {"id": 7, "name": "old", "status": "Complete"},
            ])
        })()
        with patch.object(matching, "app_client", client):
            result = await matching.list_runs(limit=1)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["runs"][0]["id"], 8)
        self.assertEqual(client.get.await_args.args, ("/api/analytics/runs",))

    async def test_run_status_calculates_progress_without_detail_request(self) -> None:
        client = type("FakeClient", (), {
            "get": AsyncMock(return_value=[
                {"id": 3, "status": "Searching", "progress_done": 1, "progress_total": 4},
            ])
        })()
        with patch.object(matching, "app_client", client):
            result = await matching.run_status(3)
        self.assertEqual(result["progress_percent"], 25)
        self.assertEqual(client.get.await_args.args, ("/api/analytics/runs",))

    async def test_explain_pair_reads_saved_score_without_calling_app_detail_endpoint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.db"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.executescript("""
                    CREATE TABLE analytics_runs (
                        id INTEGER PRIMARY KEY, name TEXT, status TEXT, created_at TEXT
                    );
                    CREATE TABLE analytics_catalog_rows (
                        run_id INTEGER, row_idx INTEGER, data_json TEXT
                    );
                    CREATE TABLE analytics_candidates (
                        run_id INTEGER, row_idx INTEGER, asin TEXT, sources TEXT,
                        confidence REAL, verdict TEXT, review_status TEXT, data_json TEXT
                    );
                """)
                connection.execute(
                    "INSERT INTO analytics_runs VALUES (5, 'Test', 'Complete', 'today')"
                )
                connection.execute(
                    "INSERT INTO analytics_catalog_rows VALUES (5, 2, ?)",
                    (json.dumps({"upc": "012345678905", "title": "Widget"}),),
                )
                connection.execute(
                    "INSERT INTO analytics_candidates VALUES (5, 2, ?, ?, ?, ?, ?, ?)",
                    (
                        "B000000001", '["UPC"]', 95, "verified", "",
                        json.dumps({
                            "scores": {
                                "explanation": {"summary": "Stored reason", "factors": ["UPC match"]},
                                "reasons": ["upc_match"],
                            },
                            "amazon": {"title": "Amazon Widget"},
                        }),
                    ),
                )
            with patch.object(history_db, "_database_path", return_value=path):
                before = path.read_bytes()
                result = await matching.explain_pair(5, 3, "B000000001")
                after = path.read_bytes()
        self.assertEqual(after, before)
        self.assertEqual(result["candidate"]["scores"]["explanation"]["summary"], "Stored reason")
        self.assertEqual(result["row"], 3)
        self.assertIn("no verdict was recalculated", result["note"])

    async def test_explain_pair_reports_missing_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.db"
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.executescript("""
                    CREATE TABLE analytics_runs (
                        id INTEGER PRIMARY KEY, name TEXT, status TEXT, created_at TEXT
                    );
                    CREATE TABLE analytics_catalog_rows (
                        run_id INTEGER, row_idx INTEGER, data_json TEXT
                    );
                    CREATE TABLE analytics_candidates (
                        run_id INTEGER, row_idx INTEGER, asin TEXT, sources TEXT,
                        confidence REAL, verdict TEXT, review_status TEXT, data_json TEXT
                    );
                    INSERT INTO analytics_runs VALUES (5, 'Test', 'Complete', 'today');
                """)
            with patch.object(history_db, "_database_path", return_value=path):
                with self.assertRaisesRegex(AppClientError, "No saved candidate"):
                    await matching.explain_pair(5, 1, "B000000001")

    async def test_estimate_uploads_file_and_returns_estimate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "catalog.csv"
            path.write_text("UPC,Title\n1,Widget\n", encoding="utf-8")
            client = type("FakeClient", (), {
                "post_file": AsyncMock(return_value=_ESTIMATE),
            })()
            with patch.object(matching, "app_client", client):
                result = await matching.estimate_run(str(path), _SETTINGS)
        self.assertEqual(result["rows"], 2)
        self.assertEqual(client.post_file.await_args.args[0], "/api/analytics/estimate")

    async def test_start_requires_confirmation_then_checks_config_and_starts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "catalog.csv"
            path.write_text("UPC,Title\n1,Widget\n", encoding="utf-8")
            client = type("FakeClient", (), {
                "get": AsyncMock(return_value={"sp_api_configured": True}),
                "post_file": AsyncMock(return_value={"run_id": 42, "status": "Searching"}),
            })()
            with (
                patch.object(matching, "app_client", client),
                patch.object(matching, "_estimate", AsyncMock(return_value=_ESTIMATE)),
            ):
                preview = await matching.start_run(str(path), _SETTINGS)
                self.assertTrue(preview["confirmation_required"])
                client.post_file.assert_not_awaited()

                started = await matching.start_run(str(path), _SETTINGS, confirm=True)

        self.assertEqual(started["run"]["run_id"], 42)
        self.assertEqual(client.get.await_args.args, ("/api/analytics/status",))
        self.assertEqual(client.post_file.await_args.args[0], "/api/analytics/runs")

    async def test_start_does_not_call_run_api_without_sp_api(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "catalog.csv"
            path.write_text("UPC,Title\n1,Widget\n", encoding="utf-8")
            client = type("FakeClient", (), {
                "get": AsyncMock(return_value={"sp_api_configured": False}),
                "post_file": AsyncMock(),
            })()
            with (
                patch.object(matching, "app_client", client),
                patch.object(matching, "_estimate", AsyncMock(return_value=_ESTIMATE)),
            ):
                with self.assertRaisesRegex(AppClientError, "SP-API is not configured"):
                    await matching.start_run(str(path), _SETTINGS, confirm=True)
            client.post_file.assert_not_awaited()

    async def test_export_requires_confirmation_and_saves_workbook(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            client = type("FakeClient", (), {
                "download": AsyncMock(return_value=b"PK\x03\x04workbook"),
            })()
            with patch.object(matching, "app_client", client):
                preview = await matching.export_run(17, output_dir=temp_dir)
                self.assertTrue(preview["confirmation_required"])
                client.download.assert_not_awaited()
                result = await matching.export_run(17, confirm=True, output_dir=temp_dir)
            output = Path(result["file_path"])
            self.assertEqual(output.read_bytes(), b"PK\x03\x04workbook")

    async def test_report_match_uses_app_reason_codes_and_one_based_row(self) -> None:
        client = type("FakeClient", (), {
            "get": AsyncMock(return_value={
                "different": [{"code": "brand"}], "same": [{"code": "wording"}],
            }),
            "post": AsyncMock(return_value={"ok": True, "id": 11}),
        })()
        with patch.object(matching, "app_client", client):
            preview = await matching.report_match(
                4, 3, "b000000001", "different", reasons=["brand"],
            )
            self.assertEqual(preview["report"]["row_idx"], 2)
            client.post.assert_not_awaited()
            result = await matching.report_match(
                4, 3, "b000000001", "different", reasons=["brand"], confirm=True,
            )
        self.assertTrue(result["submitted"])
        self.assertEqual(client.post.await_args.args[1]["row_idx"], 2)
        self.assertEqual(client.post.await_args.args[1]["asin"], "B000000001")

    async def test_report_match_rejects_invalid_reason_code(self) -> None:
        client = type("FakeClient", (), {
            "get": AsyncMock(return_value={"different": [], "same": []}),
            "post": AsyncMock(),
        })()
        with patch.object(matching, "app_client", client):
            with self.assertRaisesRegex(AppClientError, "Choose reason codes"):
                await matching.report_match(
                    4, 3, "B000000001", "different", reasons=["unknown"],
                )
        client.post.assert_not_awaited()


class MultipartClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_post_file_sends_expected_multipart_file_and_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "catalog.csv"
            path.write_bytes(b"UPC,Title\n1,Widget\n")
            seen: list[httpx.Request] = []

            def handler(request: httpx.Request) -> httpx.Response:
                seen.append(request)
                return httpx.Response(200, json={"rows": 1})

            client = AppClient(transport=httpx.MockTransport(handler))
            result = await client.post_file(
                "/api/analytics/estimate",
                path,
                {"header_row": "0"},
            )
        self.assertEqual(result, {"rows": 1})
        body = await seen[0].aread()
        self.assertIn(b'name="catalog_file"; filename="catalog.csv"', body)
        self.assertIn(b'name="header_row"', body)
        self.assertIn(b"UPC,Title", body)


if __name__ == "__main__":
    unittest.main()
