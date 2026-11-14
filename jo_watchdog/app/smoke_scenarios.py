"""Controlled operational-failure scenarios for ``smoke.py`` (#62).

Each scenario builds the production worker through ``build_worker`` on a throwaway
SQLite ledger, with scripted stand-ins for Telegram, Notion, the classifier and Jev
and a fake clock, then provokes one failure or mode and checks what the ledger,
the outbox and the stand-ins recorded. Nothing here touches the network, the
service's ledger or a real account, so it needs no secrets and leaves nothing to
clean up. ``smoke.py`` may forward the rendered ops posts to the real ops topic
when the operator passes ``--live-telegram``.

Offline tests drive the same functions, so the scenarios are verified in CI and
the manual run only repeats them against the deployed code and configuration.
"""

from __future__ import annotations

import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from jo_watchdog.adapters.jev import MatcherError
from jo_watchdog.adapters.protocols import ClassifyResult, IncomingUpdate, PageRef
from jo_watchdog.app.caches import CacheManager
from jo_watchdog.app.config import Config
from jo_watchdog.app.smoke_gate import FAIL, PASS, Result
from jo_watchdog.app.wiring import Adapters, build_worker, missing_wiring
from jo_watchdog.core.extraction import EXTRACTED_FIELDS, PROMPT_TAG, Extraction
from jo_watchdog.core.heartbeat import heartbeat_window
from jo_watchdog.core.masker import outbound_text
from jo_watchdog.core.matching import Candidate, CandidateDecision, MatchResult
from jo_watchdog.ledger.db import Ledger, fmt_utc

# 09:00 in Manila, two hours after that day's 07:00 boundary.
START = datetime(2026, 10, 22, 1, 0, tzinfo=UTC)
JO_TEXT = "MFG - TOC - JO #74\nTask: cut teaser"
PROJECT_ID = "proj-mfg"
DELIVERABLE = Candidate(page_id="d-ftr", name="Moonglow FTR SCR")
OTHER_DELIVERABLE = Candidate(page_id="d-promo", name="Moonglow Promo")


@dataclass
class ScenarioRun:
    """A scenario's verdict and the ops posts it rendered (for an optional live relay)."""

    result: Result
    ops_posts: list[str] = field(default_factory=list)


class FakeClock:
    def __init__(self, start: datetime = START):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta) -> None:
        self.now += timedelta(**delta)


class RateLimited(Exception):
    """A Telegram flood-wait: the provider asked for ``retry_after`` seconds."""

    def __init__(self, retry_after: float):
        super().__init__(f"Too Many Requests: retry after {retry_after:g}")
        self.retry_after = retry_after


class ScriptedTelegram:
    """Queues one update; fails the first sends/reactions as scripted."""

    def __init__(
        self, update: IncomingUpdate | None = None, *, send_failures=(), always_fail_send=False,
        reaction_failures=0,
    ):
        self.updates = [update] if update else []
        self.send_failures = list(send_failures)  # exceptions raised by the next sends
        self.always_fail_send = always_fail_send
        self.reaction_failures = reaction_failures
        self.send_calls = 0
        self.reactions: list[tuple[int, int, str]] = []
        self.sent: list[tuple[int, int, str]] = []

    async def get_updates(self, offset: int, timeout: int) -> list[IncomingUpdate]:
        batch, self.updates = [u for u in self.updates if u.update_id >= offset], []
        return batch

    async def set_reaction(self, chat_id: int, message_id: int, emoji: str) -> None:
        if self.reaction_failures:
            self.reaction_failures -= 1
            raise RuntimeError("reaction not allowed")
        self.reactions.append((chat_id, message_id, emoji))

    async def send_message(self, chat_id: int, topic_id: int, text: str) -> int:
        self.send_calls += 1
        if self.send_failures:
            raise self.send_failures.pop(0)
        if self.always_fail_send:
            raise RuntimeError("ops post failed")
        self.sent.append((chat_id, topic_id, text))
        return 9000 + len(self.sent)


class ScriptedNotion:
    """Echoes the written Name and Telegram Key back; optionally reads back a wrong Name."""

    def __init__(self, *, wrong_name: bool = False):
        self.wrong_name = wrong_name
        self.created: list[dict] = []
        self.retrieved: list[str] = []
        self.pages: dict[str, dict] = {}

    async def create_page(self, payload: dict) -> PageRef:
        page_id = f"page-{len(self.created) + 1}"
        props = payload["properties"]
        name = "WRONG NAME" if self.wrong_name else props["Name"]["title"][0]["text"]["content"]
        key = props["Telegram Key"]["rich_text"][0]["text"]["content"]
        self.pages[page_id] = {
            "id": page_id,
            "url": f"https://notion.example/{page_id}",
            "properties": {
                "Name": {"title": [{"plain_text": name}]},
                "Telegram Key": {"rich_text": [{"plain_text": key}]},
            },
        }
        self.created.append(payload)
        return PageRef(id=page_id, url=f"https://notion.example/{page_id}")

    async def retrieve_page(self, page_id: str) -> dict:
        self.retrieved.append(page_id)
        return self.pages[page_id]

    async def find_pages_by_telegram_key(self, data_source_id: str, telegram_key: str):
        return []

    async def query_project_deliverables(self, data_source_id: str, project_page_id: str):
        return [DELIVERABLE, OTHER_DELIVERABLE]


class FixedClassifier:
    """A fixed 'this is a Job Order' verdict, standing in for DeepSeek."""

    async def classify(self, masked_text: str, origin_lines=None) -> ClassifyResult:
        fields = {name: None for name in EXTRACTED_FIELDS}
        fields.update(title="Teaser", task="Cut teaser")
        extraction = Extraction(
            verdict="jo",
            confidence="high",
            reasons="Smoke scenario stub.",
            multiple_jo_suspected=False,
            **fields,
        )
        return ClassifyResult(
            outcome="ok", extraction=extraction, requested_model="stub", prompt_tag=PROMPT_TAG
        )


class ScriptedMatcher:
    """Answers with fixed probabilities, or raises the scripted ``MatcherError``."""

    def __init__(self, *, fail: bool = False):
        self.fail = fail
        self.calls = 0

    async def match(self, state, project_name, candidates):
        self.calls += 1
        if self.fail:
            raise MatcherError("http_error", "scripted outage")
        odds = {"d-ftr": (0.95, 0.04, 0.01), "d-promo": (0.02, 0.08, 0.90)}
        return MatchResult(
            decisions=[
                CandidateDecision(page_id=c.page_id, p_match=odds[c.page_id][0],
                                  p_maybe=odds[c.page_id][1], p_no=odds[c.page_id][2])
                for c in candidates
            ],
            requested_model="~typesafe/jev-latest",
            returned_model="typesafe/jev-smoke",
            input_tokens=200,
            output_tokens=30,
        )


class Harness:
    """One scenario's world: production worker, real SQLite, fake clock, scripted adapters."""

    def __init__(self, config: Config, tmp: str, *, telegram=None, notion=None, matcher=None,
                 text: str = JO_TEXT, queue_update: bool = True, **overrides):
        self.clock = FakeClock()
        self.config = config.model_copy(update=overrides)
        self.sleeps: list[float] = []
        self.on_sleep: Callable[[float], None] | None = None
        self.telegram = telegram or ScriptedTelegram()
        self.key = f"{self.config.intake_chat_id}:42"
        if queue_update:
            self.telegram.updates = [self.intake_post(1, 42, text)]
        self.notion = notion or ScriptedNotion()
        self.matcher = matcher
        self.ledger = Ledger(Path(tmp) / "scenario.db")
        adapters = Adapters(
            telegram=self.telegram, notion=self.notion, studio=self.notion,
            classifier=FixedClassifier(),
            matcher=matcher,
        )
        self.worker = build_worker(
            self.config, self.ledger, adapters, clock=self.clock, sleep=self._sleep
        )
        self.unwired = missing_wiring(self.worker) if matcher else []
        self._seed_caches()

    def intake_post(self, update_id: int, message_id: int, text: str) -> IncomingUpdate:
        """An intake-topic post dated by the fake clock."""
        chat_id = self.config.intake_chat_id
        return IncomingUpdate(
            update_id=update_id,
            chat_id=chat_id,
            message_id=message_id,
            message_thread_id=self.config.intake_topic_id,
            text=text,
            raw={
                "update_id": update_id,
                "message": {
                    "chat": {"id": chat_id},
                    "date": int(self.clock().timestamp()),
                    "text": text,
                },
            },
        )

    async def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if self.on_sleep:
            self.on_sleep(seconds)

    def _seed_caches(self) -> None:
        """Seed the cache snapshots the way a refresh would, so no Notion lookup is needed."""
        fetched = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        caches: CacheManager = self.worker.caches
        caches._apply("people", [{"page_id": "c-priya", "name": "Priya"}], fetched)
        caches._apply(
            "projects", [{"page_id": PROJECT_ID, "name": "Manufacturing", "code": "MFG"}], fetched
        )
        caches._apply("departments", ["TOC", "VID"], fetched)
        caches.last_attempt = datetime.now(UTC)  # the seeded snapshots are fresh: no live refresh

    def message(self) -> dict:
        return self.ledger.get_message(self.key)

    def outbox(self, kind: str) -> list[dict]:
        rows = self.ledger._conn.execute(
            "SELECT * FROM outbox WHERE kind = ? ORDER BY id", (kind,)
        )
        return [dict(r) for r in rows]

    def close(self) -> None:
        self.ledger.close()


class Checks:
    """Collects failed expectations and the evidence of the passing ones."""

    def __init__(self):
        self.failures: list[str] = []
        self.evidence: list[str] = []

    def expect(self, condition: bool, passed: str, failed: str) -> None:
        if condition:
            self.evidence.append(passed)
        else:
            self.failures.append(failed)

    def result(self) -> Result:
        if self.failures:
            return Result(FAIL, "; ".join(self.failures))
        return Result(PASS, "; ".join(self.evidence))


async def _scenario(
    config: Config,
    body: Callable[[Harness, Checks], Awaitable[None]],
    **harness_args,
) -> ScenarioRun:
    """Run ``body`` in a fresh harness with the ledger clock pointed at the fake clock."""
    checks = Checks()
    with tempfile.TemporaryDirectory() as tmp:
        harness = Harness(config, tmp, **harness_args)
        # The ledger stamps rows with its own clock; one clock keeps time controllable.
        with mock.patch("jo_watchdog.ledger.db._now", lambda: fmt_utc(harness.clock())):
            try:
                if harness.unwired:
                    checks.failures.append(f"worker lacks production wiring: {harness.unwired}")
                else:
                    await body(harness, checks)
            except Exception as exc:  # a crashed pipeline is a failed scenario, not a crash
                checks.failures.append(f"{type(exc).__name__}: {outbound_text(str(exc)).text}")
            finally:
                posts = [text for _, _, text in harness.telegram.sent]
                harness.close()
    return ScenarioRun(checks.result(), posts)


# -- scenarios ------------------------------------------------------------------


async def readback_mismatch(config: Config) -> ScenarioRun:
    """Notion reads back a different Name: recorded and reported, never rewritten."""

    async def body(h: Harness, c: Checks) -> None:
        await h.worker.run_once()
        msg = h.message()
        c.expect(
            bool(msg["readback_mismatch"]) and "Name differs" in msg["readback_mismatch"],
            f"mismatch recorded ({msg['readback_mismatch']})",
            f"no read-back mismatch recorded: {msg['readback_mismatch']!r}",
        )
        c.expect(len(h.notion.created) == 1, "one Notion create, no rewrite",
                 f"{len(h.notion.created)} Notion creates, want 1")
        reports = h.outbox("capture_report")
        c.expect(
            len(reports) == 1 and reports[0]["state"] == "sent"
            and "Notion row differs from the plan" in reports[0]["text"],
            "one capture report delivered naming the differing row",
            f"capture report missing the mismatch line: {[r['text'] for r in reports]}",
        )
        c.expect(msg["state"] == "done" and msg["reaction_state"] == "set",
                 "capture still finished and was reacted to",
                 f"mismatch blocked the capture: {msg['state']}/{msg['reaction_state']}")

    return await _scenario(config, body, notion=ScriptedNotion(wrong_name=True))


async def outbox_retry(config: Config) -> ScenarioRun:
    """A failed ops post is retried on the corrected policy: Retry-After, then five attempts."""
    schedule = config.retry_schedule_seconds

    async def honours_retry_after(h: Harness, c: Checks) -> None:
        await h.worker.run_once()
        (row,) = h.outbox("capture_report")
        wanted = fmt_utc(h.clock() + timedelta(seconds=600))
        c.expect(
            row["state"] == "pending" and row["attempts"] == 1 and row["next_attempt_at"] == wanted,
            f"Retry-After 600s beat the {schedule[0]}s delay (next attempt {wanted})",
            f"after a 600s Retry-After the row is {row['state']} next at {row['next_attempt_at']}",
        )
        calls = h.telegram.send_calls
        h.clock.advance(seconds=599)
        await h.worker.run_once()
        c.expect(h.telegram.send_calls == calls, "nothing re-sent before the wait is over",
                 "ops post retried before Retry-After elapsed")
        h.clock.advance(seconds=1)
        await h.worker.run_once()
        (row,) = h.outbox("capture_report")
        c.expect(
            row["state"] == "sent" and row["attempts"] == 2 and len(h.telegram.sent) == 1,
            "delivered exactly once on attempt 2",
            f"after the wait the row is {row['state']} after {row['attempts']} attempts",
        )
        c.expect(len(h.notion.created) == 1, "the retry never re-captured the JO",
                 "the retry created a second Notion row")

    first = await _scenario(
        config, honours_retry_after,
        telegram=ScriptedTelegram(send_failures=[RateLimited(600)]),
        matcher=ScriptedMatcher(),  # its Deliverable hint makes the capture worth a report
    )

    async def exhausts(h: Harness, c: Checks) -> None:
        for _ in range(len(schedule) + 1):
            await h.worker.run_once()
            h.clock.advance(seconds=max(schedule) + 1)
        (row,) = h.outbox("capture_report")
        c.expect(
            row["state"] == "failed" and row["attempts"] == len(schedule) + 1,
            f"exhausted after {len(schedule) + 1} total attempts and kept for manual retry",
            f"after {len(schedule) + 1} failures the row is {row['state']} "
            f"with {row['attempts']} attempts",
        )

    second = await _scenario(
        config, exhausts,
        telegram=ScriptedTelegram(always_fail_send=True),
        matcher=ScriptedMatcher(),
    )
    result = first.result if first.result.status == FAIL else second.result
    if first.result.status == PASS and second.result.status == PASS:
        result = Result(PASS, f"{first.result.detail}; {second.result.detail}")
    return ScenarioRun(result, first.ops_posts + second.ops_posts)


async def heartbeat_format(config: Config) -> ScenarioRun:
    """A capture written just before 07:00 is counted in that window, even if reacted after."""
    tz = ZoneInfo(config.timezone)

    async def body(h: Harness, c: Checks) -> None:
        await h.worker.run_once()  # a new ledger skips its first window
        c.expect(not h.outbox("heartbeat"), "a brand-new ledger sends no heartbeat",
                 "a heartbeat was sent for a ledger with no history")
        # Land the capture 2 seconds before the boundary; its reaction retry crosses it.
        boundary_start, boundary_end = heartbeat_window(
            START + timedelta(days=1), config.heartbeat_time, tz
        )
        h.clock.now = boundary_end - timedelta(seconds=2)
        h.telegram.reaction_failures = 1
        h.on_sleep = lambda seconds: h.clock.advance(seconds=seconds)
        h.telegram.updates = [h.intake_post(2, 43, "MFG - TOC - JO #75\nTask: cut trailer")]
        await h.worker.run_once()
        key = f"{h.config.intake_chat_id}:43"
        msg = h.ledger.get_message(key)
        c.expect(
            msg["state"] == "done" and msg["reaction_state"] == "set"
            and h.clock() >= boundary_end,
            "reaction completed after the boundary",
            f"scenario setup failed: {msg['state']}/{msg['reaction_state']} at {h.clock()}",
        )
        rows = h.outbox("heartbeat")
        c.expect(len(rows) == 1 and rows[0]["state"] == "sent", "one heartbeat delivered",
                 f"{len(rows)} heartbeat rows: {[r['state'] for r in rows]}")
        if rows:
            text = rows[0]["text"]
            first_line = text.splitlines()[0]
            want = (
                f"📋 Daily heartbeat — {boundary_start.astimezone(tz):%b %d %H:%M}"
                f" → {boundary_end.astimezone(tz):%b %d %H:%M}"
            )
            c.expect(first_line == want, f"window labelled {want!r}",
                     f"heartbeat header {first_line!r}, want {want!r}")
            c.expect("Captured: 1" in text.splitlines(),
                     "the capture written before 07:00 counts in that window",
                     "capture written before the boundary missing from its window")
        await h.worker.run_once()
        c.expect(len(h.outbox("heartbeat")) == 1, "the window is reported once",
                 "the same window was reported twice")

    return await _scenario(config, body, queue_update=False)


async def matcher_failure(config: Config) -> ScenarioRun:
    """Jev is down: the JO is still captured, Deliverable stays blank, one line reports it."""

    async def body(h: Harness, c: Checks) -> None:
        await h.worker.run_once()
        msg = h.message()
        props = h.notion.created[0]["properties"] if h.notion.created else {}
        c.expect(h.matcher.calls >= 1, "the matcher was attempted",
                 "the matcher was never called, so nothing was tested")
        c.expect(msg["state"] == "done" and len(h.notion.created) == 1,
                 "capture finished with one Notion row",
                 f"matcher failure blocked the capture: {msg['state']}")
        c.expect("Deliverable" not in props, "Deliverable left blank",
                 "a Deliverable relation was written despite the failure")
        reports = h.outbox("capture_report")
        lines = [ln for r in reports for ln in r["text"].splitlines()
                 if "Deliverable match skipped" in ln]
        c.expect(len(lines) == 1, "one 'Deliverable match skipped' line reported",
                 f"{len(lines)} matcher-skipped lines in the reports, want 1")
        c.expect((h.ledger.stage_result(h.key, "match") or {}).get("outcome") == "skipped",
                 "match stage recorded as skipped", "match stage not recorded as skipped")

    return await _scenario(
        config, body, matcher=ScriptedMatcher(fail=True), deliverable_link_enabled=True
    )


async def shadow_mode(config: Config) -> ScenarioRun:
    """With linking disabled the decision is kept but no relation is written."""

    async def body(h: Harness, c: Checks) -> None:
        await h.worker.run_once()
        props = h.notion.created[0]["properties"] if h.notion.created else {}
        stage = h.ledger.stage_result(h.key, "match") or {}
        c.expect(h.matcher.calls == 1, "Jev was consulted once",
                 f"matcher called {h.matcher.calls} times, want 1")
        c.expect("Deliverable" not in props, "no Deliverable relation written",
                 f"shadow mode wrote Deliverable: {props.get('Deliverable')}")
        decision = stage.get("decision") or {}
        top = next((x for x in decision.get("outcomes", []) if x["page_id"] == "d-ftr"), {})
        c.expect(stage.get("outcome") == "hinted" and top.get("p_match") == 0.95
                 and decision.get("link_enabled") is False and not decision.get("linked_page_ids"),
                 "decision and probabilities retained (hinted, p_match 0.95)",
                 f"decision not retained: {stage.get('outcome')}, {top}")
        rows = h.ledger._conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE provider = 'jev'"
        ).fetchone()[0]
        c.expect(rows == 1, "the Jev call is logged", f"{rows} Jev llm_calls rows, want 1")
        c.expect(h.message()["state"] == "done", "capture finished", "capture did not finish")

    first = await _scenario(config, body, matcher=ScriptedMatcher(), deliverable_link_enabled=False)

    async def control(h: Harness, c: Checks) -> None:
        await h.worker.run_once()
        props = h.notion.created[0]["properties"] if h.notion.created else {}
        c.expect(props.get("Deliverable") == {"relation": [{"id": "d-ftr"}]},
                 "control: the same answer links once linking is enabled",
                 f"control run did not link: {props.get('Deliverable')}")

    second = await _scenario(
        config, control, matcher=ScriptedMatcher(), deliverable_link_enabled=True
    )
    if first.result.status != PASS:
        return first
    if second.result.status != PASS:
        return ScenarioRun(second.result, first.ops_posts)
    return ScenarioRun(
        Result(PASS, f"{first.result.detail}; {second.result.detail}"), first.ops_posts
    )


async def dry_run(config: Config) -> ScenarioRun:
    """Dry-run holds at `planned`: one masked plan post, no Notion call, no reaction."""

    async def body(h: Harness, c: Checks) -> None:
        await h.worker.run_once()
        await h.worker.run_once()
        msg = h.message()
        c.expect(msg["state"] == "planned" and not msg["notion_page_id"],
                 "held at planned with no page",
                 f"state {msg['state']}, page {msg['notion_page_id']}")
        c.expect(not h.notion.created and not h.notion.retrieved,
                 "no Notion create or read-back", "dry run called Notion")
        c.expect(not h.telegram.reactions, "no reaction", f"{len(h.telegram.reactions)} reactions")
        posts = h.outbox("dry_run_plan")
        c.expect(len(posts) == 1 and posts[0]["state"] == "sent",
                 "one dry-run plan delivered", f"{len(posts)} dry-run plan posts")

    return await _scenario(config, body, dry_run=True)


SCENARIOS: dict[str, Callable[[Config], Awaitable[ScenarioRun]]] = {
    "readback_mismatch": readback_mismatch,
    "outbox_retry": outbox_retry,
    "heartbeat_format": heartbeat_format,
    "matcher_failure": matcher_failure,
    "shadow_mode": shadow_mode,
    "dry_run_scenario": dry_run,
}
