"""The shadow comparison against t6-core's lookups (#136, #137)."""

import unittest

from t6.domain import Contact, Project

from jo_watchdog.app.shadow import compare_people, compare_project
from jo_watchdog.core.resolve import (
    MentionResult,
    PeopleResolution,
    ProjectResolution,
)


def project(page_id, code, rotation=None):
    return Project(page_id=page_id, name=f"Show {page_id}", project_code=code, rotation=rotation)


def contact(page_id, name, handle=None, user_id=None, aliases=()):
    return Contact(
        page_id=page_id, name=name, telegram_handle=handle, telegram_user_id=user_id, aliases=aliases
    )


class ProjectShadowTests(unittest.TestCase):
    def test_a_reused_code_resolves_to_the_project_in_rotation(self):
        projects = [project("old", "ABC", "out"), project("new", "ABC", "in")]
        # The casefold-only lookup leaves a shared code unresolved.
        differences = compare_project("ABC", ProjectResolution(), projects)
        self.assertEqual([(d.kind, d.legacy, d.core) for d in differences], [("project", "unresolved", "resolved")])

    def test_agreement_is_not_a_difference(self):
        projects = [project("only", "XYZ", "in")]
        legacy = ProjectResolution(page_id="only", code="XYZ")
        self.assertEqual(compare_project("XYZ", legacy, projects), [])

    def test_a_different_project_is_reported(self):
        projects = [project("old", "ABC", "out"), project("new", "ABC", "in")]
        legacy = ProjectResolution(page_id="old", code="ABC")
        differences = compare_project("ABC", legacy, projects)
        self.assertEqual([d.core for d in differences], ["resolved_other"])

    def test_a_blank_project_is_not_compared(self):
        self.assertEqual(compare_project("  ", ProjectResolution(), [project("a", "ABC")]), [])

    def test_no_difference_line_carries_a_name_or_id(self):
        projects = [project("old", "ABC", "out"), project("new", "ABC", "in")]
        (difference,) = compare_project("ABC", ProjectResolution(), projects)
        line = difference.line()
        for text in ("ABC", "old", "new", "Show"):
            self.assertNotIn(text, line)


class PeopleShadowTests(unittest.TestCase):
    def people(self, *mentions):
        return PeopleResolution(mentions=list(mentions))

    def test_agreement_is_not_a_difference(self):
        contacts = [contact("c1", "Priya Raman", handle="priya")]
        part = MentionResult(raw="@priya", role="assignee", status="resolved", method="handle", page_id="c1")
        self.assertEqual(compare_people(self.people(part), [], contacts), [])

    def test_core_resolving_what_the_app_left_unresolved_is_reported(self):
        contacts = [contact("c1", "Priya Raman", aliases=("pri",))]
        part = MentionResult(raw="pri", role="assignee", status="unresolved")
        differences = compare_people(self.people(part), [], contacts)
        self.assertEqual([(d.kind, d.legacy, d.core) for d in differences], [("person", "unresolved", "resolved")])

    def test_hedged_mentions_are_skipped(self):
        contacts = [contact("c1", "Priya Raman", aliases=("pri",))]
        part = MentionResult(raw="if pri", role="assignee", status="ambiguous")
        self.assertEqual(compare_people(self.people(part), [], contacts), [])


if __name__ == "__main__":
    unittest.main()
