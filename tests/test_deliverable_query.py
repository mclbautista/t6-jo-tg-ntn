"""Live Deliverable candidate query: relation filter, pagination, no other filters (#26)."""

import json
import unittest

import httpx

from jo_watchdog.adapters.notion import NotionAdapter


def row(page_id: str, *parts: str) -> dict:
    return {
        "id": page_id,
        "properties": {"Name": {"title": [{"plain_text": p} for p in parts]}},
    }


class DeliverableQueryTests(unittest.IsolatedAsyncioTestCase):
    async def test_follows_every_page_with_only_the_project_filter(self):
        bodies = []
        pages = [
            {"results": [row("d1", "Moonglow ", "FTR SCR")], "has_more": True, "next_cursor": "c2"},
            {"results": [row("d2", "Moonglow Archive")], "has_more": False, "next_cursor": None},
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(
                str(request.url),
                "https://api.notion.com/v1/data_sources/ds-deliv/query",
            )
            bodies.append(json.loads(request.content))
            return httpx.Response(200, json=pages[len(bodies) - 1])

        adapter = NotionAdapter(token="t", transport=httpx.MockTransport(handler))
        candidates = await adapter.query_project_deliverables("ds-deliv", "proj-1")
        await adapter.close()

        self.assertEqual(
            [(c.page_id, c.name) for c in candidates],
            [("d1", "Moonglow FTR SCR"), ("d2", "Moonglow Archive")],
        )
        expected_filter = {"property": "🗺️ Projects", "relation": {"contains": "proj-1"}}
        self.assertEqual(bodies[0], {"page_size": 100, "filter": expected_filter})
        self.assertEqual(
            bodies[1], {"page_size": 100, "start_cursor": "c2", "filter": expected_filter}
        )


if __name__ == "__main__":
    unittest.main()
