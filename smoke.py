#!/usr/bin/env python3
"""Manual real-account smoke verification (issue #27). Never run in CI.

Checks the assumptions CI deliberately excludes: live Telegram, Notion, DeepSeek
and Jev behavior, the deployment's storage permissions, and the full capture
pipeline against real accounts. Each step prints PASS, FAIL, SKIP or MANUAL;
the script exits 1 when any step fails.

    python smoke.py --config config.yaml                 # everything except live Telegram
    python smoke.py --config config.yaml --live-telegram # also capture one real intake post
    python smoke.py --config config.yaml --only reactions,notion_access

Secrets come from the environment (``.env``): TELEGRAM_BOT_TOKEN,
NOTION_API_KEY, DEEPSEEK_API_KEY, OPENROUTER_API_KEY.

Side effects, by step:

- ``pipeline`` creates one clearly marked Job Order (the integration cannot
  archive it; the URL is printed so you can delete it by hand).
- ``unknown_select`` (only with ``--probe-unknown-select``) creates a second
  marked Job Order with an unknown Department option, which Notion may add to
  the schema; delete the option by hand if it does.
- ``live_telegram`` (only with ``--live-telegram``) waits for one real post in
  the intake topic, captures it into the real ledger and Notion, reacts to it,
  and posts one smoke line to the ops topic.

Steps whose code lands in a later build ticket report SKIP with the ticket
number; wire each one in as its ticket merges.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import stat
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from jo_watchdog.adapters.notion import NotionAdapter, NotionError, QueryRequest
from jo_watchdog.adapters.protocols import IncomingUpdate
from jo_watchdog.app.config import (
    SECRET_KEYS,
    Config,
    load_config,
    missing_secrets,
    require_live_keys,
)
from jo_watchdog.app.worker import Worker
from jo_watchdog.core.masker import WITHHELD, outbound_text
from jo_watchdog.ledger.db import Ledger

ROOT = Path(__file__).resolve().parent
SMOKE_MARK = "SMOKE TEST — delete this row"

# Exact live property names the watchdog reads or writes (job-order field
# mapping; note the trailing space in "Blocker " and the emoji in Coordinator).
JOB_ORDER_PROPERTIES = [
    "Name", "JO Number", "Title", "Subtitle", "Date", "Deadline", "Department", "Status",
    "Task", "Purpose", "Source / Source Drive", "Platform", "Link Settings", "Watermark",
    "Mode of Transfer", "Blocker ", "Pending", "Notes", "Telegram Key", "🗂️ Coordinator",
]
CONTACT_PROPERTIES = ["Name", "TelegramUserID", "TelegramHandle", "Aliases", "End of Shift"]
PROJECT_PROPERTIES = ["Name", "Project Code"]
STATUS_OPTION = "Needs Intake Review"

# The three real JO-to-Deliverable pairs (deliverable-matching design, #26).
JEV_REGRESSION_DELIVERABLES = [
    "Moonglow FTR SCR",
    "Someone Someday WK11 VFX",
    "Mag-Iina FTR SCR",
]
JEV_CRITERIA = {
    "match": "The Job Order is clearly work for this Deliverable.",
    "maybe": "The Job Order could be for this Deliverable, but the text doesn't make it clear.",
    "no": "The Job Order is not for this Deliverable.",
}


# -- result plumbing ----------------------------------------------------------


@dataclass
class Result:
    status: str  # PASS | FAIL | SKIP | MANUAL
    detail: str = ""


def ok(detail: str = "") -> Result:
    return Result("PASS", detail)


def fail(detail: str) -> Result:
    return Result("FAIL", detail)


def skip(detail: str) -> Result:
    return Result("SKIP", detail)


def manual(detail: str) -> Result:
    return Result("MANUAL", detail)


class Smoke:
    """Shared state for one smoke run: config, clients, and facts found so far."""

    def __init__(self, config: Config, args: argparse.Namespace):
        self.config = config
        self.args = args
        self.notion: NotionAdapter | None = None
        self.bot = None
        self.schemas: dict[str, dict] = {}
        self.deliverable_ids: dict[str, str] = {}
        self.smoke_page_key: str | None = None

    async def open(self) -> None:
        if os.environ.get("NOTION_API_KEY"):
            self.notion = NotionAdapter(os.environ["NOTION_API_KEY"])
        if os.environ.get("TELEGRAM_BOT_TOKEN"):
            from aiogram import Bot

            self.bot = Bot(token=os.environ["TELEGRAM_BOT_TOKEN"])

    async def close(self) -> None:
        if self.notion:
            await self.notion.close()
        if self.bot:
            await self.bot.session.close()

    async def schema(self, data_source_id: str) -> dict:
        if data_source_id not in self.schemas:
            self.schemas[data_source_id] = await self.notion._request(
                "GET", f"/data_sources/{data_source_id}"
            )
        return self.schemas[data_source_id]


def _title_property(schema: dict) -> str:
    return next(n for n, p in schema["properties"].items() if p["type"] == "title")


def _plain(prop: dict) -> str:
    kind = prop.get("type")
    value = prop.get(kind)
    if kind in ("title", "rich_text"):
        return "".join(part.get("plain_text", "") for part in value or [])
    if kind in ("select", "status"):
        return (value or {}).get("name", "")
    if kind == "multi_select":
        return ", ".join(o["name"] for o in value or [])
    if kind == "number" and value is not None:
        return str(value)
    if kind == "date" and value:
        return value.get("start", "")
    return ""


# -- steps --------------------------------------------------------------------


async def step_config(s: Smoke) -> Result:
    require_live_keys(s.config)
    placeholders = [
        key
        for key in ("leizette_contact_page_id", "leizette_telegram_handle", "owner_telegram_handle")
        if str(getattr(s.config, key)).startswith(("00000000", "@leizette_handle", "@owner_handle"))
    ]
    missing = missing_secrets()
    if missing or placeholders:
        return fail(f"missing secrets: {missing or '-'}; placeholder values: {placeholders or '-'}")
    return ok(f"all {len(Config.model_fields)} keys valid; {len(SECRET_KEYS)} secrets present")


async def step_storage(s: Smoke) -> Result:
    ledger = Path(s.config.ledger_path)
    if not ledger.parent.exists():
        return fail(f"{ledger.parent} does not exist; create it with mode 0700")
    problems = []
    dir_mode = stat.S_IMODE(ledger.parent.stat().st_mode)
    if dir_mode & 0o077:
        problems.append(f"{ledger.parent} is {dir_mode:04o}, want 0700")
    for path in sorted(ledger.parent.iterdir()):
        mode = stat.S_IMODE(path.stat().st_mode)
        if path.is_file() and mode & 0o077:
            problems.append(f"{path.name} is {mode:04o}, want 0600")
    if problems:
        return fail("; ".join(problems))
    return ok(f"{ledger.parent} {dir_mode:04o}, files owner-only")


async def step_reactions(s: Smoke) -> Result:
    if not s.bot:
        return skip("TELEGRAM_BOT_TOKEN not set")
    chat = await s.bot.get_chat(s.config.intake_chat_id)
    available = chat.available_reactions
    if available is None:
        print("    available_reactions: absent (all standard emoji allowed)")
        return ok(f"{s.config.captured_reaction} allowed (no restriction)")
    emoji = [r.emoji for r in available if getattr(r, "type", "") == "emoji"]
    print(f"    available_reactions: {' '.join(emoji) or '(none)'}")
    if s.config.captured_reaction not in emoji:
        return fail(f"captured_reaction {s.config.captured_reaction} is not in the list above")
    return ok(f"{s.config.captured_reaction} is available")


async def step_bot_rights(s: Smoke) -> Result:
    if not s.bot:
        return skip("TELEGRAM_BOT_TOKEN not set")
    me = await s.bot.get_me()
    intake = await s.bot.get_chat_member(s.config.intake_chat_id, me.id)
    ops = await s.bot.get_chat_member(s.config.ops_chat_id, me.id)
    problems = []
    if intake.status not in ("administrator", "creator"):
        problems.append(f"not an admin of the intake group ({intake.status})")
    if ops.status in ("left", "kicked") or getattr(ops, "can_send_messages", True) is False:
        problems.append(f"cannot post in the ops group ({ops.status})")
    if problems:
        return fail("; ".join(problems))
    return ok(f"@{me.username}: intake {intake.status}, ops {ops.status}")


async def step_owner_handle(s: Smoke) -> Result:
    return manual(
        f"confirm {s.config.owner_telegram_handle} (owner) and "
        f"{s.config.leizette_telegram_handle} (Leizette) are the real handles; "
        "the Bot API cannot look a handle up"
    )


async def step_notion_access(s: Smoke) -> Result:
    if not s.notion:
        return skip("NOTION_API_KEY not set")
    names = {
        "Job Orders": s.config.job_orders_data_source_id,
        "Contacts": s.config.contacts_data_source_id,
        "Projects": s.config.projects_data_source_id,
        "Deliverables": s.config.deliverables_data_source_id,
    }
    denied = []
    for label, ds in names.items():
        try:
            await s.schema(ds)
            await s.notion.query_data_source(ds, QueryRequest(page_size=1))
        except NotionError as exc:
            denied.append(f"{label} ({exc.status} {exc.code})")
    if denied:
        return fail("integration cannot read: " + ", ".join(denied) + "; share them with it")
    return ok("Job Orders, Contacts, Projects, Deliverables readable")


async def step_notion_schema(s: Smoke) -> Result:
    if not s.notion:
        return skip("NOTION_API_KEY not set")
    jo = (await s.schema(s.config.job_orders_data_source_id))["properties"]
    contacts = (await s.schema(s.config.contacts_data_source_id))["properties"]
    projects = (await s.schema(s.config.projects_data_source_id))["properties"]
    missing = (
        [f"Job Orders.{n!r}" for n in JOB_ORDER_PROPERTIES if n not in jo]
        + [f"Contacts.{n!r}" for n in CONTACT_PROPERTIES if n not in contacts]
        + [f"Projects.{n!r}" for n in PROJECT_PROPERTIES if n not in projects]
    )
    problems = list(missing)
    status = jo.get("Status", {})
    status_options = [o["name"] for o in status.get(status.get("type"), {}).get("options", [])]
    if STATUS_OPTION not in status_options:
        problems.append(f"Status has no {STATUS_OPTION!r} option")
    department = jo.get("Department", {})
    departments = [o["name"] for o in department.get("select", {}).get("options", [])]
    if department and department.get("type") != "select":
        problems.append(f"Department is {department.get('type')}, want select")
    print(f"    Department options: {', '.join(departments) or '(none)'}")
    print(f"    Contacts End of Shift type: {contacts.get('End of Shift', {}).get('type')}")
    print(f"    Contacts Aliases type: {contacts.get('Aliases', {}).get('type')}")
    if problems:
        return fail("; ".join(problems))
    return ok("exact property names, Status and Department options present")


async def step_deliverable_relation(s: Smoke) -> Result:
    if not s.notion:
        return skip("NOTION_API_KEY not set")
    jo = (await s.schema(s.config.job_orders_data_source_id))["properties"]
    target = s.config.deliverables_data_source_id.replace("-", "")
    relations = [
        (name, prop["relation"])
        for name, prop in jo.items()
        if prop["type"] == "relation"
        and prop["relation"].get("data_source_id", "").replace("-", "") == target
    ]
    if not relations:
        return fail("Job Orders has no relation to Deliverables")
    name, relation = relations[0]
    if relation.get("type") != "dual_property":
        return fail(f"Job Orders.{name!r} is one-way ({relation.get('type')}), want reciprocal")
    synced = relation["dual_property"].get("synced_property_name")
    return ok(f"Job Orders.{name!r} <-> Deliverables.{synced!r} (reciprocal)")


async def step_relation_contains(s: Smoke) -> Result:
    if not s.notion:
        return skip("NOTION_API_KEY not set")
    jo_schema = await s.schema(s.config.job_orders_data_source_id)
    dl_schema = await s.schema(s.config.deliverables_data_source_id)
    dl_title = _title_property(dl_schema)
    target = s.config.deliverables_data_source_id.replace("-", "")
    relation = next(
        (
            n
            for n, p in jo_schema["properties"].items()
            if p["type"] == "relation"
            and p["relation"].get("data_source_id", "").replace("-", "") == target
        ),
        None,
    )
    if relation is None:
        return fail("no Job Orders -> Deliverables relation to filter on")
    found = []
    for name in JEV_REGRESSION_DELIVERABLES:
        page = await s.notion.query_data_source(
            s.config.deliverables_data_source_id,
            QueryRequest(page_size=1, filter={"property": dl_title, "title": {"equals": name}}),
        )
        if not page.results:
            continue
        s.deliverable_ids[name] = page.results[0]["id"]
        linked = await s.notion.query_data_source(
            s.config.job_orders_data_source_id,
            QueryRequest(
                page_size=5,
                filter={"property": relation, "relation": {"contains": page.results[0]["id"]}},
            ),
        )
        found.append(f"{name}: {len(linked.results)} JO")
    if not found:
        return fail("none of the regression Deliverables were found by title")
    if any(entry.endswith(": 0 JO") for entry in found):
        return fail("relation_contains returned no JO for: " + "; ".join(found))
    return ok("relation_contains works; " + "; ".join(found))


async def step_masker(s: Smoke) -> Result:
    cases = json.loads((ROOT / "tests/fixtures/masker_cases.json").read_text(encoding="utf-8"))
    wrong = [
        source
        for source, expected, count in cases["positive"]
        if outbound_text(source).text != expected or outbound_text(source).withheld_count != count
    ]
    leaked = [source for source in cases["negative"] if WITHHELD in outbound_text(source).text]
    if wrong or leaked:
        return fail(f"{len(wrong)} positive and {len(leaked)} negative cases failed")
    return ok(f"{len(cases['positive'])} positive, {len(cases['negative'])} negative cases")


async def step_pipeline(s: Smoke) -> Result:
    """Synthetic in-scope message through ledger, plan, Notion write and read-back."""
    if not s.notion:
        return skip("NOTION_API_KEY not set")

    class RecordingTelegram:
        def __init__(self, updates):
            self.updates, self.reactions = list(updates), []

        async def get_updates(self, offset, timeout):
            batch, self.updates = [u for u in self.updates if u.update_id >= offset], []
            return batch

        async def set_reaction(self, chat_id, message_id, emoji):
            self.reactions.append((chat_id, message_id, emoji))

    message_id = int(time.time())
    text = f"{SMOKE_MARK}\nPurpose: smoke check\npw: smoke-secret"
    synthetic = IncomingUpdate(
        update_id=1,
        chat_id=s.config.intake_chat_id,
        message_id=message_id,
        message_thread_id=s.config.intake_topic_id,
        text=text,
        raw={"update_id": 1, "smoke": True},
    )
    config = s.config.model_copy(update={"dry_run": False})
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Ledger(Path(tmp) / "smoke.db")
        telegram = RecordingTelegram([synthetic])
        worker = Worker(ledger=ledger, telegram=telegram, notion=s.notion, config=config)
        try:
            await worker.run_once()
            key = f"{synthetic.chat_id}:{message_id}"
            msg = ledger.get_message(key)
        finally:
            ledger.close()
    if msg["state"] != "done":
        return fail(f"stopped at {msg['state']}")
    if msg["readback_mismatch"]:
        return fail(f"read-back mismatch: {msg['readback_mismatch']}")
    page = await s.notion.retrieve_page(msg["notion_page_id"])
    s.smoke_page_key = key
    print(f"    created {page.get('url')} — delete it by hand")
    if "smoke-secret" in json.dumps(page):
        return fail("credential reached Notion properties")
    return ok("planned, written, read back, reaction recorded")


async def step_query_visibility(s: Smoke) -> Result:
    if not s.notion or not s.smoke_page_key:
        return skip("needs the pipeline step's page")
    for attempt in range(6):
        page = await s.notion.query_data_source(
            s.config.job_orders_data_source_id,
            QueryRequest(
                page_size=1,
                filter={"property": "Telegram Key", "rich_text": {"equals": s.smoke_page_key}},
            ),
        )
        if page.results:
            return ok(f"new page visible to query after ~{attempt * 5}s")
        await asyncio.sleep(5)
    return fail("new page not visible to a Telegram Key query after 25s")


async def step_unknown_select(s: Smoke) -> Result:
    if not s.args.probe_unknown_select:
        return skip("pass --probe-unknown-select (may add a Department option)")
    payload = {
        "parent": {"data_source_id": s.config.job_orders_data_source_id},
        "properties": {
            "Name": {"title": [{"text": {"content": f"{SMOKE_MARK} (unknown select)"}}]},
            "Status": {"status": {"name": STATUS_OPTION}},
            "Department": {"select": {"name": "ZZ-SMOKE-UNKNOWN"}},
        },
    }
    try:
        ref = await s.notion.create_page(payload)
    except NotionError as exc:
        return ok(f"unknown option rejected ({exc.status} {exc.code}); planner must blank it")
    return manual(
        f"Notion accepted and created the option ({ref.url}); delete the row and the "
        "ZZ-SMOKE-UNKNOWN option, and keep the planner validating against live options"
    )


async def step_deepseek(s: Smoke) -> Result:
    key = os.environ.get("DEEPSEEK_API_KEY")
    if not key:
        return skip("DEEPSEEK_API_KEY not set")
    body = {
        "model": s.config.deepseek_model,
        "response_format": {"type": "json_object"},
        "messages": [
            {
                "role": "system",
                "content": 'Reply in json: {"is_job_order": true|false, "jo_number": string|null}',
            },
            {"role": "user", "content": "SOMSOM - TOC - #0163\nTask: conform reel 3"},
        ],
    }
    async with httpx.AsyncClient(timeout=s.config.deepseek_timeout_seconds) as client:
        resp = await client.post(
            f"{s.config.deepseek_base_url.rstrip('/')}/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json=body,
        )
    if resp.status_code != 200:
        return fail(f"HTTP {resp.status_code}: {resp.text[:200]}")
    data = resp.json()
    try:
        answer = json.loads(data["choices"][0]["message"]["content"])
    except (KeyError, ValueError) as exc:
        return fail(f"no structured JSON in the reply: {exc}")
    if "is_job_order" not in answer:
        return fail(f"JSON missing is_job_order: {answer}")
    return ok(f"model {data.get('model')} returned {answer}")


def _jev_job_order_text(page: dict) -> tuple[str, str]:
    props = page["properties"]
    lines = []
    for name in ("Name", "Title", "Task", "Purpose", "Notes", "Platform"):
        if name in props and _plain(props[name]):
            lines.append(f"{name}: {_plain(props[name])}")
    project = _plain(props["Name"]).split(" - ")[0] if "Name" in props else ""
    return project, outbound_text("\n".join(lines)).text


async def step_jev(s: Smoke) -> Result:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        return skip("OPENROUTER_API_KEY not set")
    if not s.notion or not s.deliverable_ids:
        return skip("needs the relation_contains step to find the regression pairs")

    jo_schema = await s.schema(s.config.job_orders_data_source_id)
    target = s.config.deliverables_data_source_id.replace("-", "")
    relation = next(
        n
        for n, p in jo_schema["properties"].items()
        if p["type"] == "relation"
        and p["relation"].get("data_source_id", "").replace("-", "") == target
    )
    names = list(s.deliverable_ids)
    questions = {
        f"c{i}": {
            "type": "choice",
            "instructions": f"Does this Job Order belong to the Deliverable '{name}'?",
            "criteria": JEV_CRITERIA,
        }
        for i, name in enumerate(names)
    }
    lines, wrong, rate_headers, snapshot = [], [], set(), None
    async with httpx.AsyncClient(timeout=s.config.jev_timeout_seconds) as client:
        for expected in names:
            linked = await s.notion.query_data_source(
                s.config.job_orders_data_source_id,
                QueryRequest(
                    page_size=1,
                    filter={
                        "property": relation,
                        "relation": {"contains": s.deliverable_ids[expected]},
                    },
                ),
            )
            if not linked.results:
                continue
            project, text = _jev_job_order_text(linked.results[0])
            resp = await client.post(
                s.config.jev_url,
                headers={"Authorization": f"Bearer {key}"},
                json={
                    "model": s.config.jev_model,
                    "state": {"project": project, "job_order": text},
                    "questions": questions,
                },
            )
            limits = [h for h in resp.headers if h.lower().startswith("x-ratelimit")]
            rate_headers |= set(limits)
            if "retry-after" in resp.headers:
                rate_headers.add("retry-after")
            if resp.status_code == 429:
                return fail(
                    f"429; Retry-After={resp.headers.get('retry-after')}, X-RateLimit-*={limits}"
                )
            if resp.status_code != 200:
                return fail(f"HTTP {resp.status_code}: {resp.text[:200]}")
            data = resp.json()
            snapshot = data.get("model")
            for i, name in enumerate(names):
                answer = data["answers"][f"c{i}"]
                probs = answer["probabilities"]
                lines.append(
                    f"{expected} JO vs {name}: match={probs['match']:.2f} "
                    f"maybe={probs['maybe']:.2f} no={probs['no']:.2f}"
                )
                if name == expected and answer["choice"] != "match":
                    wrong.append(f"{expected} not top match")
                would_link = answer["choice"] == "match" and probs["match"] >= s.config.t_link
                if name != expected and would_link:
                    wrong.append(f"{expected} JO would link to {name}")
    print(f"    snapshot: {snapshot}")
    print(f"    rate-limit headers seen: {sorted(rate_headers) or 'none'}")
    for line in lines:
        print(f"    {line}")
    if wrong:
        # Shadow mode exists to observe this; report but do not block go-live.
        return manual("review before enabling linking: " + "; ".join(wrong))
    return ok(f"{len(names)} regression pairs scored by {snapshot}")


async def step_live_telegram(s: Smoke) -> Result:
    if not s.args.live_telegram:
        return skip("pass --live-telegram, then post a test JO in the intake topic")
    if not s.bot or not s.notion:
        return skip("TELEGRAM_BOT_TOKEN and NOTION_API_KEY are both needed")
    from jo_watchdog.adapters.telegram import AiogramTelegramClient

    ledger = Ledger(s.config.ledger_path)
    try:
        before = ledger.last_committed_offset()
        print(f"    waiting up to {s.config.poll_timeout}s for a post in the intake topic…")
        worker = Worker(
            ledger=ledger, telegram=AiogramTelegramClient(s.bot), notion=s.notion, config=s.config
        )
        await worker.run_once()
        rows = ledger._conn.execute(
            "SELECT telegram_key, state, notion_page_id FROM messages"
            " WHERE update_id > ? ORDER BY id",
            (before,),
        ).fetchall()
    finally:
        ledger.close()
    if not rows:
        return fail("no in-scope post arrived; check intake_chat_id/intake_topic_id")
    last = dict(rows[-1])
    expected = "planned" if s.config.dry_run else "done"
    if last["state"] != expected:
        return fail(f"{last['telegram_key']} stopped at {last['state']}, want {expected}")
    await s.bot.send_message(
        s.config.ops_chat_id,
        f"🧪 Smoke test: captured {last['telegram_key']} ({last['state']}).",
        message_thread_id=s.config.ops_topic_id,
    )
    return ok(f"{last['telegram_key']} {last['state']}; ops line posted")


def pending(ticket: str, what: str) -> Callable[[Smoke], Awaitable[Result]]:
    async def step(_: Smoke) -> Result:
        return skip(f"{what} — wire in when {ticket} merges")

    return step


STEPS: list[tuple[str, Callable[[Smoke], Awaitable[Result]]]] = [
    ("config", step_config),
    ("storage", step_storage),
    ("reactions", step_reactions),
    ("bot_rights", step_bot_rights),
    ("owner_handle", step_owner_handle),
    ("notion_access", step_notion_access),
    ("notion_schema", step_notion_schema),
    ("deliverable_relation", step_deliverable_relation),
    ("relation_contains", step_relation_contains),
    ("masker", step_masker),
    ("pipeline", step_pipeline),
    ("query_visibility", step_query_visibility),
    ("unknown_select", step_unknown_select),
    ("deepseek", step_deepseek),
    ("jev", step_jev),
    ("live_telegram", step_live_telegram),
    ("classification", pending("#20", "DeepSeek classify/extract on the real post")),
    ("threshold_tuning", pending("#22/#26", "fuzzy and Deliverable thresholds against the corpus")),
    ("readback_mismatch", pending("#23", "read-back mismatch reporting to ops")),
    ("outbox_retry", pending("#23", "outbox retry after a failed ops post")),
    ("dry_run_ops_post", pending("#23", "dry_run_plan outbox message in the ops topic")),
    ("forward_origins", pending("#24", "user, hidden_user, chat and channel forward origins")),
    ("channel_link", pending("#24", "real channel-origin message link")),
    ("notion_retry_after", pending("#25", "Notion 429 Retry-After handling")),
    ("heartbeat_format", pending("#25", "heartbeat formatting in the ops topic")),
    ("matcher_failure", pending("#26", "Jev failure leaves Deliverable blank and posts one line")),
    ("shadow_mode", pending("#26", "shadow mode writes no Deliverable relation")),
]


async def run(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    only = set(args.only.split(",")) if args.only else None
    unknown = (only or set()) - {name for name, _ in STEPS}
    if unknown:
        print(f"unknown steps: {', '.join(sorted(unknown))}")
        return 2
    s = Smoke(config, args)
    await s.open()
    counts: dict[str, int] = {}
    try:
        for name, step in STEPS:
            if only and name not in only:
                continue
            try:
                result = await step(s)
            except Exception as exc:  # one broken step must not hide the rest
                result = fail(f"{type(exc).__name__}: {outbound_text(str(exc)).text}")
            counts[result.status] = counts.get(result.status, 0) + 1
            print(f"{result.status:<6} {name}: {result.detail}")
    finally:
        await s.close()
    print("\n" + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
    return 1 if counts.get("FAIL") else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--only", help="comma-separated step names to run")
    parser.add_argument("--live-telegram", action="store_true", help="capture one real post")
    parser.add_argument(
        "--probe-unknown-select",
        action="store_true",
        help="write an unknown Department option to see how Notion reacts",
    )
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
