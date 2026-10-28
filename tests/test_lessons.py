"""docs/LESSONS.md: lesson IDs are stable and never reused, so one ID names one lesson."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

LESSONS = Path(__file__).resolve().parent.parent / "docs" / "LESSONS.md"
ROW = re.compile(r"^\| ([A-Z]{2,4}-\d+) \|", re.M)


def lesson_ids(text: str) -> list[str]:
    return ROW.findall(text)


def test_the_row_pattern_finds_lessons():
    ids = lesson_ids(LESSONS.read_text())
    assert len(ids) > 50 and "AGT-01" in ids and "DEP-01" in ids


def test_lesson_ids_are_unique():
    """A duplicated ID makes every test docstring and cross-reference that names it ambiguous."""
    dupes = sorted(i for i, n in Counter(lesson_ids(LESSONS.read_text())).items() if n > 1)
    assert not dupes, f"lesson IDs used more than once in docs/LESSONS.md: {', '.join(dupes)} (give one a free ID)"


def test_a_duplicate_would_be_caught():
    text = "| INT-13 | a |\n| INT-14 | b |\n| INT-13 | c |\n"
    assert [i for i, n in Counter(lesson_ids(text)).items() if n > 1] == ["INT-13"]
