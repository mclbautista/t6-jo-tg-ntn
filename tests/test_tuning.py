"""Offline tests for corpus threshold scoring and the retained results (#61)."""

import json
import tempfile
import unittest
from pathlib import Path

import yaml

from jo_watchdog.app.config import Config
from jo_watchdog.app.tuning import (
    FuzzyCase,
    MatchSample,
    assignee_mentions,
    build_report,
    fuzzy_cases,
    fuzzy_outcome,
    match_samples,
    read_report,
    sweep_fuzzy,
    sweep_match,
    write_report,
)
from jo_watchdog.core.resolve import (
    PeopleIndex,
    Person,
    ResolveSettings,
    fuzzy_ranking,
    resolve_mention,
)
from tests.recorded import recorded_people

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
RETAINED = ROOT / "docs" / "tuning" / "threshold-results.json"
CORPUS = json.loads((FIXTURES / "redacted_corpus.json").read_text(encoding="utf-8"))
PAIRS = json.loads((FIXTURES / "jev" / "regression_pairs.json").read_text(encoding="utf-8"))
DEFAULTS = Config.model_fields


def people() -> list[Person]:
    return recorded_people()


def current() -> dict:
    return {
        "fuzzy_min_score": DEFAULTS["fuzzy_min_score"].default,
        "fuzzy_min_lead": DEFAULTS["fuzzy_min_lead"].default,
        "t_link": DEFAULTS["t_link"].default,
        "t_hint": DEFAULTS["t_hint"].default,
    }


def report(**overrides) -> dict:
    cases = fuzzy_cases(assignee_mentions(CORPUS), people(), ResolveSettings())
    args = dict(
        corpus_size=len(CORPUS),
        cases=cases,
        samples=match_samples(PAIRS),
        current=current(),
        sample_source="test",
    )
    return build_report(**{**args, **overrides})


class CorpusScoringTests(unittest.TestCase):
    def test_assignee_mentions_cover_the_corpus(self):
        mentions = assignee_mentions(CORPUS)
        self.assertGreater(len(mentions), len(CORPUS) // 2)
        self.assertIn("Paul", mentions)

    def test_only_mentions_that_reach_fuzzy_matching_are_scored(self):
        cases = fuzzy_cases(assignee_mentions(CORPUS), people(), ResolveSettings())
        names = {c.mention for c in cases}
        self.assertIn("Paul", names)
        self.assertNotIn("@priya", names)  # resolved by handle, never fuzzy

    def test_scores_match_the_production_resolver(self):
        settings = ResolveSettings()
        index = PeopleIndex(people())
        for case in fuzzy_cases(assignee_mentions(CORPUS), people(), settings):
            got = resolve_mention(case.mention, "assignee", index, settings)
            held = fuzzy_outcome(case, settings.fuzzy_min_score, settings.fuzzy_min_lead)
            self.assertEqual(got.status == "resolved", held == "resolved", case.mention)

    def test_ranking_is_best_first(self):
        ranked = fuzzy_ranking("paul", PeopleIndex(people()))
        self.assertEqual([s for _, s in ranked], sorted((s for _, s in ranked), reverse=True))

    def test_two_name_mentions_are_never_resolved_at_the_default_thresholds(self):
        cases = [c for c in fuzzy_cases(assignee_mentions(CORPUS), people(), ResolveSettings())
                 if c.mention in ("Lena Mira", "Mira Lena")]  # fmt: skip
        self.assertTrue(cases)
        for case in cases:
            self.assertNotEqual(fuzzy_outcome(case, 90, 8), "resolved")

    def test_close_runner_up_is_held_as_ambiguous(self):
        case = FuzzyCase("x", "A", 95.0, "B", 90.0)
        self.assertEqual(fuzzy_outcome(case, 90, 8), "ambiguous")
        self.assertEqual(fuzzy_outcome(case, 96, 0), "below_score")
        self.assertEqual(fuzzy_outcome(case, 90, 5), "resolved")

    def test_sweep_resolves_less_as_thresholds_tighten(self):
        cases = fuzzy_cases(assignee_mentions(CORPUS), people(), ResolveSettings())
        rows = {(r["fuzzy_min_score"], r["fuzzy_min_lead"]): r for r in sweep_fuzzy(cases)}
        self.assertGreaterEqual(rows[(80.0, 0.0)]["resolved"], rows[(90.0, 8.0)]["resolved"])
        self.assertGreaterEqual(rows[(90.0, 8.0)]["resolved"], rows[(98.0, 16.0)]["resolved"])
        for row in rows.values():
            self.assertEqual(row["resolved"] + row["below_score"] + row["ambiguous"], len(cases))


class MatchSweepTests(unittest.TestCase):
    def test_recorded_pairs_give_samples_with_expected_verdicts(self):
        samples = match_samples(PAIRS)
        self.assertEqual([s.expected for s in samples], ["hint", "link", "link"])

    def test_default_thresholds_make_no_false_links_on_the_recorded_pairs(self):
        rows = {(r["t_link"], r["t_hint"]): r for r in sweep_match(match_samples(PAIRS))}
        self.assertEqual(rows[(0.90, 0.50)]["false_links"], 0)
        self.assertEqual(rows[(0.90, 0.50)]["missed_links"], 0)

    def test_a_loose_link_threshold_shows_its_false_link(self):
        sample = MatchSample("maybe-case", 0.75, 0.2, 0.05, expected="hint")
        rows = {(r["t_link"], r["t_hint"]): r for r in sweep_match([sample])}
        self.assertEqual(rows[(0.70, 0.50)]["false_links"], 1)
        self.assertEqual(rows[(0.90, 0.50)]["false_links"], 0)


class RetainedResultsTests(unittest.TestCase):
    def test_report_never_enables_linking_or_changes_thresholds(self):
        r = report()
        self.assertFalse(r["link_enabled"])
        self.assertEqual(r["adopted"], current())
        self.assertFalse(DEFAULTS["deliverable_link_enabled"].default)

    def test_looser_fuzzy_points_that_resolve_more_are_refused(self):
        r = report()
        for row in r["results"]["refused_loosening"]:
            self.assertLessEqual(row["fuzzy_min_score"], current()["fuzzy_min_score"])
            self.assertLessEqual(row["fuzzy_min_lead"], current()["fuzzy_min_lead"])
        # A synthetic case that a looser lead would newly resolve is refused, not adopted.
        loose = [FuzzyCase("x", "A", 92.0, "B", 88.0)]
        r = report(cases=loose)
        self.assertTrue(r["results"]["refused_loosening"])
        self.assertEqual(r["adopted"]["fuzzy_min_lead"], current()["fuzzy_min_lead"])

    def test_new_results_start_with_a_pending_owner_review(self):
        self.assertEqual(report()["owner_review"]["status"], "pending")

    def test_owner_review_survives_regeneration_only_for_identical_results(self):
        first = report()
        first["owner_review"] = {
            "status": "reviewed", "reviewed_by": "Cyril", "reviewed_at": "2026-10-25T00:00:00Z",
            "evidence": "kept defaults",
        }  # fmt: skip
        self.assertEqual(report(previous=first)["owner_review"]["status"], "reviewed")
        changed = report(previous=first, cases=[FuzzyCase("x", "A", 92.0, "B", 88.0)])
        self.assertEqual(changed["owner_review"]["status"], "pending")

    def test_round_trip_through_the_results_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "results.json"
            r = report()
            write_report(path, r)
            self.assertEqual(read_report(path), r)
            self.assertIsNone(read_report(Path(tmp) / "absent.json"))

    def test_retained_results_are_current_and_match_config_defaults(self):
        retained = read_report(RETAINED)
        self.assertIsNotNone(retained, f"{RETAINED} missing; run smoke.py --only threshold_tuning")
        self.assertEqual(retained["adopted"], current())
        self.assertFalse(retained["link_enabled"])
        regenerated = report(
            previous=retained, sample_source=retained["results"]["match_sample_source"]
        )
        self.assertEqual(retained["results_digest"], regenerated["results_digest"])

    def test_example_config_carries_the_adopted_thresholds(self):
        example = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
        self.assertEqual({k: example[k] for k in current()}, current())
        self.assertFalse(example.get("deliverable_link_enabled", False))
