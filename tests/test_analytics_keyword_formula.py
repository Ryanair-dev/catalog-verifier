from __future__ import annotations

import asyncio
import io
import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services.analytics import brand_extractor, runner
from services.analytics.keyword_formula import KEYWORD_FORMULA
from services.analytics.matching_core.adapter import build_pair
from services.analytics.matching_core.agents.query_agent import QueryAgent, _new_stats
from services.analytics.matching_core.agents.query_prompts import BUILD_QUERY_SYSTEM
from services.analytics.matching_core.models import Offer
from services.analytics.parser import SourceRow
from routers import analytics as analytics_router


KEYWORD_ANSWER = {
    "items": [{
        "id": "0",
        "search_keyword": "Acme Wound Dressing",
        "keyword_specs": "Acme Wound Dressing 4 in x 4 in, 10 Count",
    }],
}


class KeywordFormulaTests(unittest.TestCase):
    def test_formula_covers_requested_outputs_and_normalization_rules(self):
        self.assertIn("search_keyword", KEYWORD_FORMULA)
        self.assertIn("keyword_specs", KEYWORD_FORMULA)
        self.assertIn("10/Box, 40/Case", KEYWORD_FORMULA)
        self.assertIn("manufacturer and MPN", KEYWORD_FORMULA)
        self.assertIn("OpSite Flexigrid", KEYWORD_FORMULA)

    def test_anthropic_is_primary_and_gpt_is_fallback(self):
        with patch.dict(os.environ, {
            "ANTHROPIC_API_KEY": "anthropic-test",
            "OPENAI_API_KEY": "openai-test",
        }, clear=True):
            provider, model, _ = brand_extractor._selected_provider()
        self.assertEqual((provider, model), ("anthropic", brand_extractor.ANTHROPIC_MODEL))

        with patch.dict(os.environ, {"OPENAI_API_KEY": "openai-test"}, clear=True):
            provider, model, _ = brand_extractor._selected_provider()
        self.assertEqual((provider, model), ("openai", brand_extractor.OPENAI_MODEL))

    def test_anthropic_call_uses_compatible_messages_signature(self):
        response = SimpleNamespace(
            content=[SimpleNamespace(type="text", text=json.dumps(KEYWORD_ANSWER))],
            usage=SimpleNamespace(input_tokens=12, output_tokens=8),
        )

        class FakeMessages:
            system = ""

            def create(self, *, model, max_tokens, system, messages):
                self.system = system
                self.messages = messages
                return response

        messages = FakeMessages()
        answers, tokens_in, tokens_out = brand_extractor._call(
            SimpleNamespace(messages=messages),
            [{"kind": "vendor", "text": "DRESSING, WOUND", "manufacturer": "Acme", "mpn": "X1"}],
            provider="anthropic",
            model=brand_extractor.ANTHROPIC_MODEL,
        )
        self.assertEqual(answers["0"]["search_keyword"], "Acme Wound Dressing")
        self.assertEqual((tokens_in, tokens_out), (12, 8))
        self.assertIn("10/Box, 40/Case", messages.system)
        self.assertIn('"manufacturer": "Acme"', messages.messages[0]["content"])

    def test_openai_call_still_uses_supported_completion_arguments(self):
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(KEYWORD_ANSWER)))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=7),
        )

        class FakeCompletions:
            def create(self, *, model, temperature, response_format, messages):
                return response

        client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
        answers, tokens_in, tokens_out = brand_extractor._call(
            client,
            [{"kind": "vendor", "text": "DRESSING, WOUND"}],
        )
        self.assertEqual(answers["0"]["keyword_specs"], KEYWORD_ANSWER["items"][0]["keyword_specs"])
        self.assertEqual((tokens_in, tokens_out), (10, 7))

    def test_query_agent_maps_keyword_fields_without_legacy_aliases(self):
        response = SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps({
                "search_keyword": "Acme Wound Dressing",
                "keyword_specs": "Acme Wound Dressing 4 in, 10 Count",
            }))],
            usage=SimpleNamespace(input_tokens=10, output_tokens=8),
        )

        class FakeMessages:
            def create(self, *, model, max_tokens, system, messages):
                return response

        agent = QueryAgent(SimpleNamespace(messages=FakeMessages()), api=None)
        stats = _new_stats()
        query, comparison_title = agent._build_query(
            Offer(offer_id="1", title="DRESSING, WOUND, ACME"),
            stats,
        )
        self.assertEqual(query, "Acme Wound Dressing")
        self.assertEqual(comparison_title, "Acme Wound Dressing 4 in, 10 Count")
        self.assertEqual(stats["search_keyword"], query)
        self.assertEqual(stats["keyword_specs"], comparison_title)

    def test_cache_lookup_requires_current_prompt_version(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE ai_field_cache (key TEXT PRIMARY KEY, result_json TEXT, prompt_version TEXT)"
        )
        conn.executemany(
            "INSERT INTO ai_field_cache(key, result_json, prompt_version) VALUES (?, ?, ?)",
            [
                ("current", '{"search_keyword":"Acme Cream"}', "fields-keywords-v3"),
                ("old", '{"search_keyword":"Old Result"}', "fields-v2"),
            ],
        )
        self.assertEqual(
            brand_extractor._lookup(conn, ["current", "old"], "fields-keywords-v3"),
            {"current": {"search_keyword": "Acme Cream"}},
        )
        conn.close()

    def test_untitled_row_uses_manufacturer_and_mpn_cache_key(self):
        row = SourceRow(
            row_idx=4,
            itemid="MPN-42",
            title="Untitled product",
            brand="Acme",
        )
        observed = {}

        def fake_extract(run_id, kind, entries, progress_cb=None):
            observed["kind"] = kind
            observed["entry"] = entries[0]
            return {
                entries[0]["key"]: {
                    "search_keyword": "Acme MPN-42",
                    "keyword_specs": "Acme MPN-42",
                }
            }

        with patch("services.analytics.brand_extractor._extract", side_effect=fake_extract):
            result = brand_extractor.extract_brands([row])
        self.assertEqual(observed["entry"]["key"], "vendor:mpn:acme|mpn-42")
        self.assertEqual(observed["entry"]["text"], "Untitled product")
        self.assertEqual(observed["entry"]["manufacturer"], "Acme")
        self.assertEqual(result[4]["search_keyword"], "Acme MPN-42")

    def test_tier_three_uses_generated_keyword(self):
        class FakeCatalog:
            terms = []

            def search_by_keywords(self, term, *, page_token=None, page_size=20):
                self.terms.append(term)
                return {"items": [], "pagination": {}}

        api = FakeCatalog()
        runner._tier3_title(
            api,
            [SourceRow(row_idx=2, title="DRESSING, WOUND, DURAMAX")],
            max_pages=1,
            extracted_brands={2: {"search_keyword": "Duramax Wound Dressing"}},
        )
        self.assertEqual(api.terms, ["Duramax Wound Dressing"])

    def test_keyword_specs_are_used_for_scoring_but_raw_title_is_retained(self):
        offer, _ = build_pair(
            {"row_idx": 1, "title": "DRESSING, WOUND, DURAMAX"},
            {"asin": "B000000001", "title": "Duramax Wound Dressing 4 in"},
            extracted={
                "search_keyword": "Duramax Wound Dressing",
                "keyword_specs": "Duramax Wound Dressing 4 in x 4 in, 10 Count",
            },
        )
        self.assertEqual(offer.title, "Duramax Wound Dressing 4 in x 4 in, 10 Count")
        self.assertEqual(offer.raw_text, "DRESSING, WOUND, DURAMAX")

    def test_query_agent_uses_the_same_formula(self):
        self.assertIn(KEYWORD_FORMULA, BUILD_QUERY_SYSTEM)
        self.assertIn("search_keyword must also be returned as search_query", BUILD_QUERY_SYSTEM)
        self.assertIn("keyword_specs must also be returned as comparison_title", BUILD_QUERY_SYSTEM)

    def test_export_includes_both_keywords_for_candidates_and_not_found(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript("""
            CREATE TABLE analytics_runs (
                id INTEGER PRIMARY KEY, name TEXT, ai_clean_titles INTEGER,
                clean_amazon_titles INTEGER, max_rank INTEGER, min_rank INTEGER,
                passthrough_cols TEXT
            );
            CREATE TABLE analytics_catalog_rows (
                run_id INTEGER, row_idx INTEGER, data_json TEXT, extracted_json TEXT
            );
            CREATE TABLE analytics_candidates (
                run_id INTEGER, row_idx INTEGER, asin TEXT, confidence REAL, verdict TEXT,
                sales_rank INTEGER, amz_pack INTEGER, eligibility_status TEXT,
                storage_fee REAL, storage_fee_peak REAL, data_json TEXT
            );
        """)
        conn.execute(
            "INSERT INTO analytics_runs VALUES (9, 'Keyword test', 1, 0, 0, 0, '[]')"
        )
        conn.executemany(
            "INSERT INTO analytics_catalog_rows VALUES (9, ?, ?, ?)",
            [
                (0, json.dumps({
                    "upc": "123456789012", "itemid": "A1",
                    "title": "Noisy vendor title", "brand": "Acme", "raw": {},
                }), json.dumps({
                    "search_keyword": "Acme Wound Dressing",
                    "keyword_specs": "Acme Wound Dressing 4 in, 10 Count",
                })),
                (1, json.dumps({
                    "upc": "", "itemid": "A2",
                    "title": "Untitled product", "brand": "Acme", "raw": {},
                }), json.dumps({
                    "search_keyword": "Acme A2",
                    "keyword_specs": "Acme A2",
                })),
            ],
        )
        conn.execute(
            "INSERT INTO analytics_candidates VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                9, 0, "B000000001", 70, "review", None, None, None, None, None,
                json.dumps({
                    "amazon": {"title": "Acme Wound Dressing", "brand": "Acme"},
                    "scores": {},
                }),
            ),
        )

        async def response_bytes(response):
            return b"".join([chunk async for chunk in response.body_iterator])

        with (
            patch("routers.analytics.database._connect", return_value=conn),
            patch("routers.analytics.export_family_columns", return_value={}),
            patch("routers.analytics.FAMILY_EXPORT_COLUMNS", []),
            patch("routers.analytics.FAMILY_EXPORT_BLANK", []),
        ):
            response = analytics_router.export_analytics_run(9)
            workbook = load_workbook(
                io.BytesIO(asyncio.run(response_bytes(response))),
                read_only=True,
                data_only=True,
            )

        review_rows = workbook["Review"].iter_rows(values_only=True)
        review_header = next(review_rows)
        review_data = next(review_rows)
        self.assertIn("Search Keyword", review_header)
        self.assertIn("Search Keyword + Spec", review_header)
        keyword_col = review_header.index("Search Keyword")
        keyword_spec_col = review_header.index("Search Keyword + Spec")
        self.assertEqual(review_data[keyword_col], "Acme Wound Dressing")
        self.assertEqual(
            review_data[keyword_spec_col],
            "Acme Wound Dressing 4 in, 10 Count",
        )
        not_found_rows = workbook["Not Found"].iter_rows(values_only=True)
        not_found_header = next(not_found_rows)
        not_found_data = next(not_found_rows)
        self.assertEqual(not_found_data[not_found_header.index("Search Keyword")], "Acme A2")
        conn.close()


if __name__ == "__main__":
    unittest.main()
