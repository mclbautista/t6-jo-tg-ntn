#!/usr/bin/env python3
"""Manual real-account smoke verification (issue #27). Never run in CI.

Checks the assumptions CI deliberately excludes: live Telegram, Notion, DeepSeek
and Jev behavior, the deployment's storage permissions, and the full capture
pipeline against real accounts. Each step prints PASS, FAIL, SKIP, MANUAL or
ATTESTED. The run ends with a verdict, and only FULL VERIFICATION exits 0:

- FULL (exit 0): every required step is PASS or ATTESTED. Only ``unknown_select``
  and ``live_telegram`` are optional.
- FAILED (exit 1): any step failed.
- INCOMPLETE or PARTIAL (exit 3): a required step was SKIP, MANUAL or not run, or
  ``--only`` ran a subset. A skipped check is never a success.

    python smoke.py --config config.yaml                 # everything except live Telegram
    python smoke.py --config config.yaml --live-telegram --live-message-id 12345
    python smoke.py --config config.yaml --fixture-message-id 900001 --attestations att.json
    python smoke.py --config config.yaml --only reactions,notion_access

Secrets come from the environment (``.env``): TELEGRAM_BOT_TOKEN,
NOTION_API_KEY, DEEPSEEK_API_KEY, OPENROUTER_API_KEY.

Side effects, by step:

- ``pipeline`` and ``dry_run`` build the worker with the production wiring
  (``jo_watchdog.app.wiring``: caches, Jev matcher, Deliverable source) on a
  throwaway ledger, with a fixed classifier verdict and a recording Telegram.
  The fixture is one explicit message (``--fixture-message-id``, printed). The
  step verifies that message's classification, plan, Notion write and read-back,
  delivered capture report and ``reaction_state = set``, not the last ledger row.
  ``pipeline`` creates one clearly marked Job Order (the integration cannot
  archive it; the URL is printed so you can delete it by hand).
- ``unknown_select`` (only with ``--probe-unknown-select``) creates a second
  marked Job Order with an unknown Department option, which Notion may add to
  the schema; delete the option by hand if it does.
- ``live_telegram`` (only with ``--live-telegram --live-message-id N``) waits for
  the one intake post with that Telegram message id and ignores every other
  update. Post a test JO in the intake topic first and note its message id. It
  captures into a throwaway ledger (the service's ledger and offset are not
  touched), writes a real Notion row, reacts to the post, posts the capture
  report and one smoke line to the ops topic, then runs the same checks.
  Stop the running watchdog first: two pollers cannot share one bot token.
  Cleanup: delete the Notion row by hand (URL printed) and the smoke line.

Steps the script cannot run report SKIP (or MANUAL). A person who checked one by
hand records it in an ``--attestations`` JSON list of ``{step, actor, at, scope,
evidence}`` (``at`` an ISO-8601 time); all five fields are required, and an
attestation settles only a SKIP or MANUAL step, never a FAIL or PASS.

Checks that are covered by CI but not yet automated against the real accounts
report SKIP with their ticket number; verify those by hand and attest them.
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
from pathlib import Path

import httpx

from jo_watchdog.adapters.notion import NotionAdapter, NotionError, QueryRequest
from jo_watchdog.adapters.protocols import ClassifyResult, IncomingUpdate
from jo_watchdog.app.config import (
    SECRET_KEYS,
    Config,
    load_config,
    missing_secrets,
    placeholder_keys,
    require_live_keys,
)
from jo_watchdog.app.smoke_gate import (
    FAIL,
    MANUAL,
    PASS,
    SKIP,
    Result,
    attest,
    load_attestations,
    verdict,
    verify_capture,
)
from jo_watchdog.app.wiring import build_adapters, build_worker, missing_wiring
from jo_watchdog.core.extraction import EXTRACTED_FIELDS, PROMPT_TAG, Extraction
from jo_watchdog.core.masker import WITHHELD, outbound_text
from jo_watchdog.core.report import DRY_RUN_HEADER
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


class RecordingTelegram:
    """Records reactions and ops posts instead of sending them (no token needed)."""

    def __init__(self, updates=()):
        self.updates: list[IncomingUpdate] = list(updates)
        self.reactions: list[tuple[int, int, str]] = []
        self.sent: list[tuple[int, int, str]] = []

    async def get_updates(self, offset: int, timeout: int) -> list[IncomingUpdate]:
        batch, self.updates = [u for u in self.updates if u.update_id >= offset], []
        return batch

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        self.reactions.append((chat_id, message_id, emoji))

    async def send_message(self, chat_id: int, topic_id: int, text: str) -> int:
        self.sent.append((chat_id, topic_id, text))
        return len(self.sent)


class AlwaysJoClassifier:
    """Stands in for DeepSeek in the synthetic pipeline steps."""

    async def classify(self, masked_text: str, origin_lines) -> ClassifyResult:
        extraction = Extraction(
            verdict="jo",
            confidence="high",
            reasons="Smoke test stub.",
            multiple_jo_suspected=False,
            **{field: None for field in EXTRACTED_FIELDS},
        )
        return ClassifyResult(
            outcome="ok", extraction=extraction, requested_model="stub", prompt_tag=PROMPT_TAG
        )


def synthetic_update(config: Config, text: str, message_id: int) -> IncomingUpdate:
    return IncomingUpdate(
        update_id=1,
        chat_id=config.intake_chat_id,
        message_id=message_id,
        message_thread_id=config.intake_topic_id,
        text=text,
        raw={"update_id": 1, "smoke": True},
    )


# -- result plumbing ----------------------------------------------------------


def ok(detail: str = "") -> Result:
    return Result(PASS, detail)


def fail(detail: str) -> Result:
    return Result(FAIL, detail)


def skip(detail: str) -> Result:
    return Result(SKIP, detail)


def manual(detail: str) -> Result:
    return Result(MANUAL, detail)


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
        # One explicit identity for the synthetic fixture, printed so it can be traced.
        self.fixture_message_id = args.fixture_message_id or int(time.time() * 1000) % 2_000_000_000

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
    placeholders = placeholder_keys(s.config)
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


class ReadOnlyNotion:
    """Real Notion for cache refreshes and Deliverable lookups; page writes are forbidden."""

    def __init__(self, notion):
        self._notion = notion

    def __getattr__(self, name):
        return getattr(self._notion, name)

    async def create_page(self, payload):
        raise AssertionError("dry run called Notion create")

    async def retrieve_page(self, page_id):
        raise AssertionError("dry run called Notion retrieve")


async def synthetic_capture(s: Smoke, text: str, *, dry_run: bool) -> tuple[Result, object]:
    """Run one named fixture through the production-wired worker on an isolated ledger.

    Real Notion, caches, Jev matcher and Deliverable source; only Telegram (a
    recorder) and the classifier (a fixed verdict) are stood in, so the message's
    own ledger, Notion and outbox facts can be checked.
    """
    if missing_secrets():
        return skip(f"production wiring needs every secret; missing {missing_secrets()}"), None
    synthetic = synthetic_update(s.config, text, s.fixture_message_id)
    key = f"{synthetic.chat_id}:{synthetic.message_id}"
    config = s.config.model_copy(update={"dry_run": dry_run})
    telegram = RecordingTelegram([synthetic])
    adapters = build_adapters(config)
    notion = ReadOnlyNotion(adapters.notion) if dry_run else adapters.notion
    adapters = adapters.with_(telegram=telegram, classifier=AlwaysJoClassifier(), notion=notion)
    with tempfile.TemporaryDirectory() as tmp:
        ledger = Ledger(Path(tmp) / "smoke.db")
        try:
            worker = build_worker(config, ledger, adapters)
            if absent := missing_wiring(worker):
                return fail(f"smoke worker lacks production wiring: {absent}"), telegram
            for _ in range(3):  # poll, drain, deliver; a later cycle only finishes the tail
                await worker.run_once()
                msg = ledger.get_message(key)
                if msg and msg["state"] in ("done", "ignored", "exhausted"):
                    break
                if dry_run and msg and msg["state"] == "planned":
                    break
            result = await verify_capture(
                ledger,
                key,
                notion=adapters.notion,
                expect_dry_run=dry_run,
                recorded_reactions=telegram.reactions,
            )
            msg = ledger.get_message(key)
            s.smoke_page_key = key if msg and msg["notion_page_id"] else None
            if msg and msg["notion_page_id"]:
                page = await adapters.notion.retrieve_page(msg["notion_page_id"])
                print(f"    created {page.get('url')} — delete it by hand")
                if "smoke-secret" in json.dumps(page):
                    return fail("credential reached Notion properties"), telegram
        finally:
            await adapters.close()
            ledger.close()
    return result, telegram


async def step_pipeline(s: Smoke) -> Result:
    """Synthetic in-scope message: ledger, plan, Notion write, read-back, outbox, reaction."""
    print(f"    fixture message {s.config.intake_chat_id}:{s.fixture_message_id}")
    result, _ = await synthetic_capture(
        s, f"{SMOKE_MARK}\nPurpose: smoke check\npw: smoke-secret", dry_run=False
    )
    if result.status == PASS:
        result.detail += " (Telegram recorded, not live)"
    return result


async def step_dry_run(s: Smoke) -> Result:
    """Dry-run stops at `planned`: one masked ops post, no Notion call, no reaction."""
    result, telegram = await synthetic_capture(s, f"{SMOKE_MARK}\npw: smoke-secret", dry_run=True)
    if result.status != PASS:
        return result
    posts = [text for _, _, text in telegram.sent if DRY_RUN_HEADER in text]
    if telegram.reactions or len(posts) != 1:
        return fail(f"{len(telegram.reactions)} reaction(s), {len(posts)} dry-run post(s)")
    if "smoke-secret" in posts[0]:
        return fail("dry-run post leaked a credential")
    if s.bot and s.args.live_telegram:
        await s.bot.send_message(
            s.config.ops_chat_id, posts[0], message_thread_id=s.config.ops_topic_id,
            parse_mode="HTML",
        )
        return ok("held at planned; masked plan posted to the ops topic")
    return ok("held at planned; one masked plan rendered, no Notion write, no reaction")


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
    from jo_watchdog.adapters.deepseek import DeepSeekClassifier

    classifier = DeepSeekClassifier(
        key,
        base_url=s.config.deepseek_base_url,
        model=s.config.deepseek_model,
        timeout_seconds=s.config.deepseek_timeout_seconds,
    )
    try:
        result = await classifier.classify(
            outbound_text("SOMSOM - TOC - #0163\nTask: conform reel 3\npw: hunter22").text, []
        )
    finally:
        await classifier.close()
    if result.extraction is None:
        return fail(f"{result.outcome}: {result.error}")
    print(f"    returned model: {result.returned_model}; tokens "
          f"{result.input_tokens}/{result.output_tokens}; {result.latency_ms} ms")
    if result.extraction.verdict != "jo":
        return manual(f"structured output OK but verdict {result.extraction.verdict!r}")
    return ok(f"structured extraction from {result.returned_model}")


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


class OnlyMessage:
    """Passes through only the named intake message so a live run cannot capture others."""

    def __init__(self, telegram, chat_id: int, message_id: int):
        self._telegram = telegram
        self._identity = (chat_id, message_id)

    def __getattr__(self, name):
        return getattr(self._telegram, name)

    async def get_updates(self, offset: int, timeout: int):
        updates = await self._telegram.get_updates(offset, timeout)
        return [u for u in updates if (u.chat_id, u.message_id) == self._identity]


async def step_live_telegram(s: Smoke) -> Result:
    if not s.args.live_telegram:
        return skip("pass --live-telegram --live-message-id <id> after posting a test JO")
    if not s.args.live_message_id:
        return skip("--live-message-id is required: the Telegram message id of the post to capture")
    if missing_secrets():
        return skip(f"production wiring needs every secret; missing {missing_secrets()}")
    chat_id, message_id = s.config.intake_chat_id, s.args.live_message_id
    key = f"{chat_id}:{message_id}"
    adapters = build_adapters(s.config)
    adapters = adapters.with_(telegram=OnlyMessage(adapters.telegram, chat_id, message_id))
    with tempfile.TemporaryDirectory() as tmp:
        # Isolated ledger: the service's real ledger and offset are never touched.
        ledger = Ledger(Path(tmp) / "smoke-live.db")
        try:
            worker = build_worker(s.config, ledger, adapters)
            if absent := missing_wiring(worker):
                return fail(f"smoke worker lacks production wiring: {absent}")
            print(f"    waiting up to {s.config.poll_timeout}s for {key} in the intake topic…")
            for _ in range(3):
                await worker.run_once()
                msg = ledger.get_message(key)
                if msg and msg["state"] in ("done", "planned", "ignored", "exhausted"):
                    break
            msg = ledger.get_message(key)
            if msg is None:
                return fail(f"{key} did not arrive; check the id and the intake chat/topic")
            if msg["state"] == "ignored":
                return manual(f"{key} was classified not-a-JO; post a real JO and re-run")
            result = await verify_capture(
                ledger, key, notion=adapters.notion, expect_dry_run=s.config.dry_run
            )
        finally:
            await adapters.close()
            ledger.close()
    if result.status != PASS:
        return result
    await s.bot.send_message(
        s.config.ops_chat_id,
        f"🧪 Smoke test: captured {key}.",
        message_thread_id=s.config.ops_topic_id,
    )
    return ok(f"{result.detail}; ops line posted")


def pending(ticket: str, what: str) -> Callable[[Smoke], Awaitable[Result]]:
    async def step(_: Smoke) -> Result:
        return skip(f"{what} — not automated yet ({ticket}); check by hand")

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
    ("dry_run", step_dry_run),
    ("live_telegram", step_live_telegram),
    ("threshold_tuning", pending("#22/#26", "fuzzy and Deliverable thresholds against the corpus")),
    ("readback_mismatch", pending("#23", "read-back mismatch reporting to ops")),
    ("outbox_retry", pending("#23", "outbox retry after a failed ops post")),
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
    names = [name for name, _ in STEPS]
    unknown = (only or set()) - set(names)
    if unknown:
        print(f"unknown steps: {', '.join(sorted(unknown))}")
        return 2
    try:
        attestations = (
            load_attestations(args.attestations, set(names)) if args.attestations else {}
        )
    except (OSError, ValueError, TypeError) as exc:
        print(f"bad attestations file: {exc}")
        return 2
    s = Smoke(config, args)
    await s.open()
    results: dict[str, Result] = {}
    try:
        for name, step in STEPS:
            if only and name not in only:
                continue
            try:
                result = await step(s)
            except Exception as exc:  # one broken step must not hide the rest
                result = fail(f"{type(exc).__name__}: {outbound_text(str(exc)).text}")
            result = attest(result, attestations.get(name))
            results[name] = result
            print(f"{result.status:<8} {name}: {result.detail}")
    finally:
        await s.close()
    counts: dict[str, int] = {}
    for result in results.values():
        counts[result.status] = counts.get(result.status, 0) + 1
    print("\n" + ", ".join(f"{n} {k}" for k, n in sorted(counts.items())))
    final = verdict(results, names, selected_only=bool(only))
    print(f"\n{final.label} VERIFICATION" + "".join(f"\n  {r}" for r in final.reasons))
    return final.exit_code


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--only", help="comma-separated step names to run")
    parser.add_argument("--live-telegram", action="store_true", help="capture one real post")
    parser.add_argument(
        "--live-message-id",
        type=int,
        help="with --live-telegram: the Telegram message id of the one post to capture",
    )
    parser.add_argument(
        "--fixture-message-id",
        type=int,
        help="message id for the synthetic fixture (default: derived from the clock)",
    )
    parser.add_argument(
        "--attestations",
        help="JSON list of {step, actor, at, scope, evidence} settling SKIP/MANUAL steps by hand",
    )
    parser.add_argument(
        "--probe-unknown-select",
        action="store_true",
        help="write an unknown Department option to see how Notion reacts",
    )
    sys.exit(asyncio.run(run(parser.parse_args())))


if __name__ == "__main__":
    main()
