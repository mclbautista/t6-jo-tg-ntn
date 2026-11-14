"""Pure core: Notion page links."""

from __future__ import annotations


def page_url_for(page_id: str) -> str:
    """A working Notion URL for a page id."""
    return f"https://www.notion.so/{page_id.replace('-', '')}"
