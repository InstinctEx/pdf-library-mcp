"""Words broken across lines, and the two opposite ways OCR gets them wrong."""

from __future__ import annotations

from pdf_library.textfix import rejoin_hyphenation


def test_a_genuine_split_is_rejoined() -> None:
    text = "εφαρμόζουμε πάλι παραγο-\n\n  -ντική ολοκλήρωση."
    report = rejoin_hyphenation(text)
    assert "παραγοντική" in report.text
    assert report.counts["hyphen_rejoined"] == 1


def test_a_duplicated_tail_is_dropped() -> None:
    """Marker sometimes joins the word and emits the tail a second time."""
    text = "Να υπολογίσετε το ολοκλήρωμα\n\n  - ρωμα $\\int x\\,dx$"
    report = rejoin_hyphenation(text)
    assert report.text.count("ρωμα") == 1
    assert "$\\int x\\,dx$" in report.text
    assert report.counts["hyphen_duplicate_dropped"] == 1


def test_bullet_lists_are_left_alone() -> None:
    text = "Κριτήρια:\n\n- πρώτο στοιχείο\n- δεύτερο στοιχείο"
    report = rejoin_hyphenation(text)
    assert report.text == text
    assert report.total == 0


def test_a_dash_starting_a_sentence_is_left_alone() -> None:
    text = "Τέλος.\n\n- Και μετά νέα πρόταση."
    assert rejoin_hyphenation(text).total == 0


def test_repair_is_idempotent() -> None:
    text = "παραγο-\n\n-ντική ολοκλήρωση"
    once = rejoin_hyphenation(text).text
    assert rejoin_hyphenation(once).text == once


def test_a_leading_fragment_with_no_predecessor_survives() -> None:
    assert rejoin_hyphenation("-ντική ολοκλήρωση").total == 0


def test_english_hyphenation_too() -> None:
    report = rejoin_hyphenation("we apply integra-\n\n-tion by parts")
    assert "integration by parts" in report.text
