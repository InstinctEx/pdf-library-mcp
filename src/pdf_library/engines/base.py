"""The extraction engine interface.

Engines are swappable and none of them is required at import time: the fast
engine ships with the package, the quality engine is optional and is only
touched when an upgrade is actually requested.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..blocks import Block


@dataclass
class PageInspection:
    page_number: int
    native_chars: int
    has_images: bool
    is_scanned: bool
    ocr_layer: bool = False
    # True when the page uses fonts that only appear in typeset mathematics.
    math_fonts: bool = False


@dataclass
class Inspection:
    page_count: int
    pages: list[PageInspection]
    kind: str  # digital_text | scanned | mixed
    scanned_pages: list[int] = field(default_factory=list)
    title: str | None = None

    @property
    def needs_ocr(self) -> bool:
        return bool(self.scanned_pages)


@dataclass
class ExtractedPage:
    page_number: int
    markdown: str
    ocr_used: bool = False
    ocr_reason: str | None = None
    # Laid-out regions, when the engine reports them. Used for reliable chunk
    # typing and for cropping the original of a single equation.
    blocks: list["Block"] = field(default_factory=list)
    page_width: float = 0.0
    page_height: float = 0.0


@dataclass
class ExtractionResult:
    engine: str
    engine_version: str
    pages: list[ExtractedPage]
    duration_s: float = 0.0


class EngineUnavailable(RuntimeError):
    """Raised when an optional engine is not installed or not runnable."""


@runtime_checkable
class ExtractionEngine(Protocol):
    name: str
    quality_tier: str  # "fast" or "high"

    def available(self) -> tuple[bool, str]:
        """Return (usable, human-readable reason)."""

    def version(self) -> str: ...

    def extract(
        self, pdf_path: Path, pages: list[int] | None = None
    ) -> ExtractionResult:
        """Extract Markdown. ``pages`` is 1-based; None means the whole file."""
