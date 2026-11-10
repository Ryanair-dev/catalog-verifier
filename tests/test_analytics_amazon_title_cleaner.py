import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from services.analytics.amazon_title_cleaner import _clean_batch
from services.analytics.matching_core.adapter import build_pair
from services.analytics.matching_core.matching.attributes import size_mismatch
from services.analytics.matching_core.matching.scorer import _strengths


class AmazonTitleCleanerTests(unittest.TestCase):
    def test_batch_parses_cleaned_titles_in_order(self):
        response = SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps({
                "results": [
                    {"cleaned_title": "Acme lotion 8 oz"},
                    {"cleaned_title": "Acme cream 4 oz"},
                ]
            }))]
        )

        class FakeMessages:
            def create(self, *, model, max_tokens, system, messages):
                return response

        client = SimpleNamespace(messages=FakeMessages())
        self.assertEqual(
            _clean_batch(client, ["noisy lotion title", "noisy cream title"]),
            ["Acme lotion 8 oz", "Acme cream 4 oz"],
        )

    def test_prompt_extracts_core_item_name_instead_of_rewriting_listing(self):
        response = SimpleNamespace(
            content=[SimpleNamespace(text=json.dumps({
                "results": [{"cleaned_title": "Acme Cleaner Spray, 32 oz"}]
            }))]
        )

        class FakeMessages:
            system = ""

            def create(self, *, model, max_tokens, system, messages):
                self.system = system
                return response

        messages = FakeMessages()
        self.assertEqual(
            _clean_batch(SimpleNamespace(messages=messages), [
                "Acme Ultra Strength Cleaner Spray, Powerful Stain Removal for Home and Office, 32 oz"
            ]),
            ["Acme Cleaner Spray, 32 oz"],
        )
        self.assertIn("This is extraction", messages.system)
        self.assertIn("Remove sales language", messages.system)
        self.assertIn('"Acme Citrus Cleaner Spray, 32 fl oz"', messages.system)

    def test_cleaned_title_only_changes_fuzzy_matching(self):
        source = {
            "row_idx": 1,
            "title": "Acme lotion 8 oz",
            "brand": "Acme",
        }
        amazon = {
            "asin": "B000000001",
            "title": "Acme Lotion 16 oz with excessive marketing copy",
            "matching_title": "Acme lotion 16 oz",
            "brand": "Acme",
        }
        offer, candidate = build_pair(source, amazon)

        self.assertEqual(candidate.title, amazon["title"])
        self.assertEqual(candidate.matching_title, amazon["matching_title"])
        self.assertTrue(size_mismatch(offer, candidate))

        with patch(
            "services.analytics.matching_core.matching.scorer.title_similarity",
            return_value=88,
        ) as fuzzy:
            _strengths(offer, candidate, upc_hit=False)
        fuzzy.assert_called_once_with(offer.title, amazon["matching_title"])


if __name__ == "__main__":
    unittest.main()
