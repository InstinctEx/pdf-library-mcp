"""Rendering page regions as images, under a token budget.

An image of a page costs several times what its extracted text costs, and it
stays in the conversation for every later turn, so this is never something to
do by default. It earns its place in exactly one situation: the text is
visibly wrong and the reader needs to see the original.

Two things keep the cost honest. Cropping to a single block -- one equation
rather than a whole page -- is roughly a sixth of the pixels. And the render
scale is derived from a token budget rather than a DPI, so a caller asks for
"about 400 tokens" and gets the largest image that fits.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pymupdf

# Anthropic's vision models bill an image at roughly width*height/750 tokens,
# and downscale anything whose longest side exceeds 1568 pixels.
PIXELS_PER_TOKEN = 750
MAX_DIMENSION = 1568


@dataclass
class RenderedRegion:
    data: bytes
    mime_type: str
    width: int
    height: int
    page_number: int
    cropped: bool
    estimated_tokens: int
    note: str = ""


def estimate_image_tokens(width: int, height: int) -> int:
    scale = min(1.0, MAX_DIMENSION / max(width, height, 1))
    return int((width * scale) * (height * scale) / PIXELS_PER_TOKEN)


def _scale_for_budget(
    rect: pymupdf.Rect, max_tokens: int, max_scale: float = 4.0
) -> float:
    """Choose the render scale whose output lands within the token budget."""
    width = max(rect.width, 1.0)
    height = max(rect.height, 1.0)
    # tokens = (w*s * h*s) / 750  =>  s = sqrt(tokens * 750 / (w*h))
    scale = ((max_tokens * PIXELS_PER_TOKEN) / (width * height)) ** 0.5
    # Never exceed the point at which the model would downscale anyway.
    ceiling = MAX_DIMENSION / max(width, height)
    return max(0.2, min(scale, ceiling, max_scale))


def render_region(
    pdf_path: Path,
    page_number: int,
    bbox: tuple[float, float, float, float] | None = None,
    max_tokens: int = 900,
    margin: float = 8.0,
    quality: int = 80,
) -> RenderedRegion:
    """Render one page, or one region of it, as a JPEG.

    ``page_number`` is 1-based. ``bbox`` is in PDF points, as Marker reports
    it; a margin is added so an equation is not clipped at its own baseline.
    """
    with pymupdf.open(pdf_path) as doc:
        if not 1 <= page_number <= doc.page_count:
            raise ValueError(
                f"page {page_number} is outside this document (1-{doc.page_count})"
            )
        page = doc[page_number - 1]
        clip = None
        cropped = False
        if bbox:
            requested = pymupdf.Rect(*bbox)
            # Judge the region by what was asked for, not by what the margin
            # inflated it to: a degenerate box must fall back to the page.
            if requested.width >= 4 and requested.height >= 4:
                grown = (
                    requested + (-margin, -margin, margin, margin)
                ) & page.rect
                if not grown.is_empty and grown.width >= 4 and grown.height >= 4:
                    clip = grown
                    cropped = True

        rect = clip or page.rect
        scale = _scale_for_budget(rect, max_tokens)
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False
        )
        data = pixmap.tobytes("jpeg", jpg_quality=quality)
        width, height = pixmap.width, pixmap.height

    return RenderedRegion(
        data=data,
        mime_type="image/jpeg",
        width=width,
        height=height,
        page_number=page_number,
        cropped=cropped,
        estimated_tokens=estimate_image_tokens(width, height),
    )
