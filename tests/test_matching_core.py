"""Pure decision-rule tests for Deliverable matching (#26)."""

import unittest

from jo_watchdog.core.matching import (
    Candidate,
    CandidateDecision,
    InvalidMatchResult,
    MatchResult,
    decide,
    hint_line,
    normalize_name,
    validate_result,
)


def result(*decisions: tuple[str, float, float, float]) -> MatchResult:
    return MatchResult(
        decisions=[
            CandidateDecision(page_id=p, p_match=m, p_maybe=y, p_no=n) for p, m, y, n in decisions
        ],
        requested_model="~typesafe/jev-latest",
        returned_model="typesafe/jev-1.14-20261014",
        input_tokens=1,
        output_tokens=1,
    )


def cands(*names: str) -> list[Candidate]:
    return [Candidate(page_id=f"p{i}", name=n) for i, n in enumerate(names)]


def run(candidates, res, *, t_link=0.90, t_hint=0.50, link_enabled=True):
    return decide(candidates, res, t_link=t_link, t_hint=t_hint, link_enabled=link_enabled)


class ThresholdTests(unittest.TestCase):
    def test_link_at_exactly_t_link(self):
        d = run(cands("A"), result(("p0", 0.90, 0.05, 0.05)))
        self.assertEqual(d.outcomes[0].verdict, "link")
        self.assertEqual(d.linked_page_ids, ["p0"])
        self.assertEqual(d.outcome, "linked")

    def test_just_below_t_link_with_maybe_is_hint(self):
        d = run(cands("A"), result(("p0", 0.89, 0.06, 0.05)))
        self.assertEqual(d.outcomes[0].verdict, "hint")
        self.assertEqual(d.linked_page_ids, [])
        self.assertEqual(d.outcome, "hinted")

    def test_match_must_be_top_choice_to_link(self):
        d = run(cands("A"), result(("p0", 0.45, 0.50, 0.05)), t_link=0.40)
        self.assertEqual(d.outcomes[0].verdict, "hint")

    def test_hint_at_exactly_t_hint(self):
        d = run(cands("A"), result(("p0", 0.20, 0.30, 0.50)))
        self.assertEqual(d.outcomes[0].verdict, "hint")

    def test_below_t_hint_is_silent(self):
        d = run(cands("A"), result(("p0", 0.20, 0.29, 0.51)))
        self.assertEqual(d.outcomes[0].verdict, "none")
        self.assertEqual(d.hints, [])
        self.assertEqual(d.outcome, "none")

    def test_thresholds_are_configurable(self):
        res = result(("p0", 0.80, 0.15, 0.05))
        self.assertEqual(run(cands("A"), res).outcomes[0].verdict, "hint")
        self.assertEqual(run(cands("A"), res, t_link=0.75).outcomes[0].verdict, "link")


class ManyCandidateTests(unittest.TestCase):
    def test_zero_one_or_many_qualify_independently(self):
        d = run(
            cands("FTR", "Promo", "DCP"),
            result(("p0", 0.95, 0.04, 0.01), ("p1", 0.91, 0.08, 0.01), ("p2", 0.0, 0.1, 0.9)),
        )
        self.assertEqual([o.verdict for o in d.outcomes], ["link", "link", "none"])
        self.assertEqual(d.linked_page_ids, ["p0", "p1"])

    def test_none_qualify(self):
        d = run(cands("FTR", "DCP"), result(("p0", 0.0, 0.1, 0.9), ("p1", 0.05, 0.05, 0.9)))
        self.assertEqual(d.outcome, "none")
        self.assertEqual(d.linked_page_ids, [])


class DuplicateNameTests(unittest.TestCase):
    def test_normalized_duplicates_never_link(self):
        d = run(
            cands("Moonglow  FTR SCR", "moonglow ftr scr"),
            result(("p0", 0.97, 0.02, 0.01), ("p1", 0.10, 0.20, 0.70)),
        )
        self.assertEqual(d.outcomes[0].verdict, "hint")
        self.assertTrue(d.outcomes[0].duplicate_name)
        self.assertEqual(d.outcomes[1].verdict, "none")
        self.assertEqual(d.linked_page_ids, [])
        self.assertEqual(
            hint_line(d.hints[0]),
            "Possible Deliverable: Moonglow  FTR SCR (match probability 0.97)",
        )

    def test_normalize_name(self):
        self.assertEqual(normalize_name("  Mag-Iina\tFTR   SCR "), "mag-iina ftr scr")


class ShadowModeTests(unittest.TestCase):
    def test_shadow_mode_writes_nothing_and_reports_would_be_links_as_hints(self):
        d = run(
            cands("A", "B"),
            result(("p0", 0.95, 0.04, 0.01), ("p1", 0.3, 0.3, 0.4)),
            link_enabled=False,
        )
        self.assertEqual(d.outcomes[0].verdict, "link")  # the decision is retained
        self.assertEqual(d.linked_page_ids, [])
        self.assertEqual([h.page_id for h in d.hints], ["p0", "p1"])
        self.assertEqual(d.outcome, "hinted")
        self.assertFalse(d.link_enabled)


class ValidationTests(unittest.TestCase):
    def test_missing_candidate_fails(self):
        with self.assertRaises(InvalidMatchResult):
            validate_result(cands("A", "B"), result(("p0", 1.0, 0.0, 0.0)))

    def test_unknown_key_fails(self):
        with self.assertRaises(InvalidMatchResult):
            validate_result(cands("A"), result(("p0", 1, 0, 0), ("zz", 1, 0, 0)))

    def test_duplicate_decision_fails(self):
        with self.assertRaises(InvalidMatchResult):
            validate_result(cands("A"), result(("p0", 1, 0, 0), ("p0", 1, 0, 0)))

    def test_sum_tolerance(self):
        validate_result(cands("A"), result(("p0", 0.5, 0.3, 0.21)))  # 1.01 is allowed
        with self.assertRaises(InvalidMatchResult):
            validate_result(cands("A"), result(("p0", 0.5, 0.3, 0.22)))

    def test_decide_rejects_invalid_results(self):
        with self.assertRaises(InvalidMatchResult):
            run(cands("A"), result(("p0", 0.9, 0.9, 0.9)))


class OutputShapeTests(unittest.TestCase):
    def test_hint_line_has_probability_and_no_reason_or_tag(self):
        d = run(cands("Mainpic DCP"), result(("p0", 0.62, 0.20, 0.18)))
        line = hint_line(d.hints[0])
        self.assertEqual(line, "Possible Deliverable: Mainpic DCP (match probability 0.62)")
        self.assertNotIn("@", line)
        self.assertNotIn("Reason", line)


if __name__ == "__main__":
    unittest.main()
