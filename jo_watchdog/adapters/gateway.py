"""Sixto gateway adapter: the ``TelegramClient`` protocol over the gateway's module API.

In ``gateway`` mode the watchdog holds a module secret instead of the Telegram
token. The gateway owns ``getUpdates`` and the send queue; this client reads the
watchdog's routed items from ``GET /updates``, queues ops posts with
``POST /notify`` and reactions with ``POST /react``.

The gateway's per-module ``seq`` is the watchdog's update id, so the ledger's
offset (``last + 1``) maps straight onto ``/updates?after=<last>``.
"""

from __future__ import annotations

import httpx

from jo_watchdog.adapters.protocols import IncomingUpdate

# Slack on top of the long-poll ``timeout`` so the HTTP read does not give up first.
_POLL_SLACK_SECONDS = 15.0
_REQUEST_TIMEOUT_SECONDS = 30.0


class GatewayTelegramClient:
    """Plain HTTP client for the gateway; the worker, ledger and core don't know it."""

    # The worker passes the outbox row's idempotency key only to clients that say so.
    accepts_idempotency_key = True

    def __init__(
        self,
        base_url: str,
        secret: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {secret}"},
            timeout=_REQUEST_TIMEOUT_SECONDS,
            transport=transport,
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def get_updates(self, offset: int, timeout: int) -> list[IncomingUpdate]:
        response = await self._http.get(
            "/updates",
            params={"after": max(offset - 1, 0), "timeout": timeout},
            timeout=timeout + _POLL_SLACK_SECONDS,
        )
        response.raise_for_status()
        result: list[IncomingUpdate] = []
        for item in response.json()["items"]:
            # Every kind is passed on, commands included: the cursor is the gateway seq, so
            # an item the worker never sees would be re-read forever. The worker's scope
            # filter ignores (and moves the offset past) anything that is not intake.
            result.append(
                IncomingUpdate(
                    update_id=item["seq"],
                    chat_id=item["chat"],
                    message_id=item["message_id"],
                    message_thread_id=item.get("topic"),
                    text=item.get("text") or "",
                    raw=item["raw"],
                    edited=bool(item.get("edited")),
                    edit_date=item.get("edit_date"),
                )
            )
        return result

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        response = await self._http.post(
            "/react", json={"chat": chat_id, "message_id": message_id, "emoji": emoji}
        )
        response.raise_for_status()

    async def send_message(
        self, chat_id: int, topic_id: int, text: str, *, idempotency_key: str | None = None
    ) -> int:
        body: dict = {"to": chat_id, "topic": topic_id, "text": text, "format": "html"}
        if idempotency_key is not None:
            body["idempotency_key"] = idempotency_key
        response = await self._http.post("/notify", json=body)
        response.raise_for_status()
        return response.json()["message_ref"]
