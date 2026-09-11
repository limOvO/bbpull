"""Offline tests for course year/term parsing and filtering.

The shapes asserted here are the ones actually observed on the live site (91
enrolments): `[2026/27-1] Title`, `[2023/24]` with no term, plain names with no
prefix, and courses whose name could not be fetched at all.

Pure logic - no Tk, no network - so this runs as part of `unittest discover`.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bbpull.gui.courses import (  # noqa: E402
    ANY,
    UNSPECIFIED,
    CourseFilter,
    available_terms,
    available_years,
    filter_courses,
    group_by_year,
    parse_course,
    parse_course_name,
    parse_courses,
    summarise,
    term_label,
    year_label,
)

#: Real names, copied from the live enrolment list.
REAL = [
    {"courseId": "_12529_1", "name": "[2026/27-1] NUR2051/NUR2046 Nursing Practicum I (NY2023)",
     "role": "Student"},
    {"courseId": "_12569_1", "name": "[2025/26-3] GEB1305-L01 China and The World",
     "role": "Student"},
    {"courseId": "_12564_1",
     "name": "[2025/26-3] GEA3302-L01 Problem Solving Skills in The Modern World",
     "role": "Student"},
    {"courseId": "_12648_1", "name": "[2024/25-1] Clinical Alert", "role": "Student"},
    {"courseId": "_12647_1", "name": "Library", "role": "Student"},
    {"courseId": "_3249_1", "name": "Nursing_Society", "role": "Student"},
    {"courseId": "_12159_1", "name": "", "role": "Student"},
]


class ParseNameTests(unittest.TestCase):
    def test_full_prefix(self):
        year, term, title = parse_course_name(
            "[2026/27-1] NUR2051/NUR2046 Nursing Practicum I (NY2023)")
        self.assertEqual(year, "2026/27")
        self.assertEqual(term, "1")
        self.assertEqual(title, "NUR2051/NUR2046 Nursing Practicum I (NY2023)")

    def test_year_only_prefix(self):
        year, term, title = parse_course_name("[2023/24] Something")
        self.assertEqual(year, "2023/24")
        self.assertEqual(term, "")
        self.assertEqual(title, "Something")

    def test_no_prefix(self):
        year, term, title = parse_course_name("Library")
        self.assertEqual((year, term), ("", ""))
        self.assertEqual(title, "Library")

    def test_empty_name(self):
        self.assertEqual(parse_course_name(""), ("", "", ""))
        self.assertEqual(parse_course_name(None), ("", "", ""))

    def test_tolerates_spacing_variants(self):
        for text in ("[2025/26-2] X", "[ 2025/26-2 ] X", "[2025/26 - 2] X"):
            year, term, title = parse_course_name(text)
            self.assertEqual(year, "2025/26", text)
            self.assertEqual(term, "2", text)
            self.assertEqual(title, "X", text)

    def test_four_digit_second_year(self):
        year, term, _ = parse_course_name("[2025/2026-1] X")
        self.assertEqual(year, "2025/2026")

    def test_alphanumeric_term(self):
        year, term, _ = parse_course_name("[2025/26-S] Summer")
        self.assertEqual(year, "2025/26")
        self.assertEqual(term, "S")

    def test_prefix_without_title_falls_back_to_full_name(self):
        _, _, title = parse_course_name("[2025/26-1]")
        self.assertTrue(title)


class ParseCourseTests(unittest.TestCase):
    def test_parses_the_real_list(self):
        metas = parse_courses(REAL)
        self.assertEqual(len(metas), 7)
        first = metas[0]
        self.assertEqual(first.course_id, "_12529_1")
        self.assertEqual(first.year, "2026/27")
        self.assertEqual(first.term, "1")
        self.assertFalse(first.unresolved)

    def test_unresolved_name_is_flagged(self):
        metas = parse_courses(REAL)
        blank = [m for m in metas if m.course_id == "_12159_1"][0]
        self.assertTrue(blank.unresolved)
        self.assertEqual(blank.year, "")
        self.assertIn("無法取得", blank.display_name)

    def test_display_name_strips_the_prefix(self):
        metas = parse_courses(REAL)
        self.assertTrue(metas[0].display_name.startswith("NUR2051"))
        self.assertNotIn("[2026/27-1]", metas[0].display_name)

    def test_labels(self):
        metas = parse_courses(REAL)
        self.assertEqual(metas[0].year_label, "2026/27")
        self.assertEqual(metas[0].term_label, "第 1 學期")
        library = [m for m in metas if m.course_id == "_12647_1"][0]
        self.assertEqual(library.year_label, "未標示學年")
        self.assertEqual(library.term_label, "未標示學期")

    def test_subtitle_contains_role_and_id(self):
        meta = parse_course(REAL[0])
        self.assertIn("Student", meta.subtitle)
        self.assertIn("_12529_1", meta.subtitle)

    def test_missing_fields_do_not_crash(self):
        meta = parse_course({})
        self.assertEqual(meta.course_id, "")
        self.assertTrue(meta.unresolved)


class AvailableOptionsTests(unittest.TestCase):
    def setUp(self):
        self.metas = parse_courses(REAL)

    def test_years_newest_first_with_unspecified_last(self):
        years = available_years(self.metas)
        self.assertEqual(years[0], "2026/27")
        self.assertIn("2025/26", years)
        self.assertIn("2024/25", years)
        self.assertEqual(years[-1], UNSPECIFIED)

    def test_years_are_deduplicated(self):
        years = available_years(self.metas)
        self.assertEqual(len(years), len(set(years)))
        # Two courses share 2025/26.
        self.assertEqual(years.count("2025/26"), 1)

    def test_terms_within_a_year(self):
        terms = available_terms(self.metas, "2025/26")
        self.assertEqual(terms, ["3"])

    def test_terms_are_descending(self):
        """Newest term first: 3, 2, 1 - matching the year ordering above it."""
        metas = parse_courses([
            {"courseId": "_1", "name": "[2025/26-1] A"},
            {"courseId": "_2", "name": "[2025/26-2] B"},
            {"courseId": "_3", "name": "[2025/26-3] C"},
        ])
        self.assertEqual(available_terms(metas, "2025/26"), ["3", "2", "1"])

    def test_unspecified_term_stays_last_even_when_descending(self):
        metas = parse_courses([
            {"courseId": "_1", "name": "[2025/26-1] A"},
            {"courseId": "_2", "name": "[2025/26-3] C"},
            {"courseId": "_3", "name": "[2025/26] NoTerm"},
        ])
        self.assertEqual(available_terms(metas, "2025/26")[-1], UNSPECIFIED)

    def test_terms_across_all_years(self):
        terms = available_terms(self.metas)
        self.assertIn("1", terms)
        self.assertIn("3", terms)
        self.assertEqual(terms[-1], UNSPECIFIED)

    def test_no_metas(self):
        self.assertEqual(available_years([]), [])
        self.assertEqual(available_terms([]), [])


class FilterTests(unittest.TestCase):
    def setUp(self):
        self.metas = parse_courses(REAL)

    def test_no_filter_returns_everything(self):
        spec = CourseFilter()
        self.assertFalse(spec.active)
        self.assertEqual(len(filter_courses(self.metas, spec)), len(self.metas))

    def test_filter_by_year(self):
        spec = CourseFilter(year="2025/26")
        self.assertTrue(spec.active)
        got = filter_courses(self.metas, spec)
        self.assertEqual(len(got), 2)
        self.assertTrue(all(m.year == "2025/26" for m in got))

    def test_filter_by_year_and_term(self):
        got = filter_courses(self.metas, CourseFilter(year="2025/26", term="3"))
        self.assertEqual(len(got), 2)
        got = filter_courses(self.metas, CourseFilter(year="2025/26", term="1"))
        self.assertEqual(got, [])

    def test_filter_unspecified_year(self):
        got = filter_courses(self.metas, CourseFilter(year=UNSPECIFIED))
        ids = {m.course_id for m in got}
        self.assertIn("_12647_1", ids)   # Library (no prefix)
        self.assertIn("_12159_1", ids)   # unresolved name
        self.assertNotIn("_12529_1", ids)

    def test_filter_unspecified_term_within_a_year(self):
        got = filter_courses(self.metas, CourseFilter(year="2024/25", term=UNSPECIFIED))
        # "[2024/25-1] Clinical Alert" has term 1, so it must be excluded.
        self.assertEqual(got, [])

    def test_text_search_matches_title_and_id(self):
        self.assertEqual(len(filter_courses(self.metas, CourseFilter(text="Library"))), 1)
        self.assertEqual(len(filter_courses(self.metas, CourseFilter(text="_12529_1"))), 1)
        self.assertEqual(len(filter_courses(self.metas, CourseFilter(text="nursing"))), 2)

    def test_text_search_is_case_insensitive(self):
        self.assertEqual(len(filter_courses(self.metas, CourseFilter(text="LIBRARY"))), 1)

    def test_combined_filters_intersect(self):
        spec = CourseFilter(year="2025/26", text="China")
        got = filter_courses(self.metas, spec)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].course_id, "_12569_1")

    def test_no_match(self):
        self.assertEqual(filter_courses(self.metas, CourseFilter(text="zzz")), [])

    def test_describe_is_human_readable(self):
        spec = CourseFilter(year="2025/26", term="3", text="China")
        text = spec.describe()
        self.assertIn("2025/26", text)
        self.assertIn("第 3 學期", text)
        self.assertIn("China", text)


class GroupingTests(unittest.TestCase):
    def setUp(self):
        self.metas = parse_courses(REAL)

    def test_grouped_newest_year_first(self):
        groups = group_by_year(self.metas)
        keys = list(groups)
        self.assertEqual(keys[0], "2026/27")
        self.assertEqual(keys[-1], UNSPECIFIED)

    def test_within_a_year_sorted_by_term_then_name(self):
        groups = group_by_year(self.metas)
        same_year = groups["2025/26"]
        self.assertEqual(len(same_year), 2)
        names = [m.display_name for m in same_year]
        self.assertEqual(names, sorted(names))

    def test_terms_inside_a_group_are_descending(self):
        metas = parse_courses([
            {"courseId": "_1", "name": "[2025/26-1] Alpha"},
            {"courseId": "_2", "name": "[2025/26-2] Beta"},
            {"courseId": "_3", "name": "[2025/26-3] Gamma"},
        ])
        groups = group_by_year(metas)
        terms = [m.term for m in groups["2025/26"]]
        self.assertEqual(terms, ["3", "2", "1"])

    def test_every_course_lands_in_exactly_one_group(self):
        groups = group_by_year(self.metas)
        total = sum(len(v) for v in groups.values())
        self.assertEqual(total, len(self.metas))

    def test_empty(self):
        self.assertEqual(group_by_year([]), {})


class LabelTests(unittest.TestCase):
    def test_year_labels(self):
        self.assertEqual(year_label(ANY), "全部學年")
        self.assertEqual(year_label(UNSPECIFIED), "未標示學年")
        self.assertEqual(year_label("2025/26"), "2025/26")

    def test_term_labels(self):
        self.assertEqual(term_label(ANY), "全部學期")
        self.assertEqual(term_label(UNSPECIFIED), "未標示學期")
        self.assertEqual(term_label("2"), "第 2 學期")


class SummaryTests(unittest.TestCase):
    def test_unfiltered(self):
        self.assertEqual(summarise(list(range(91)), list(range(91))), "91 門課程")

    def test_filtered(self):
        self.assertEqual(summarise(list(range(91)), list(range(12))), "12 / 91 門課程")

    def test_empty(self):
        self.assertEqual(summarise([], []), "0 門課程")


if __name__ == "__main__":
    unittest.main()
