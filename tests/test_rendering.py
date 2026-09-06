"""Images are the expensive path, so their cost has to be predictable."""

from __future__ import annotations

from pathlib import Path

import pytest

from pdf_library.library import Library, LibraryError
from pdf_library.rendering import estimate_image_tokens, render_region


def test_render_respects_the_token_budget(math_pdf: Path) -> None:
    for budget in (200, 400, 900, 1500):
        region = render_region(math_pdf, 1, max_tokens=budget)
        assert region.estimated_tokens <= budget * 1.1


def test_cropping_is_sharper_at_the_same_budget(math_pdf: Path) -> None:
    """The argument for cropping: same cost, far more pixels on the region."""
    full = render_region(math_pdf, 1, max_tokens=900)
    crop = render_region(math_pdf, 1, bbox=(50, 300, 550, 420), max_tokens=900)
    assert crop.cropped
    assert crop.width > full.width


def test_a_tiny_bbox_falls_back_to_the_page(math_pdf: Path) -> None:
    region = render_region(math_pdf, 1, bbox=(10, 10, 11, 11))
    assert not region.cropped


def test_bbox_outside_the_page_falls_back(math_pdf: Path) -> None:
    region = render_region(math_pdf, 1, bbox=(9000, 9000, 9100, 9100))
    assert not region.cropped


def test_page_out_of_range_is_rejected(math_pdf: Path) -> None:
    with pytest.raises(ValueError):
        render_region(math_pdf, 999)


def test_estimate_matches_the_documented_formula() -> None:
    assert estimate_image_tokens(750, 1000) == 1000
    # Oversized images are downscaled by the model before billing.
    assert estimate_image_tokens(4000, 4000) == estimate_image_tokens(1568, 1568)


def test_render_through_the_library(library: Library, math_pdf: Path) -> None:
    result = library.import_pdf(math_pdf)
    region = library.render_page_region(result.document_id, 1, max_tokens=400)
    assert region.data[:2] == b"\xff\xd8"  # JPEG magic
    assert region.estimated_tokens <= 440


def test_missing_block_layout_is_explained(library: Library, math_pdf: Path) -> None:
    """The fast tier records no blocks; asking for one must not be an error."""
    result = library.import_pdf(math_pdf)
    region = library.render_page_region(result.document_id, 1, block=0)
    assert not region.cropped
    assert "no block layout" in region.note


def test_unknown_page_is_a_library_error(library: Library, math_pdf: Path) -> None:
    result = library.import_pdf(math_pdf)
    with pytest.raises(LibraryError):
        library.render_page_region(result.document_id, 99)
