"""An in-memory Sixto gateway (spec section 7), served through ``httpx.MockTransport``.

Only the module API the watchdog uses: ``GET /updates``, ``POST /notify`` and
``POST /react`` and ``GET /notify/{ref}``, all behind a bearer secret. ``/notify`` is idempotent on
``idempotency_key`` like the real gateway.
"""

from __future__ import annotations

import json

import httpx

SECRET = "w" * 32

# Telegram's message id for a queued ref is deliberately a different number (ref 5530
# becomes message 466), so a ref stored as an ID shows up as a wrong backlink (#63).
REF_TO_TELEGRAM_OFFSET = 5064


def telegram_id_for(ref: int) -> int:
    return ref - REF_TO_TELEGRAM_OFFSET


class FakeGateway:
    def __init__(self, secret: str = SECRET):
        self.secret = secret
        self.items: list[dict] = []
        self.notifications: list[dict] = []  # queued once per idempotency key
        self.reactions: list[dict] = []
        self.requests: list[httpx.Request] = []
        self.fail_next = 0  # answer 503 to this many requests
        self.notify_state = "sent"  # what GET /notify/{ref} reports unless overridden
        self.notify_states: dict[int, str] = {}  # per-ref override: queued | sent
        self._refs: dict[str, int] = {}
        self._issued: set[int] = set()
        self._next_ref = 5530

    def post(self, chat: int, topic: int | None, message_id: int, text: str, **extra) -> dict:
        """Deliver one ``update`` item to the watchdog's queue; ``seq`` is global."""
        seq = len(self.items) + 1
        edited = extra.pop("edited", False)
        edit_date = extra.pop("edit_date", None)
        body = {"chat": {"id": chat}, "date": 1790564400, "text": text}
        key = "edited_message" if edited else "message"
        item = {
            "seq": seq,
            "kind": "update",
            "update_id": 912345000 + seq,
            "chat": chat,
            "topic": topic,
            "message_id": message_id,
            "from": {"id": 111, "username": "anareyes", "contact_id": None},
            "text": text,
            "edited": edited,
            "edit_date": edit_date,
            "raw": {"update_id": 912345000 + seq, key: body},
        }
        self.items.append(item)
        return item

    def set_notify_state(self, ref: int, state: str) -> None:
        self.notify_states[ref] = state

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.headers.get("authorization") != f"Bearer {self.secret}":
            return httpx.Response(401, json={"detail": "unauthorized"})
        if self.fail_next:
            self.fail_next -= 1
            return httpx.Response(503, json={"detail": "busy"})
        path = request.url.path
        if request.method == "GET" and path == "/updates":
            after = int(request.url.params["after"])
            items = [i for i in self.items if i["seq"] > after][:100]
            return httpx.Response(200, json={"items": items})
        if request.method == "GET" and path.startswith("/notify/"):
            ref = int(path.rsplit("/", 1)[1])
            if ref not in self._issued:
                return httpx.Response(404, json={"detail": "unknown message_ref"})
            state = self.notify_states.get(ref, self.notify_state)
            sent = state == "sent"
            return httpx.Response(
                200,
                json={
                    "state": state,
                    "telegram_message_id": telegram_id_for(ref) if sent else None,
                    "error": None,
                },
            )
        body = json.loads(request.content or b"{}")
        if request.method == "POST" and path == "/notify":
            key = body.get("idempotency_key")
            if key is not None and key in self._refs:
                return httpx.Response(202, json={"message_ref": self._refs[key]})
            ref = self._next_ref
            self._next_ref += 1
            if key is not None:
                self._refs[key] = ref
            self._issued.add(ref)
            self.notifications.append(body)
            return httpx.Response(202, json={"message_ref": ref})
        if request.method == "POST" and path == "/react":
            self.reactions.append(body)
            return httpx.Response(202, json={"message_ref": 1})
        return httpx.Response(404)
