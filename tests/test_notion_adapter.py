"""The smoke tools' Notion adapter: pinned version and request shapes over t6-core's client."""

import json
import unittest

import httpx

from jo_watchdog.adapters.notion import NOTION_VERSION, NotionAdapter, QueryRequest


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_sends_pinned_version_and_returns_a_ref(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["version"] = request.headers["Notion-Version"]
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "pg1", "url": "https://n/pg1"})

        adapter = NotionAdapter("t", transport=httpx.MockTransport(handler))
        ref = await adapter.create_page({"parent": {"data_source_id": "ds"}, "properties": {}})
        await adapter.close()
        self.assertEqual((ref.id, seen["version"]), ("pg1", NOTION_VERSION))

    async def test_query_returns_at_most_page_size_rows(self):
        def handler(request: httpx.Request) -> httpx.Response:
            rows = [{"id": f"r{i}"} for i in range(5)]
            return httpx.Response(200, json={"results": rows, "has_more": False})

        adapter = NotionAdapter("t", transport=httpx.MockTransport(handler))
        page = await adapter.query_data_source("ds", QueryRequest(page_size=1))
        await adapter.close()
        self.assertEqual([r["id"] for r in page.results], ["r0"])

    def test_pinned_version_is_2026_03_11(self):
        self.assertEqual(NOTION_VERSION, "2026-03-11")


if __name__ == "__main__":
    unittest.main()
