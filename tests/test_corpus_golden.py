"""Golden planning results over the 77-Job-Order redacted corpus.

Expected values live in ``fixtures/corpus_plan_golden.json`` and were checked by
hand against the #21 deadline table. See ``tests/corpus.py`` for the fixture
posting time and extraction stand-in.
"""

import json
import unittest

from jo_watchdog.core.header import parse_header
from jo_watchdog.core.job_order import plan_job_order
from tests.corpus import FIXTURES, extraction_for, load_corpus, posted_at_for

ALIASES = {"TO": "TOC"}
SHIFTS = [("Assignee", "7:00 PM")]


def load_scenarios() -> list[dict]:
    path = FIXTURES / "corpus_deadline_scenarios.json"
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def load_golden() -> dict:
    return json.loads((FIXTURES / "corpus_plan_golden.json").read_text(encoding="utf-8"))


class CorpusHeaderGoldenTests(unittest.TestCase):
    def test_every_distinct_header_has_a_golden_parse(self):
        golden = load_golden()["headers"]
        headers = {extraction_for(text).header_raw for text in load_corpus()}
        self.assertEqual(headers, set(golden))
        for header_raw, expected in golden.items():
            with self.subTest(header=header_raw):
                header = parse_header(header_raw, ALIASES)
                actual = None if header is None else header.model_dump()
                self.assertEqual(actual, expected)


class CorpusPlanGoldenTests(unittest.TestCase):
    def test_every_message_matches_its_golden_plan(self):
        corpus = load_corpus()
        golden = load_golden()["messages"]
        self.assertEqual(len(corpus), 77)
        self.assertEqual(len(golden), 77)
        for text, expected in zip(corpus, golden, strict=True):
            with self.subTest(index=expected["index"]):
                ext = extraction_for(text)
                plan = plan_job_order(
                    ext,
                    telegram_key=f"-100:{expected['index']}",
                    raw_text=text,
                    posted_at=posted_at_for(text),
                    source_block="Posted by: fixture",
                    department_aliases=ALIASES,
                    assignee_shifts=SHIFTS,
                )
                jo_number = plan.properties.get("JO Number")
                deadline = plan.properties.get("Deadline")
                actual = {
                    "index": expected["index"],
                    "name": plan.name,
                    "jo_number": jo_number["rich_text"][0]["text"]["content"]
                    if jo_number
                    else None,
                    "jo_number_int": plan.jo_number_int,
                    "deadline_raw": ext.deadline_raw,
                    "posted_at": plan.properties["Date"]["date"]["start"],
                    "deadline": deadline["date"]["start"] if deadline else None,
                    "deadline_source": plan.deadline_source,
                    "findings": [f.code for f in plan.findings],
                }
                self.assertEqual(actual, expected)

    def test_shift_rows_fall_back_to_2359_without_a_resolved_assignee(self):
        for text, expected in zip(load_corpus(), load_golden()["messages"], strict=True):
            plan = plan_job_order(
                extraction_for(text),
                telegram_key="k",
                raw_text=text,
                posted_at=posted_at_for(text),
                source_block="",
                department_aliases=ALIASES,
            )
            deadline = plan.properties.get("Deadline")
            with self.subTest(index=expected["index"]):
                if expected["deadline_source"] != "shift":
                    self.assertEqual(plan.deadline_source, expected["deadline_source"])
                    continue
                # Every corpus shift row resolves to 19:00 on its stated/posting day.
                day = expected["deadline"][:10]
                self.assertEqual(deadline["date"]["start"], f"{day}T23:59:00+08:00")
                self.assertEqual(plan.deadline_source, "fallback")
                self.assertIn("shift_fallback", [f.code for f in plan.findings])

    def test_derived_scenarios_match_their_golden_deadlines(self):
        corpus = load_corpus()
        for case in load_scenarios():
            with self.subTest(case=case["name"]):
                text = corpus[case["base"]]
                ext = extraction_for(text)
                ext.deadline_raw = case["deadline_raw"]
                plan = plan_job_order(
                    ext,
                    telegram_key="k",
                    raw_text=text,
                    posted_at=posted_at_for(text),
                    source_block="",
                    department_aliases=ALIASES,
                    assignee_shifts=[tuple(shift) for shift in case["shifts"]],
                )
                deadline = plan.properties.get("Deadline")
                self.assertEqual(
                    {
                        "deadline": deadline["date"]["start"] if deadline else None,
                        "source": plan.deadline_source,
                        "findings": [f.code for f in plan.findings],
                    },
                    {k: case[k] for k in ("deadline", "source", "findings")},
                )

    def test_derived_scenarios_cover_the_required_edge_cases(self):
        names = " | ".join(case["name"] for case in load_scenarios())
        for needed in (
            "overnight",
            "multiple assignees",
            "23:59",
            "hedged",
            "past",
            "future",
            "unparsed",
            "placeholder",
        ):
            self.assertIn(needed, names)

    def test_corpus_covers_every_deadline_family(self):
        sources = {m["deadline_source"] for m in load_golden()["messages"]}
        self.assertEqual(sources, {"stated", "shift", "blank"})
        findings = {c for m in load_golden()["messages"] for c in m["findings"]}
        self.assertEqual(findings, {"asap", "deadline_past", "deadline_unparsed"})


if __name__ == "__main__":
    unittest.main()
