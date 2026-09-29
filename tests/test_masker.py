"""External contract tests for deterministic credential masking.

Fixture sources are the live GitHub issues #19/#28 and the read-only design
files in ``/Users/cyrilbautista/Projects/t6-jo-tg-ntn/map``. The 77-message
corpus is copied byte-for-byte from ``map/tickets/sample-corpus-redacted.md``;
only its Markdown fences are removed by the fixture extraction.
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

from jo_watchdog.core import masker

FIXTURES = Path(__file__).parent / "fixtures"
WITHHELD = "[password withheld – see Telegram message]"


def load_json(name: str):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def mask_result(text: str) -> tuple[str, int]:
    """Call the required count-bearing public contract."""
    result = getattr(masker, "mask_result", None)
    if result is None:
        # Keep the rest of the external-output contract observable during RED;
        # dedicated count tests still fail until production exposes this API.
        return masker.mask_text(text), 0
    return result(text)


class CredentialAssignmentTests(unittest.TestCase):
    def test_fixture_assignments_retain_label_and_mask_value(self):
        cases = load_json("masker_cases.json")["positive"]
        for source, expected, expected_count in cases:
            with self.subTest(source=source):
                self.assertEqual(masker.mask_text(source), expected)
                self.assertEqual(mask_result(source), (expected, expected_count))

    def test_separator_must_immediately_follow_supported_label(self):
        for source in [
            "pw : secret",
            "pass = secret",
            "password : secret",
            "passcode = secret",
            "PIN : 1234",
        ]:
            with self.subTest(source=source):
                self.assertEqual(masker.mask_text(source), source)
                self.assertEqual(mask_result(source), (source, 0))

    def test_separator_at_end_masks_next_non_blank_line(self):
        source = "PIN=\n \n\t\n1234\nDeadline: today"
        expected = f"PIN=\n \n\t\n{WITHHELD}\nDeadline: today"
        self.assertEqual(mask_result(source), (expected, 1))

    def test_separator_without_a_value_changes_nothing(self):
        for source in ["PW:", "password=\n", "pass:\n\n"]:
            with self.subTest(source=source):
                self.assertEqual(mask_result(source), (source, 0))

    def test_count_includes_each_withheld_assignment(self):
        source = "PW: alpha\nplain\npass=beta\nPIN:\n\n1234"
        expected = f"PW: {WITHHELD}\nplain\npass={WITHHELD}\nPIN:\n\n{WITHHELD}"
        self.assertEqual(mask_result(source), (expected, 3))

    def test_every_assignment_on_one_line_is_masked_and_counted(self):
        source = "PW: first pass: second"
        expected = f"PW: {WITHHELD} pass: {WITHHELD}"
        self.assertEqual(mask_result(source), (expected, 2))


class RequiredNotToMaskTests(unittest.TestCase):
    def test_every_negative_fixture_is_byte_for_byte_unchanged(self):
        for source in load_json("masker_cases.json")["negative"]:
            with self.subTest(source=source):
                self.assertEqual(masker.mask_text(source), source)
                self.assertEqual(mask_result(source), (source, 0))

    def test_url_on_next_non_blank_line_is_not_consumed_as_a_value(self):
        for source in [
            "PW:\nhttps://example.com/?password=secret",
            "PW:\n  https://example.com/?password=secret  ",
            "pass=\n\nhttps://example.com/pw=secret&pin=1234",
            "PIN:\nwww.dropbox.com/s/abc?dl=0&rlkey=xyz",
            "Link: https://example.com/file#PW:secret",
        ]:
            with self.subTest(source=source):
                self.assertEqual(mask_result(source), (source, 0))

    def test_next_line_value_masks_non_url_text_but_preserves_url(self):
        source = "PW:\nsecret https://example.com/?password=link-key trailing"
        expected = f"PW:\n{WITHHELD} https://example.com/?password=link-key "
        self.assertEqual(mask_result(source), (expected, 1))
        self.assertEqual(mask_result(expected), (expected, 0))

    def test_url_inline_with_a_label_is_unchanged_and_not_dangling(self):
        source = "PW: https://example.com/?password=secret\nnot-a-password"
        self.assertEqual(mask_result(source), (source, 0))

    def test_url_is_preserved_while_assignment_outside_it_is_masked(self):
        source = "https://example.com/?password=secret PW: actual-secret"
        expected = f"https://example.com/?password=secret PW: {WITHHELD}"
        self.assertEqual(mask_result(source), (expected, 1))

    def test_inline_value_masks_around_preserved_url(self):
        cases = [
            (
                "PW: https://files.example/download secret-after-url",
                f"PW: https://files.example/download {WITHHELD}",
                1,
            ),
            (
                "PW: secret-before-url https://files.example/download trailing-secret",
                f"PW: {WITHHELD} https://files.example/download ",
                1,
            ),
            (
                "PW: first https://files.example/one trailing pass: "
                "https://files.example/two second",
                f"PW: {WITHHELD} https://files.example/one  pass: "
                f"https://files.example/two {WITHHELD}",
                2,
            ),
        ]
        for source, expected, count in cases:
            with self.subTest(source=source):
                self.assertEqual(mask_result(source), (expected, count))
                self.assertEqual(mask_result(expected), (expected, 0))


class DeterminismAndMetadataTests(unittest.TestCase):
    def test_reapplying_masker_is_idempotent_and_adds_no_count(self):
        first_text, first_count = mask_result("PW: secret")
        second_text, second_count = mask_result(first_text)
        self.assertEqual(first_text, f"PW: {WITHHELD}")
        self.assertEqual(first_count, 1)
        self.assertEqual(second_text, first_text)
        self.assertEqual(second_count, 0)

    def test_next_line_masking_preserves_indentation_and_is_idempotent(self):
        source = "PW:\n\n  secret"
        expected = f"PW:\n\n  {WITHHELD}"
        self.assertEqual(mask_result(source), (expected, 1))
        self.assertEqual(mask_result(expected), (expected, 0))

    def test_mask_text_and_mask_result_agree(self):
        source = "password=secret\nDL w pw, 14 days expiry"
        masked, _ = mask_result(source)
        self.assertEqual(masker.mask_text(source), masked)

    def test_version_metadata_is_stable_v1(self):
        self.assertEqual(getattr(masker, "MASKER_VERSION", None), "1.0.0")

    def test_fixture_replacement_is_the_public_replacement(self):
        self.assertEqual(load_json("masker_cases.json")["replacement"], WITHHELD)


class RedactedCorpusTests(unittest.TestCase):
    def test_fixture_contains_all_77_job_orders(self):
        corpus = load_json("redacted_corpus.json")
        self.assertEqual(len(corpus), 77)
        self.assertTrue(all(isinstance(job, str) and job for job in corpus))

    def test_known_pw_example_is_the_only_corpus_redaction(self):
        corpus = load_json("redacted_corpus.json")
        known = [job for job in corpus if "PP EP 2 (APC) - TOC - #0004" in job]
        self.assertEqual(len(known), 1)
        expected = known[0].replace("PW: [REDACTED]", f"PW: {WITHHELD}")
        self.assertEqual(mask_result(known[0]), (expected, 1))
        self.assertIn("Link: [LINK]", expected)

    def test_all_other_corpus_jobs_remain_byte_for_byte_unchanged(self):
        corpus = load_json("redacted_corpus.json")
        for job in corpus:
            if "PP EP 2 (APC) - TOC - #0004" in job:
                continue
            with self.subTest(first_line=job.splitlines()[0]):
                self.assertEqual(mask_result(job), (job, 0))

    def test_whole_corpus_masking_is_repeatable(self):
        for job in load_json("redacted_corpus.json"):
            first, _ = mask_result(job)
            second, second_count = mask_result(first)
            self.assertEqual(second, first)
            self.assertEqual(second_count, 0)


if __name__ == "__main__":
    unittest.main()
