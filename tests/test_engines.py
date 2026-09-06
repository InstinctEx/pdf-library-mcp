"""Engine adapters, especially the parts that only misbehave in production."""

from __future__ import annotations

from pathlib import Path

import pytest

from pdf_library.config import Config
from pdf_library.engines import EngineUnavailable, MarkerEngine, get_engine
from pdf_library.engines.marker_engine import _explain


def test_unknown_engine_is_rejected(config: Config) -> None:
    with pytest.raises(EngineUnavailable):
        get_engine("nonesuch", config)


def test_fast_engine_is_always_available(config: Config) -> None:
    ok, _ = get_engine("pymupdf4llm", config).available()
    assert ok


@pytest.mark.parametrize(
    ("pages", "expected"),
    [
        ([1], "0"),
        ([1, 2, 3], "0-2"),
        ([1, 2, 3, 9], "0-2,8"),
        ([120], "119"),
        ([5, 4, 3], "2-4"),
    ],
)
def test_page_ranges_are_zero_based_and_collapsed(
    pages: list[int], expected: str
) -> None:
    assert MarkerEngine()._page_range(pages) == expected


def test_marker_is_found_inside_the_virtualenv() -> None:
    """PATH is not enough when an MCP server is launched by absolute path."""
    import shutil
    import sys

    beside = Path(sys.executable).parent / "marker_single"
    if not beside.is_file():
        pytest.skip("marker not installed")
    resolved = MarkerEngine()._resolve()
    assert resolved == str(beside) or resolved == shutil.which("marker_single")


def test_missing_runner_is_explained_not_echoed() -> None:
    """The last line of marker's traceback is a hint, not the cause."""
    output = (
        "Traceback (most recent call last):\n"
        "surya.inference.backends.spawn.SpawnError: llama-server binary not found."
        " Install with:\n  macOS:  brew install llama.cpp\n"
        "Or set LLAMA_CPP_BINARY in your env to the binary path.\n"
    )
    explanation = _explain(output)
    assert "llama.cpp" in explanation
    assert not explanation.startswith("Or set")


def test_exception_line_is_preferred_over_trailing_noise() -> None:
    output = "Traceback:\nValueError: bad page range\n  see the docs for help\n"
    assert _explain(output) == "ValueError: bad page range"


def test_page_separator_splitting() -> None:
    engine = MarkerEngine()
    markdown = (
        "{0}------------------------------------\n"
        "First page body.\n"
        "{1}------------------------------------\n"
        "Second page body.\n"
    )
    pages = engine._split_pages(markdown, requested=None)
    assert [p.page_number for p in pages] == [1, 2]
    assert pages[0].markdown == "First page body."


def test_split_pages_honours_requested_numbering() -> None:
    engine = MarkerEngine()
    markdown = (
        "{0}------------------------------------\n"
        "Body of page one hundred and twenty.\n"
    )
    pages = engine._split_pages(markdown, requested=[120])
    assert [p.page_number for p in pages] == [120]
