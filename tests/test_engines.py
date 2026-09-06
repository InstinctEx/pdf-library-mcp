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


def test_json_output_is_parsed_into_blocks() -> None:
    """The adapter reads Marker's JSON, not its Markdown renderer."""
    from pdf_library.blocks import page_methods_from_meta, parse_document

    document = {
        "children": [
            {
                "id": "/page/119/Page/1",
                "block_type": "Page",
                "bbox": [0, 0, 596, 842],
                "children": [
                    {
                        "id": "/page/119/Text/2",
                        "block_type": "Text",
                        "bbox": [10, 20, 300, 60],
                        "html": "<p>Let <math>f(x) = x</math> be given.</p>",
                    },
                    {
                        "id": "/page/119/Equation/3",
                        "block_type": "Equation",
                        "bbox": [10, 70, 500, 200],
                        "html": '<p><math display="block">\\int f = 1</math></p>',
                    },
                ],
            }
        ]
    }
    meta = {"page_stats": [{"page_id": 119, "text_extraction_method": "surya"}]}

    pages = parse_document(document, page_methods_from_meta(meta))
    assert len(pages) == 1
    page = pages[0]
    assert page.page_number == 120  # Marker counts from zero
    assert page.ocr_used
    assert [b.block_type for b in page.blocks] == ["Text", "Equation"]
    assert page.blocks[1].bbox == (10.0, 70.0, 500.0, 200.0)
    assert "$$" in page.markdown
    assert "$f(x) = x$" in page.markdown


def test_native_text_pages_are_not_marked_as_ocr() -> None:
    from pdf_library.blocks import page_methods_from_meta, parse_document

    document = {
        "children": [
            {
                "id": "/page/0/Page/1",
                "block_type": "Page",
                "bbox": [0, 0, 596, 842],
                "children": [
                    {"block_type": "Text", "bbox": [0, 0, 1, 1], "html": "<p>hi</p>"}
                ],
            }
        ]
    }
    meta = {"page_stats": [{"page_id": 0, "text_extraction_method": "pdftext"}]}
    assert not parse_document(document, page_methods_from_meta(meta))[0].ocr_used
