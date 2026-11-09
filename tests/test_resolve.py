"""People, Project and Department resolution over the recorded Notion fixtures and corpus."""

import json
import re
import unittest
from datetime import time
from pathlib import Path

from jo_watchdog.app.caches import department_options, person_from_page, project_from_page
from jo_watchdog.core.resolve import (
    MentionEntity,
    PeopleIndex,
    Person,
    ResolveSettings,
    normalize_name,
    parse_end_of_shift,
    relation_properties,
    resolve_department,
    resolve_mention,
    resolve_people,
    resolve_project,
    split_aliases,
    split_mentions,
)

FIXTURES = Path(__file__).parent / "fixtures"
NOTION = FIXTURES / "notion"

PRIYA = "c0000000-0000-4000-8000-000000000001"
RAMON = "c0000000-0000-4000-8000-000000000002"
LENA = "c0000000-0000-4000-8000-000000000003"
MIRA = "c0000000-0000-4000-8000-000000000004"
PAUL = "c0000000-0000-4000-8000-000000000005"
NINA = "c0000000-0000-4000-8000-000000000006"
JOSHUA = "c0000000-0000-4000-8000-000000000007"
RAE = "c0000000-0000-4000-8000-000000000008"
JASON = "c0000000-0000-4000-8000-000000000009"
LARA = "c0000000-0000-4000-8000-000000000013"


def _load(name):
    return json.loads((NOTION / name).read_text(encoding="utf-8"))


def people():
    pages = (
        _load("contacts_query_page1.json")["results"]
        + _load("contacts_query_page2.json")["results"]
    )
    return [person_from_page(p) for p in pages]


def projects():
    return [project_from_page(p) for p in _load("projects_query.json")["results"]]


DEPARTMENTS = department_options(_load("job_orders_data_source.json"))
SETTINGS = ResolveSettings(department_aliases={"TO": "TOC"}, standing_cc_contact_page_id=PRIYA)


def resolve(mention, entities=()):
    return resolve_mention(mention, "assignee", PeopleIndex(people()), SETTINGS, entities)


class FixtureParsingTests(unittest.TestCase):
    def test_contacts_fields_read_by_live_property_names(self):
        by_id = {p.page_id: p for p in people()}
        self.assertEqual(by_id[NINA].telegram_handle, "@user_three")
        self.assertEqual(by_id[NINA].aliases, "Nina, sam, KUYA sam")
        self.assertEqual(by_id[RAMON].end_of_shift, "10:00 PM")
        self.assertEqual(by_id[MIRA].telegram_user_id, "5550001")
        self.assertEqual(by_id[PRIYA].end_of_shift, "")

    def test_projects_and_departments(self):
        codes = {p.code for p in projects()}
        self.assertIn("COLD CUT", codes)
        self.assertIn("", codes)  # a project with no code is cached but never matches
        self.assertEqual(DEPARTMENTS, ["TOC", "AE", "OE", "COOR", "COLOR", "VFX", "SOUND", "ADMIN"])


class NormalizationTests(unittest.TestCase):
    def test_aliases_split_trim_casefold_and_drop_honorifics(self):
        self.assertEqual(split_aliases("Nina,  sam , KUYA sam,,"), ["nina", "sam", "sam"])

    def test_honorifics_dropped_on_mention_side(self):
        for raw in ("ate Lena", "Ate LENA", "sir lena", "kuya  Lena"):
            self.assertEqual(normalize_name(raw), "lena")

    def test_end_of_shift_parsing(self):
        self.assertEqual(parse_end_of_shift("10:00 PM"), time(22, 0))
        self.assertEqual(parse_end_of_shift("12:00 AM"), time(0, 0))
        self.assertEqual(parse_end_of_shift("12:00 PM"), time(12, 0))
        self.assertEqual(parse_end_of_shift("6:00 AM"), time(6, 0))
        self.assertIsNone(parse_end_of_shift("Flexible / Variable"))
        self.assertIsNone(parse_end_of_shift(""))
        self.assertIsNone(parse_end_of_shift(None))


class MentionSplittingTests(unittest.TestCase):
    CASES = {
        "Paul / Ramon": ["Paul", "Ramon"],
        "Paul, Ramon / Mira, Lena": ["Paul", "Ramon", "Mira", "Lena"],
        "Ramon & @user_two": ["Ramon", "@user_two"],
        "Paul and Ramon": ["Paul", "Ramon"],
        "Paul\nRamon": ["Paul", "Ramon"],
        "for @user_one  thank youuu": ["@user_one"],
        "@user_eight thankies!!": ["@user_eight"],
        "@user_two tyvmmmm": ["@user_two"],
        "ate Nina thank u!": ["ate Nina"],
        "Lena po": ["Lena"],
        "Mira / @user_two kung sakaling umabot kay user_two": [
            "Mira",
            "@user_two kung sakaling umabot kay user_two",
        ],
        "ate Lena / Ramon, if abutin the next day": [
            "ate Lena",
            "Ramon if abutin the next day",
        ],
        "Ramon @user_two // @user_three": ["Ramon", "@user_two", "@user_three"],
        "@user_three  @user_one": ["@user_three", "@user_one"],
        "ate @user_four / Ramon": ["ate @user_four", "Ramon"],
        "Ramon but maybe Lena": ["Ramon but maybe Lena"],
        "": [],
    }

    def test_corpus_shapes(self):
        for raw, expected in self.CASES.items():
            with self.subTest(raw=raw):
                self.assertEqual(split_mentions(raw), expected)


class PeopleStepTests(unittest.TestCase):
    def test_step1_telegram_user_id_from_text_mention_entity(self):
        entity = MentionEntity(text="Jadey", user_id="5550001")
        result = resolve("Jadey", [entity])
        self.assertEqual(
            (result.status, result.method, result.page_id), ("resolved", "user_id", MIRA)
        )

    def test_step2_handle_case_insensitive_with_or_without_at(self):
        for raw, page_id in (("@user_three", NINA), ("@USER_ONE", JOSHUA), ("@user_five", JASON)):
            with self.subTest(raw=raw):
                result = resolve(raw)
                self.assertEqual((result.method, result.page_id), ("handle", page_id))

    def test_entity_for_jo_cannot_supply_identity_for_majo(self):
        crowd = [
            Person(page_id="p-jo", name="Jo Lim", telegram_user_id="123"),
            Person(page_id="p-lena", name="Lena Cruz", aliases="Lena"),
        ]
        entity = MentionEntity(text="Jo", user_id="123")
        result = resolve_mention("Lena", "assignee", PeopleIndex(crowd), SETTINGS, [entity])
        self.assertEqual((result.method, result.page_id), ("alias", "p-lena"))
        own = resolve_mention("Jo", "assignee", PeopleIndex(crowd), SETTINGS, [entity])
        self.assertEqual((own.method, own.page_id), ("user_id", "p-jo"))

    def test_handle_entity_binds_only_a_whole_handle_token(self):
        crowd = [
            Person(page_id="p-rae", name="Rae", telegram_handle="@rae"),
            Person(page_id="p-raed", name="Raed", telegram_handle="@raed"),
        ]
        index = PeopleIndex(crowd)
        entity = MentionEntity(text="@rae", handle="rae")
        result = resolve_mention("@raed", "assignee", index, SETTINGS, [entity])
        self.assertEqual(result.page_id, "p-raed")
        spaced = resolve_mention("ate @rae", "assignee", index, SETTINGS, [entity])
        self.assertEqual(spaced.page_id, "p-rae")

    def test_step2_handle_from_mention_entity(self):
        entity = MentionEntity(text="@user_two", handle="user_two")
        result = resolve("@user_two", [entity])
        self.assertEqual((result.method, result.page_id), ("handle", RAE))

    def test_honorific_before_handle(self):
        self.assertEqual(resolve("ate @user_six").page_id, LARA)

    def test_step3_alias_with_honorifics_on_both_sides(self):
        for raw in ("Lena", "ate Lena", "sam", "kuya sam", "sir Nina"):
            with self.subTest(raw=raw):
                result = resolve(raw)
                self.assertEqual(result.method, "alias")
        self.assertEqual(resolve("ate Lena").page_id, LENA)

    def test_alias_collision_stays_ambiguous(self):
        result = resolve("Kim")
        self.assertEqual(result.status, "ambiguous")
        self.assertIsNone(result.page_id)

    def test_handle_guard_never_fuzzy(self):
        # "Alec Gaba" exists with no handle; "@user_nine" must not fuzzy-match it.
        result = resolve("@user_nine")
        self.assertEqual(result.status, "unresolved")
        self.assertIsNone(result.method)

    def test_unknown_handle_unresolved(self):
        self.assertEqual(resolve("@user_eight").status, "unresolved")

    def test_fuzzy_pass(self):
        result = resolve("Paul")
        self.assertEqual(
            (result.status, result.method, result.page_id), ("resolved", "fuzzy", PAUL)
        )

    def test_fuzzy_fail_low_score(self):
        self.assertEqual(resolve("Zzyzx").status, "unresolved")

    def test_fuzzy_fail_insufficient_lead(self):
        crowd = [
            Person(page_id="a", name="Paul Garcia"),
            Person(page_id="b", name="Paula Garcia"),
        ]
        result = resolve_mention("Paul Garcia", "assignee", PeopleIndex(crowd), SETTINGS)
        self.assertEqual(result.status, "unresolved")

    def test_fuzzy_thresholds_are_configurable(self):
        strict = ResolveSettings(fuzzy_min_score=99)
        result = resolve_mention("Paul", "assignee", PeopleIndex(people()), strict)
        self.assertEqual(result.status, "unresolved")

    def test_hedge_is_one_ambiguous_mention(self):
        result = resolve("Ramon but maybe Lena")
        self.assertEqual(result.status, "ambiguous")
        self.assertEqual(result.raw, "Ramon but maybe Lena")


class ResolvePeopleTests(unittest.TestCase):
    def test_multiple_assignees_standing_cc_always_in_cc(self):
        out = resolve_people("Paul / Ramon", None, people(), SETTINGS)
        self.assertEqual(out.assigned, [PAUL, RAMON])
        self.assertEqual(out.cc, [PRIYA])
        self.assertEqual([f.code for f in out.findings], ["fuzzy_person"])
        self.assertEqual(out.findings[0].params["contact"], "Paul Garcia")

    def test_standing_cc_not_duplicated_when_named(self):
        out = resolve_people("Lena", "@priya, @user_six", people(), SETTINGS)
        self.assertEqual(out.cc, [PRIYA, LARA])

    def test_names_without_separator_split_only_on_exact_aliases(self):
        out = resolve_people("Ramon @user_two Mira Lena", None, people(), SETTINGS)
        self.assertEqual(out.assigned, [RAMON, RAE, MIRA, LENA])
        self.assertEqual(out.findings, [])
        out = resolve_people("Mira Zzyzx", None, people(), SETTINGS)
        self.assertEqual(out.assigned, [])
        self.assertEqual(out.findings[0].params["raw"], "Mira Zzyzx")

    def test_unresolved_and_ambiguous_mentions_quoted_as_written(self):
        out = resolve_people("Ramon but maybe Lena / Kim", "@nobody", people(), SETTINGS)
        self.assertEqual(out.assigned, [])
        self.assertEqual(
            [(f.code, f.params["raw"], f.params["role"]) for f in out.findings],
            [
                ("ambiguous_person", "Ramon but maybe Lena", "assignee"),
                ("ambiguous_person", "Kim", "assignee"),
                ("unresolved_person", "@nobody", "cc"),
            ],
        )

    def test_conditional_mention_stays_ambiguous_with_complete_wording(self):
        out = resolve_people("Lena if available", "Mira kung free", people(), SETTINGS)
        self.assertEqual(out.assigned, [])
        self.assertEqual(out.cc, [PRIYA])
        self.assertEqual(
            [(m.raw, m.status) for m in out.mentions],
            [("Lena if available", "ambiguous"), ("Mira kung free", "ambiguous")],
        )
        self.assertEqual(
            [f.params["raw"] for f in out.findings], ["Lena if available", "Mira kung free"]
        )

    def test_unconditional_mentions_beside_a_conditional_one_still_resolve(self):
        out = resolve_people("ate Lena / Ramon, if abutin the next day", None, people(), SETTINGS)
        self.assertEqual(out.assigned, [LENA])
        self.assertEqual(out.findings[0].params["raw"], "Ramon if abutin the next day")

    def test_assignee_shifts_feed_deadline_resolver(self):
        out = resolve_people("Ramon, Mira / ate Lena", None, people(), SETTINGS)
        shifts = [(s.name, s.end_of_shift, s.shift_time) for s in out.assignee_shifts]
        self.assertEqual(
            shifts,
            [
                ("Ramon Reyes", "10:00 PM", time(22, 0)),
                ("Mira Santos", "6:00 AM", time(6, 0)),
                ("Lena Cruz", "Flexible / Variable", None),
            ],
        )

    def test_no_assignee_resolved_gives_empty_shifts(self):
        out = resolve_people("@user_eight", None, people(), SETTINGS)
        self.assertEqual(out.assignee_shifts, [])

    def test_no_standing_cc_config_means_no_standing_cc(self):
        out = resolve_people("Ramon", None, people(), ResolveSettings())
        self.assertEqual(out.cc, [])


class ProjectTests(unittest.TestCase):
    def test_whole_token_case_insensitive(self):
        out = resolve_project("mfg", projects())
        self.assertEqual((out.code, out.findings), ("MFG", []))
        self.assertEqual(resolve_project("SOMSOM", projects()).code, "SOMSOM")

    def test_longest_code_first(self):
        self.assertEqual(resolve_project("COLD CUT", projects()).code, "COLD CUT")
        self.assertEqual(resolve_project("cold", projects()).code, "COLD")
        self.assertEqual(resolve_project("LSB", projects()).code, "LSB")

    def test_substring_is_not_a_token(self):
        self.assertIsNone(resolve_project("MFGX", projects()).page_id)
        # "58" is not the whole code "58th".
        self.assertIsNone(resolve_project("58", projects()).page_id)

    def test_two_codes_in_one_mention_is_ambiguous(self):
        out = resolve_project("PP EP 2 (APC)", projects())
        self.assertIsNone(out.page_id)
        self.assertEqual(out.findings[0].code, "unresolved_project")
        self.assertEqual(out.findings[0].params["raw"], "PP EP 2 (APC)")

    def test_code_on_two_pages_is_ambiguous(self):
        self.assertIsNone(resolve_project("TWIN", projects()).page_id)

    def test_absent(self):
        out = resolve_project("NOPE", projects())
        self.assertIsNone(out.page_id)
        self.assertEqual([f.code for f in out.findings], ["unresolved_project"])
        self.assertEqual(resolve_project("", projects()).findings, [])


class RelationPropertyTests(unittest.TestCase):
    def test_only_resolved_ids_and_no_coordinator(self):
        people_out = resolve_people("Ramon / @nobody", None, people(), SETTINGS)
        props = relation_properties(people_out, resolve_project("NOPE", projects()))
        self.assertEqual(
            props,
            {
                "Assigned": {"relation": [{"id": RAMON}]},
                "cc": {"relation": [{"id": PRIYA}]},
                "Projects": {"relation": []},
            },
        )
        self.assertNotIn("🗂️ Coordinator", props)
        resolved = relation_properties(people_out, resolve_project("MFG", projects()))
        self.assertEqual(len(resolved["Projects"]["relation"]), 1)


class DepartmentTests(unittest.TestCase):
    def test_alias_then_live_validation(self):
        self.assertEqual(resolve_department("TO", DEPARTMENTS, {"TO": "TOC"}).department, "TOC")
        self.assertEqual(resolve_department(" coor ", DEPARTMENTS, {}).department, "COOR")
        self.assertEqual(resolve_department("AE", DEPARTMENTS, {}).department, "AE")

    def test_unknown_department_blank_with_finding(self):
        out = resolve_department("EDIT", DEPARTMENTS, {"TO": "TOC"})
        self.assertFalse(out.valid)
        self.assertIsNone(out.department)
        self.assertEqual(
            [(f.code, f.params) for f in out.findings], [("unknown_department", {"token": "EDIT"})]
        )

    def test_live_option_variations_resolve_to_exact_spelling(self):
        out = resolve_department("TO", [" toc"], {"TO": "TOC"})
        self.assertEqual(out.department, " toc")

    def test_ambiguous_live_options_stay_blank_with_finding(self):
        out = resolve_department("TOC", ["TOC", "toc "], {})
        self.assertIsNone(out.department)
        self.assertEqual([f.code for f in out.findings], ["unknown_department"])

    def test_option_set_is_live_not_hard_coded(self):
        self.assertEqual(resolve_department("GRADE", ["GRADE"], {}).department, "GRADE")
        self.assertFalse(resolve_department("TOC", ["GRADE"], {}).valid)


class CorpusTests(unittest.TestCase):
    """Every corpus JO resolves without error; Priya is always in cc."""

    ASSIGNED_RE = re.compile(r"^\s*assigned(?: to)?\s*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
    CC_RE = re.compile(r"^\s*cc\s*:\s*(.*)$", re.IGNORECASE | re.MULTILINE)
    HEADER_RE = re.compile(r"^(?:JO CODE:\s*)?\\?(.+?)\s*-\s*([A-Za-z]+)\s*-", re.IGNORECASE)

    def setUp(self):
        self.corpus = json.loads((FIXTURES / "redacted_corpus.json").read_text(encoding="utf-8"))
        self.index = PeopleIndex(people())

    def test_every_jo_resolves_and_keeps_standing_cc(self):
        for i, text in enumerate(self.corpus):
            with self.subTest(i=i):
                a = self.ASSIGNED_RE.search(text)
                c = self.CC_RE.search(text)
                out = resolve_people(
                    a.group(1) if a else None, c.group(1) if c else None, self.index, SETTINGS
                )
                self.assertIn(PRIYA, out.cc)
                self.assertEqual(len(out.assigned), len(set(out.assigned)))
                for mention in out.mentions:
                    if mention.status != "resolved":
                        self.assertIsNone(mention.page_id)

    def test_corpus_department_tokens_all_validate(self):
        tokens = set()
        for text in self.corpus:
            m = self.HEADER_RE.match(text.split("\n", 1)[0].strip())
            if m:
                tokens.add(m.group(2))
        self.assertTrue({"TOC", "AE", "TO", "COOR", "OE"} <= tokens)
        for token in tokens:
            with self.subTest(token=token):
                self.assertTrue(resolve_department(token, DEPARTMENTS, {"TO": "TOC"}).valid)


if __name__ == "__main__":
    unittest.main()


class MentionEntityTests(unittest.TestCase):
    def test_slices_by_utf16_offsets_and_keeps_user_ids(self):
        from jo_watchdog.core.resolve import mention_entities

        message = {
            "text": "😀 @raed and Benjamin",
            "entities": [
                {"type": "mention", "offset": 3, "length": 5},
                {"type": "text_mention", "offset": 13, "length": 8, "user": {"id": 222}},
                {"type": "bold", "offset": 0, "length": 2},
            ],
        }
        self.assertEqual(
            mention_entities(message),
            [
                MentionEntity(text="@raed", handle="raed"),
                MentionEntity(text="Benjamin", user_id="222"),
            ],
        )
