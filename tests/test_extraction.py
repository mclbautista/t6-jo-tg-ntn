"""Tests for the pure classify/extract contract, few-shot split, and report scoring."""

import json
import unittest
from pathlib import Path

from pydantic import ValidationError

from jo_watchdog.app.extraction_report import held_out_cases, render, score
from jo_watchdog.core.extraction import (
    EXTRACTED_FIELDS,
    FEWSHOT_PATH,
    PROMPT_TAG,
    SYSTEM_PROMPT,
    Extraction,
    build_messages,
    build_user_message,
    field_matches,
    forward_origin_lines,
)
from jo_watchdog.core.masker import outbound_text

FIXTURES = Path(__file__).parent / "fixtures"


def load(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def valid(**overrides) -> dict:
    data = {field: None for field in EXTRACTED_FIELDS}
    data.update(verdict="jo", confidence="high", reasons="r", multiple_jo_suspected=False)
    data.update(overrides)
    return data


class SchemaTests(unittest.TestCase):
    def test_valid_payload(self):
        self.assertEqual(Extraction.model_validate(valid()).verdict, "jo")

    def test_unknown_keys_are_a_schema_violation(self):
        # The model may never supply page ids, canonical names, or datetimes.
        for key in ("page_id", "name", "deadline"):
            with self.subTest(key), self.assertRaises(ValidationError):
                Extraction.model_validate(valid(**{key: "x"}))

    def test_missing_keys_are_a_schema_violation(self):
        data = valid()
        del data["task"]
        with self.assertRaises(ValidationError):
            Extraction.model_validate(data)

    def test_bounded_verdict_and_confidence(self):
        with self.assertRaises(ValidationError):
            Extraction.model_validate(valid(verdict="maybe"))
        with self.assertRaises(ValidationError):
            Extraction.model_validate(valid(confidence="0.9"))

    def test_subtitle_is_restricted(self):
        for ok in (None, "None", "Burned-in: English"):
            self.assertEqual(Extraction.model_validate(valid(subtitle=ok)).subtitle, ok)
        for bad in ("English", "Burned-in:", "-"):
            with self.subTest(bad), self.assertRaises(ValidationError):
                Extraction.model_validate(valid(subtitle=bad))

    def test_poster_placeholders_pass_through_but_blank_is_null(self):
        e = Extraction.model_validate(valid(link_settings="N/A", watermark="-", purpose="  "))
        self.assertEqual(e.link_settings, "N/A")
        self.assertEqual(e.watermark, "-")
        self.assertIsNone(e.purpose)

    def test_empty_reasons_rejected(self):
        with self.assertRaises(ValidationError):
            Extraction.model_validate(valid(reasons=" "))


class ModelInputTests(unittest.TestCase):
    def test_plain_message_is_the_masked_text_alone(self):
        self.assertEqual(build_user_message("hello"), "hello")

    def test_forward_origin_lines_cover_each_origin_type(self):
        forwarder = {"first_name": "Lei", "last_name": "Z"}
        cases = {
            "user": ({"type": "user", "sender_user": {"first_name": "Ana"}}, "Ana"),
            "hidden_user": ({"type": "hidden_user", "sender_user_name": "Ben"}, "Ben"),
            "chat": ({"type": "chat", "sender_chat": {"title": "Post Group"}}, "Post Group"),
            "channel": ({"type": "channel", "chat": {"title": "T6 News"}}, "T6 News"),
        }
        for origin_type, (origin, original) in cases.items():
            with self.subTest(origin_type):
                lines = forward_origin_lines({"from": forwarder, "forward_origin": origin})
                self.assertEqual(
                    lines,
                    [
                        f"Origin type: {origin_type}",
                        "Forwarded by: Lei Z",
                        f"Originally from: {original}",
                    ],
                )

    def test_non_forward_has_no_origin_block(self):
        self.assertIsNone(forward_origin_lines({"text": "x", "date": 1}))

    def test_origin_block_carries_no_date(self):
        lines = forward_origin_lines(
            {"forward_origin": {"type": "user", "date": 1790000000, "sender_user": {}}}
        )
        self.assertFalse(any("1790000000" in line for line in lines))

    def test_prompt_has_no_roster_date_or_reply_chain(self):
        messages = build_messages("Task: x")
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1], {"role": "user", "content": "Task: x"})
        self.assertNotIn("today", SYSTEM_PROMPT.lower())
        self.assertNotIn("roster", SYSTEM_PROMPT.lower())


class FewShotTests(unittest.TestCase):
    def setUp(self):
        self.fewshot = json.loads(FEWSHOT_PATH.read_text(encoding="utf-8"))
        self.ids = load("fewshot_ids.json")
        self.golden = load("extraction_golden.json")
        self.corpus = load("redacted_corpus.json")

    def test_prompt_tag_matches(self):
        self.assertEqual(self.fewshot["prompt_tag"], PROMPT_TAG)
        self.assertEqual(self.ids["prompt_tag"], PROMPT_TAG)

    def test_fewshot_list_matches_committed_ids(self):
        corpus_ids = [e["id"] for e in self.fewshot["examples"] if e["id"].startswith("corpus-")]
        self.assertEqual(corpus_ids, [f"corpus-{i}" for i in self.ids["corpus_indices"]])
        self.assertTrue(12 <= len(corpus_ids) <= 18)  # roughly 15

    def test_fewshot_outputs_validate_and_inputs_are_masked_corpus_text(self):
        expected = {c["corpus_index"]: c["expected"] for c in self.golden["cases"]}
        for example in self.fewshot["examples"]:
            with self.subTest(example["id"]):
                Extraction.model_validate(example["output"])
                self.assertEqual(outbound_text(example["input"]).text, example["input"])
                if example["id"].startswith("corpus-"):
                    index = int(example["id"].split("-")[1])
                    self.assertEqual(example["input"], outbound_text(self.corpus[index]).text)
                    output = {f: example["output"][f] for f in EXTRACTED_FIELDS}
                    self.assertEqual(output, expected[index])

    def test_golden_covers_the_whole_corpus(self):
        indices = [c["corpus_index"] for c in self.golden["cases"]]
        self.assertEqual(indices, list(range(len(self.corpus))))
        for case in self.golden["cases"]:
            self.assertEqual(set(case["expected"]), set(EXTRACTED_FIELDS))

    def test_held_out_set_is_disjoint_from_fewshot(self):
        held_out = held_out_cases(FIXTURES)
        held_ids = {c["corpus_index"] for c in held_out}
        self.assertFalse(held_ids & set(self.ids["corpus_indices"]))
        self.assertTrue(55 <= len(held_out) <= 65)  # roughly 60
        # A repost of a few-shot message would leak it into evaluation.
        fewshot_texts = {self.corpus[i] for i in self.ids["corpus_indices"]}
        self.assertFalse(any(self.corpus[i] in fewshot_texts for i in held_ids))


class ScoringTests(unittest.TestCase):
    def test_exact_match_after_whitespace_normalisation(self):
        matches = field_matches({"task": "a  b\n c"}, {"task": " a b c "})
        self.assertTrue(matches["task"])
        self.assertTrue(matches["title"])  # both null

    def test_score_counts_failed_calls_as_misses(self):
        cases = [
            {"corpus_index": 1, "expected": {"task": "x"}},
            {"corpus_index": 2, "expected": {"task": "y"}},
        ]
        rates = score(cases, {1: {"task": "x"}, 2: None})
        self.assertEqual(rates["task"], 0.5)
        report = render(rates, 2, 1, 1)
        self.assertIn("task", report)
        self.assertIn("no pass bar", report)


if __name__ == "__main__":
    unittest.main()
