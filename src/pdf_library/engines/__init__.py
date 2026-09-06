"""Extraction engine registry."""

from __future__ import annotations

from ..config import Config
from .base import (
    EngineUnavailable,
    ExtractedPage,
    ExtractionEngine,
    ExtractionResult,
    Inspection,
    PageInspection,
)
from .marker_engine import MarkerEngine
from .pymupdf_engine import PyMuPDFEngine

__all__ = [
    "EngineUnavailable",
    "ExtractedPage",
    "ExtractionEngine",
    "ExtractionResult",
    "Inspection",
    "MarkerEngine",
    "PageInspection",
    "PyMuPDFEngine",
    "get_engine",
]


def get_engine(name: str, config: Config):
    if name in ("pymupdf4llm", "pymupdf", "fast"):
        return PyMuPDFEngine()
    if name == "marker":
        return MarkerEngine(
            executable=config.marker.executable,
            torch_device=config.marker.torch_device,
            timeout_seconds=config.marker.timeout_seconds,
            extra_args=config.marker.extra_args,
        )
    raise EngineUnavailable(f"unknown engine: {name}")
