"""Notion adapter contract tests: pinned version, parent shape, pagination.

Runs offline via ``httpx.MockTransport`` — verifies the exact request shapes the
adapter sends, not a live account.
"""

import json
import unittest

import httpx

from jo_watchdog.adapters.notion import NOTION_VERSION, NotionAdapter, QueryRequest


def _adapter(handler) -> NotionAdapter:
    transport = httpx.MockTransport(handler)
    return NotionAdapter(token="test-token", transport=transport)


class CreatePageTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_sends_pinned_version_and_parent(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["version"] = request.headers["Notion-Version"]
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"id": "pg1", "url": "https://n/pg1"})

        adapter = _adapter(handler)
        ref = await adapter.create_page(
            {"parent": {"data_source_id": "ds-1"}, "properties": {}, "children": []}
        )
        await adapter._client.aclose()

        self.assertEqual(ref.id, "pg1")
        self.assertEqual(seen["url"], "https://api.notion.com/v1/pages")
        self.assertEqual(seen["version"], "2026-03-11")
        self.assertEqual(seen["body"]["parent"], {"data_source_id": "ds-1"})

    async def test_create_raises_notion_error_on_4xx(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                400, json={"code": "validation_error", "message": "bad"}
            )

        adapter = _adapter(handler)
        with self.assertRaises(Exception) as ctx:
            await adapter.create_page({"parent": {}, "properties": {}, "children": []})
        await adapter._client.aclose()
        self.assertEqual(ctx.exception.status, 400)
        self.assertEqual(ctx.exception.code, "validation_error")


class RetrievePageTests(unittest.IsolatedAsyncioTestCase):
    async def test_retrieve_hits_pages_endpoint(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"id": "pg1", "properties": {}})

        adapter = _adapter(handler)
        await adapter.retrieve_page("pg1")
        await adapter._client.aclose()
        self.assertEqual(seen["url"], "https://api.notion.com/v1/pages/pg1")


class QueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_query_is_pagination_ready(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200, json={"results": [], "has_more": True, "next_cursor": "c2"}
            )

        adapter = _adapter(handler)
        resp = await adapter.query_data_source(
            "ds-1", QueryRequest(page_size=100, start_cursor="c1")
        )
        await adapter._client.aclose()

        self.assertEqual(
            seen["url"], "https://api.notion.com/v1/data_sources/ds-1/query"
        )
        self.assertEqual(seen["body"]["start_cursor"], "c1")
        self.assertEqual(seen["body"]["page_size"], 100)
        self.assertTrue(resp.has_more)
        self.assertEqual(resp.next_cursor, "c2")


class PinnedVersionTests(unittest.TestCase):
    def test_pinned_version_is_2026_03_11(self):
        self.assertEqual(NOTION_VERSION, "2026-03-11")


if __name__ == "__main__":
    unittest.main()
