from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from mcp_server.tools import pair_management


class PairManagementTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_returns_library_and_blacklist(self) -> None:
        client = type("FakeClient", (), {
            "post": AsyncMock(side_effect=[
                {"results": [{"asin": "B000000001"}]},
                {"results": [{"asin": "B000000002"}]},
            ])
        })()
        with patch.object(pair_management, "app_client", client):
            result = await pair_management.search_pairs("123456789012")
        self.assertEqual(len(result["pair_library"]), 1)
        self.assertEqual(len(result["blacklist"]), 1)


if __name__ == "__main__":
    unittest.main()
