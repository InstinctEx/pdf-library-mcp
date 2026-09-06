"""Chunk boundaries and labels.

Lecture notes carry no Markdown headings; their structure is in the words that
open each passage, and those words are what makes a chunk findable.
"""

from __future__ import annotations

import pytest

from pdf_library.chunking import chunk_pages, classify, section_marker


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("Παράδειγμα: Να υπολογίσετε το ολοκλήρωμα", "Παράδειγμα"),
        ("Περίπτωση 2: Αν έχουμε", "Περίπτωση 2"),
        ("Θεώρημα 2.5. Έστω η δυναμοσειρά", "Θεώρημα 2.5"),
        ("Λύση Έχουμε δύο συναρτήσεις", "Λύση"),
        ("Βήμα 3 Εξισώνουμε τα κλάσματα", "Βήμα 3"),
        ("**Proof.** Apply Fatou's lemma", "Proof"),
    ],
)
def test_section_markers_are_recognised(line: str, expected: str) -> None:
    assert section_marker(line) == expected


@pytest.mark.parametrize(
    "line",
    [
        "Η συνάρτηση είναι συνεχής παντού",
        "παραγοντική ολοκλήρωση μέσα σε πρόταση",
        "$$\\int f = 1$$",
        "",
    ],
)
def test_ordinary_lines_are_not_headings(line: str) -> None:
    assert section_marker(line) is None


def test_marker_line_stays_in_the_body() -> None:
    """The heading word also opens the sentence, so it must not be eaten."""
    chunks = chunk_pages({1: "Λύση Έχουμε δύο συναρτήσεις.\n\nΤέλος."})
    assert chunks[0].heading == "Λύση"
    assert "Έχουμε δύο συναρτήσεις" in chunks[0].content


@pytest.mark.parametrize(
    ("heading", "expected"),
    [
        ("Απόδειξη", "proof"),
        ("Λύση", "solution"),
        ("Περίπτωση 2", "case"),
        ("Βήμα 3", "step"),
        ("Άσκηση 4", "exercise"),
        ("Θεώρημα 1.2", "theorem"),
        ("Παρατήρηση", "remark"),
        ("Ορισμός", "definition"),
    ],
)
def test_greek_headings_set_the_chunk_type(heading: str, expected: str) -> None:
    assert classify(heading, "some body text") == expected


def test_heading_wins_over_the_body() -> None:
    """A proof that mentions a theorem is still a proof."""
    assert classify("Απόδειξη", "This uses Theorem 4 and the example above") == "proof"


def test_a_new_marker_starts_a_new_chunk() -> None:
    page = (
        "Παράδειγμα: υπολόγισε το ολοκλήρωμα.\n\n"
        "Λύση Θέτουμε u = x.\n\n"
        "Άσκηση Να δείξετε ότι ισχύει.\n"
    )
    chunks = chunk_pages({1: page}, target_chars=10_000, max_chars=20_000)
    assert [c.heading for c in chunks] == ["Παράδειγμα", "Λύση", "Άσκηση"]
    assert [c.chunk_type for c in chunks] == ["example", "solution", "exercise"]
