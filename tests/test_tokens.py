"""Token estimation only has to be roughly right, and never low by much."""

from __future__ import annotations

from pdf_library.tokens import estimate_tokens, truncate_to_tokens


def test_empty_text_is_zero() -> None:
    assert estimate_tokens("") == 0


def test_estimate_grows_with_length() -> None:
    short = estimate_tokens("hello world")
    long = estimate_tokens("hello world " * 50)
    assert long > short * 20


def test_greek_costs_more_than_latin_per_character() -> None:
    latin = estimate_tokens("abcdefghij" * 10)
    greek = estimate_tokens("αβγδεζηθικ" * 10)
    assert greek > latin


def test_truncation_respects_the_budget() -> None:
    text = "word " * 2000
    cut, was_cut = truncate_to_tokens(text, 100)
    assert was_cut
    assert estimate_tokens(cut) <= 100


def test_short_text_is_untouched() -> None:
    cut, was_cut = truncate_to_tokens("short", 100)
    assert cut == "short" and not was_cut


def test_truncation_breaks_at_whitespace() -> None:
    cut, _ = truncate_to_tokens("alpha beta gamma delta " * 100, 20)
    assert not cut.endswith(("alph", "bet", "gamm"))
