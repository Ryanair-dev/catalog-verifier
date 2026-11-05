from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import AsyncMock, patch

from mcp_server.client import AppClientError
from mcp_server import history_db
from mcp_server.tools import asin_research


def _create_history_db(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript("""
            CREATE TABLE analytics_runs (
                id INTEGER PRIMARY KEY,
                name TEXT,
                status TEXT,
                created_at TEXT
            );
            CREATE TABLE analytics_catalog_rows (
                run_id INTEGER,
                row_idx INTEGER,
                data_json TEXT
            );
            CREATE TABLE analytics_candidates (
                run_id INTEGER,
                row_idx INTEGER,
                asin TEXT,
                sources TEXT,
                confidence REAL,
                verdict TEXT,
                review_status TEXT,
                data_json TEXT
            );
        """)
        connection.execute(
            "INSERT INTO analytics_runs VALUES (?, ?, ?, ?)",
            (7, "Saved test run", "Complete", "2026-11-05"),
        )
        connection.execute(
            "INSERT INTO analytics_catalog_rows VALUES (?, ?, ?)",
            (7, 0, json.dumps({
                "upc": "012345678905",
                "itemid": "ABC-123",
                "title": "Sample product",
                "brand": "Sample Brand",
                "raw": {"Private Header": "not returned"},
            })),
        )
        connection.execute(
            "INSERT INTO analytics_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (7, 0, "B000000001", '["UPC"]', 92.5, "verified", "",
             json.dumps({
                 "scores": {
                     "explanation": {
                         "summary": "Saved score explanation",
                         "factors": ["UPC match: yes"],
                     },
                     "reasons": ["upc_match"],
                 },
                 "amazon": {
                     "asin": "B000000001",
                     "title": "Sample Amazon listing",
                     "brand": "Sample Brand",
                     "sales_rank": 1234,
                     "private_field": "not returned",
                 },
             })),
        )


class LookupProductTests(unittest.IsolatedAsyncioTestCase):
    async def test_lookup_searches_pairs_blacklist_and_saved_runs_read_only(self) -> None:
        client = type("FakeClient", (), {
            "post": AsyncMock(side_effect=[
                {"results": [{"asin": "B000000001"}]},
                {"results": [{"asin": "B000000002"}]},
            ])
        })()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.db"
            _create_history_db(path)
            with (
                patch.object(asin_research, "app_client", client),
                patch.object(history_db, "_database_path", return_value=path),
            ):
                before = path.read_bytes()
                result = await asin_research.lookup_product(upc="012345678905")
                after = path.read_bytes()

        self.assertEqual(after, before)
        self.assertEqual(result["identifier"], "012345678905")
        self.assertEqual(result["pair_library"], [{"asin": "B000000001"}])
        self.assertEqual(result["blacklist"], [{"asin": "B000000002"}])
        saved = result["saved_offer_analysis_matches"]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["candidate"]["asin"], "B000000001")
        self.assertEqual(saved[0]["candidate"]["verdict"], "verified")
        self.assertEqual(saved[0]["candidate"]["scores"]["explanation"]["summary"],
                         "Saved score explanation")
        self.assertNotIn("raw", saved[0]["catalog_item"])
        self.assertNotIn("private_field", saved[0]["candidate"]["amazon"])
        self.assertEqual(
            [call.args[0] for call in client.post.await_args_list],
            ["/api/pairs/library/search", "/api/pairs/search"],
        )

    async def test_lookup_can_find_saved_runs_by_item_id(self) -> None:
        client = type("FakeClient", (), {
            "post": AsyncMock(return_value={"results": []}),
        })()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.db"
            _create_history_db(path)
            with (
                patch.object(asin_research, "app_client", client),
                patch.object(history_db, "_database_path", return_value=path),
            ):
                result = await asin_research.lookup_product(item_id="abc-123")
        self.assertEqual(len(result["saved_offer_analysis_matches"]), 1)

    async def test_lookup_can_find_saved_runs_by_asin(self) -> None:
        client = type("FakeClient", (), {
            "post": AsyncMock(return_value={"results": []}),
        })()
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "history.db"
            _create_history_db(path)
            with (
                patch.object(asin_research, "app_client", client),
                patch.object(history_db, "_database_path", return_value=path),
            ):
                result = await asin_research.lookup_product(asin="b000000001")
        self.assertEqual(len(result["saved_offer_analysis_matches"]), 1)

    async def test_lookup_reports_missing_database(self) -> None:
        client = type("FakeClient", (), {
            "post": AsyncMock(return_value={"results": []}),
        })()
        with (
            patch.object(asin_research, "app_client", client),
            patch.object(
                history_db, "_database_path",
                return_value=Path("missing-catalog-verifier-test.db"),
            ),
        ):
            with self.assertRaisesRegex(AppClientError, "database not found"):
                await asin_research.lookup_product(upc="012345678905")

    async def test_requires_exactly_one_identifier(self) -> None:
        with self.assertRaisesRegex(AppClientError, "exactly one"):
            await asin_research.lookup_product()
        with self.assertRaisesRegex(AppClientError, "exactly one"):
            await asin_research.lookup_product(upc="012345678905", asin="B000000001")
        with self.assertRaisesRegex(AppClientError, "exactly one"):
            await asin_research.lookup_product(upc="012345678905", item_id="ABC-123")


class MpnLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_search_returns_preview_without_calling_paid_endpoint(self) -> None:
        client = type("FakeClient", (), {
            "post": AsyncMock(return_value={
                "results": [{
                    "mpn": "ABC-1", "manufacturer": "Acme", "matched": False,
                    "product_id": None, "qty_case": None, "ea_case": None,
                }]
            })
        })()
        with patch.object(asin_research, "app_client", client):
            result = await asin_research.mpn_lookup(
                mpn="ABC-1", manufacturer="Acme", use_ai_fallback=True,
            )

        self.assertEqual(client.post.await_count, 1)
        preview = result["items"][0]["ai_fallback_preview"]
        self.assertFalse(preview["will_run"])
        self.assertIn("charges vary", preview["estimated_cost"])

    async def test_confirmed_ai_search_runs_only_when_standard_lookup_unresolved(self) -> None:
        async def post(path: str, payload: dict, **kwargs) -> dict:
            if path == "/api/mpn-check":
                return {"results": [{
                    "mpn": "ABC-1", "manufacturer": "Acme", "matched": False,
                    "product_id": None, "qty_case": None, "ea_case": None,
                }]}
            self.assertEqual(path, "/api/mpn-check/ai-fallback")
            self.assertEqual(kwargs["timeout"], 90)
            return {"results": [{
                "mpn": "ABC-1", "manufacturer": "Acme", "ea_case": 24,
                "confidence": "high", "note": "AI (high confidence): spec sheet",
            }]}

        client = type("FakeClient", (), {"post": AsyncMock(side_effect=post)})()
        with patch.object(asin_research, "app_client", client):
            result = await asin_research.mpn_lookup(
                mpn="ABC-1", manufacturer="Acme", use_ai_fallback=True, confirm=True,
            )

        self.assertEqual(client.post.await_count, 2)
        self.assertEqual(result["items"][0]["ai_ea_case"], 24)
        self.assertEqual(result["items"][0]["ai_confidence"], "high")

    async def test_does_not_run_ai_when_standard_lookup_has_answer(self) -> None:
        client = type("FakeClient", (), {
            "post": AsyncMock(return_value={
                "results": [{
                    "mpn": "ABC-1", "manufacturer": "Acme", "matched": True,
                    "qty_case": 6, "ea_case": 24,
                }]
            })
        })()
        with patch.object(asin_research, "app_client", client):
            result = await asin_research.mpn_lookup(
                mpn="ABC-1", manufacturer="Acme", use_ai_fallback=True, confirm=True,
            )

        self.assertEqual(client.post.await_count, 1)
        self.assertIn("already returned", result["items"][0]["ai_fallback_preview"]["reason"])
