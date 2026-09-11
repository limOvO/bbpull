"""Course metadata: parse the academic year / term out of a course name.

Real shapes observed on this institution's site (measured over 91 enrolments):

    [2026/27-1] NUR2051/NUR2046 Nursing Practicum I (NY2023)   <- 58 of them
    [2023/24] ...                                              <- year only
    Library / CAPLE / Nursing_Society                          <- no prefix
    (no name at all)                                           <- 29 courses the
                                                                 student role gets
                                                                  403 on

Terms run 1..3 per academic year. They are labelled factually ("第 1 學期")
rather than guessed as 上/下/暑期, because the mapping is institution-specific
and a wrong label is worse than a neutral one.

Pure functions only - no Tk, no I/O - so all of this is testable offline.
"""

import re
from collections import OrderedDict
from dataclasses import dataclass, field

#: Filter sentinel meaning "no restriction".
ANY = None
#: Filter sentinel for courses whose year (or term) could not be determined.
UNSPECIFIED = "__unspecified__"

UNSPECIFIED_YEAR_LABEL = "未標示學年"
UNSPECIFIED_TERM_LABEL = "未標示學期"
UNRESOLVED_NAME = "(無法取得課程名稱)"

_YEAR_TERM = re.compile(
    r"^\s*\[\s*(?P<year>\d{4})\s*/\s*(?P<second>\d{2,4})\s*"
    r"(?:-\s*(?P<term>[0-9A-Za-z]+))?\s*\]\s*(?P<rest>.*)$"
)


@dataclass
class CourseMeta:
    """One enrolment, with the year/term pulled out of its name."""

    course_id: str
    name: str = ""
    role: str = ""
    available: object = None
    year: str = ""          # e.g. "2026/27", "" when unknown
    term: str = ""          # e.g. "1", "" when unknown
    title: str = ""         # name with the [year/term] prefix removed
    unresolved: bool = False  # the API would not give us a name (403 commonly)

    # -- display helpers -------------------------------------------------
    @property
    def year_label(self):
        return self.year or UNSPECIFIED_YEAR_LABEL

    @property
    def term_label(self):
        if not self.term:
            return UNSPECIFIED_TERM_LABEL
        return f"第 {self.term} 學期"

    @property
    def display_name(self):
        if self.unresolved or not self.name:
            return UNRESOLVED_NAME
        return self.title or self.name

    @property
    def subtitle(self):
        parts = []
        if self.role:
            parts.append(str(self.role))
        parts.append(self.course_id)
        return "  ·  ".join(parts)

    def to_dict(self):
        return {
            "courseId": self.course_id,
            "name": self.name,
            "role": self.role,
            "available": self.available,
            "year": self.year,
            "term": self.term,
            "title": self.title,
            "unresolved": self.unresolved,
        }


@dataclass
class CourseFilter:
    """A year + term restriction, applied to a list of `CourseMeta`."""

    year: str = ANY
    term: str = ANY
    text: str = ""

    @property
    def active(self):
        return self.year is not ANY or self.term is not ANY or bool(self.text.strip())

    def matches(self, meta):
        if self.year is not ANY:
            if self.year == UNSPECIFIED:
                if meta.year:
                    return False
            elif meta.year != self.year:
                return False
        if self.term is not ANY:
            if self.term == UNSPECIFIED:
                if meta.term:
                    return False
            elif meta.term != self.term:
                return False
        needle = self.text.strip().lower()
        if needle:
            haystack = f"{meta.name} {meta.title} {meta.course_id}".lower()
            if needle not in haystack:
                return False
        return True

    def describe(self):
        bits = []
        if self.year is not ANY:
            bits.append(self.year if self.year != UNSPECIFIED else UNSPECIFIED_YEAR_LABEL)
        if self.term is not ANY:
            bits.append(self.term if self.term == UNSPECIFIED
                        else f"第 {self.term} 學期")
        if self.text.strip():
            bits.append(f"「{self.text.strip()}」")
        return " · ".join(bits)


def parse_course_name(name):
    """Split a course name into (year, term, title).

    Returns ("", "", name) when there is no recognisable prefix, so unparsable
    names still render sensibly instead of being dropped.
    """
    text = (name or "").strip()
    if not text:
        return "", "", ""
    match = _YEAR_TERM.match(text)
    if not match:
        return "", "", text
    year = f"{match.group('year')}/{match.group('second')}"
    term = match.group("term") or ""
    rest = (match.group("rest") or "").strip()
    return year, term, rest or text


def parse_course(course):
    """Build a `CourseMeta` from a normalised course row."""
    course_id = str(course.get("courseId") or course.get("id") or "").strip()
    name = (course.get("name") or course.get("displayName") or "").strip()
    year, term, title = parse_course_name(name)
    return CourseMeta(
        course_id=course_id,
        name=name,
        role=str(course.get("role") or ""),
        available=course.get("available"),
        year=year,
        term=term,
        title=title,
        unresolved=not bool(name),
    )


def parse_courses(courses):
    return [parse_course(c) for c in (courses or [])]


def _year_sort_key(year):
    """Sort newest first; unknown years always last."""
    if not year:
        return (1, 0, "")
    digits = re.match(r"^(\d{4})", year)
    return (0, -int(digits.group(1)) if digits else 0, year)


def available_years(metas):
    """Distinct years present, newest first, with `UNSPECIFIED` last if needed."""
    years = {m.year for m in metas if m.year}
    ordered = sorted(years, key=_year_sort_key)
    if any(not m.year for m in metas):
        ordered.append(UNSPECIFIED)
    return ordered


def available_terms(metas, year=ANY):
    """Distinct terms present for the given year (or across all years).

    Ordered **descending** (3, 2, 1) to match the year ordering: the list reads
    newest-first throughout, so the most recent term of a year is at the top.
    """
    pool = [m for m in metas if year is ANY or (m.year if m.year else UNSPECIFIED) == year]
    terms = {m.term for m in pool if m.term}
    ordered = sorted(terms, key=lambda t: (not t.isdigit(),
                                           -(int(t) if t.isdigit() else 0), t))
    if any(not m.term for m in pool):
        ordered.append(UNSPECIFIED)
    return ordered


def year_label(year):
    if year is ANY:
        return "全部學年"
    if year == UNSPECIFIED:
        return UNSPECIFIED_YEAR_LABEL
    return year


def term_label(term):
    if term is ANY:
        return "全部學期"
    if term == UNSPECIFIED:
        return UNSPECIFIED_TERM_LABEL
    return f"第 {term} 學期"


def filter_courses(metas, spec=None, text=""):
    """Apply a `CourseFilter` (or year/term pair) to `metas`."""
    if spec is None:
        spec = CourseFilter(text=text)
    return [m for m in metas if spec.matches(m)]


def group_by_year(metas):
    """Group courses by academic year, newest first, for the grouped sidebar.

    Within a year, terms run **descending** (3, 2, 1) and then unspecified;
    within a term, courses keep a stable alphabetical order so the list does not
    shuffle between renders.
    """
    groups = OrderedDict()
    for meta in metas:
        groups.setdefault(meta.year or UNSPECIFIED, []).append(meta)
    ordered = OrderedDict()
    for key in sorted(groups, key=_year_sort_key):
        items = groups[key]
        items.sort(key=lambda m: (
            _term_sort(m.term), m.display_name.lower(), m.course_id
        ))
        ordered[key] = items
    return ordered


def _term_sort(term):
    """Sort key giving descending terms: 3, 2, 1, then unspecified."""
    if not term:
        return (1, 0, "")
    return (0, -(int(term)) if term.isdigit() else 0, term)


def summarise(metas, filtered):
    """A short status string for the sidebar footer."""
    total = len(metas)
    shown = len(filtered)
    if shown == total:
        return f"{total} 門課程"
    return f"{shown} / {total} 門課程"
