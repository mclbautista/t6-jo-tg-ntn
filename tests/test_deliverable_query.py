"""Live Deliverable candidate query: relation filter, pagination, no other filters (#26)."""

import json
import unittest

import httpx

from jo_watchdog.adapters.t6_studio import T6Studio


def row(page_id: str, *parts: str) -> dict:
    return {
        "id": page_id,
        "properties": {"Name": {"type": "title", "title": [{"plain_text": p} for p in parts]}},
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

        adapter = T6Studio("t", transport=httpx.MockTransport(handler))
        candidates = await adapter.query_project_deliverables("ds-deliv", "proj-1")
        await adapter.close()

        self.assertEqual(
            [(c.page_id, c.name) for c in candidates],
            [("d1", "Moonglow FTR SCR"), ("d2", "Moonglow Archive")],
        )
        self.assertEqual(len(bodies), 2)
        for body in bodies:
            self.assertEqual(set(body) - {"start_cursor"}, {"page_size", "filter"})
            self.assertEqual(body["filter"]["relation"], {"contains": "proj-1"})
        self.assertNotIn("start_cursor", bodies[0])
        self.assertEqual(bodies[1]["start_cursor"], "c2")


if __name__ == "__main__":
    unittest.main()
