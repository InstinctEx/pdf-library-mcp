"""Fast extraction path, built on PyMuPDF / pymupdf4llm.

This engine is deliberately the one that always runs first: it is local, needs
no models, and turns a 500-page book into searchable Markdown in seconds. It
reads a page's existing text layer -- including an OCR layer someone else put
there -- but it does not perform OCR itself and it does not produce LaTeX.
Pages where that matters are flagged for the quality engine.
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import pymupdf
import pymupdf4llm

from ..mathfix import repair_html_scripts
from .base import (
    ExtractedPage,
    ExtractionResult,
    Inspection,
    PageInspection,
)

# Below this many characters a page with images is almost certainly a scan.
_SCAN_CHAR_THRESHOLD = 60

# Fonts that only get used for typeset mathematics. CMEX carries TeX's large
# operators and extensible delimiters, so it is near-proof of a display
# equation; the AMS symbol fonts and any OpenType math font are equally telling.
# CMMI and CMSY are deliberately excluded: a single italic variable pulls them
# in, so they say nothing about display math.
_MATH_FONT = re.compile(
    r"(CMEX|MSAM|MSBM|RSFS|EUFM|EUSM|STIXMath|LatinModernMath|XITSMath"
    r"|CambriaMath|AsanaMath|TeXGyre\w*Math|[-+]Math\b)",
    re.IGNORECASE,
)


class PyMuPDFEngine:
    name = "pymupdf4llm"
    quality_tier = "fast"

    def available(self) -> tuple[bool, str]:
        return True, f"pymupdf {pymupdf.version[0]}"

    def version(self) -> str:
        return str(pymupdf.version[0])

    def inspect(self, pdf_path: Path) -> Inspection:
        pages: list[PageInspection] = []
        title = None
        with pymupdf.open(pdf_path) as doc:
            meta = doc.metadata or {}
            title = (meta.get("title") or "").strip() or None
            for index, page in enumerate(doc, start=1):
                text = page.get_text("text") or ""
                chars = len(text.strip())
                has_images = bool(page.get_images(full=False))
                scanned = chars < _SCAN_CHAR_THRESHOLD and has_images
                fonts = " ".join(font[3] for font in page.get_fonts(full=False))
                math_fonts = bool(_MATH_FONT.search(fonts))
                pages.append(
                    PageInspection(
                        page_number=index,
                        native_chars=chars,
                        has_images=has_images,
                        is_scanned=scanned,
                        # A page whose text sits in an invisible render mode is
                        # an OCR layer laid over a scan.
                        ocr_layer=chars > 0 and has_images and chars < 400,
                        math_fonts=math_fonts,
                    )
                )

        scanned_pages = [p.page_number for p in pages if p.is_scanned]
        total = len(pages) or 1
        if len(scanned_pages) >= total * 0.8:
            kind = "scanned"
        elif scanned_pages:
            kind = "mixed"
        else:
            kind = "digital_text"

        return Inspection(
            page_count=len(pages),
            pages=pages,
            kind=kind,
            scanned_pages=scanned_pages,
            title=title,
        )

    def extract(
        self, pdf_path: Path, pages: list[int] | None = None
    ) -> ExtractionResult:
        started = time.perf_counter()
        # pymupdf4llm takes 0-based page numbers; everything else here is 1-based.
        zero_based = [p - 1 for p in pages] if pages else None

        with pymupdf.open(pdf_path) as doc:
            results = pymupdf4llm.to_markdown(
                doc,
                pages=zero_based,
                page_chunks=True,
                table_strategy="lines_strict",
                show_progress=False,
                ignore_images=True,
                ignore_graphics=False,
                force_text=True,
            )

        extracted: list[ExtractedPage] = []
        for item in results:
            metadata = item.get("metadata") or {}
            # metadata["page"] is 1-based in pymupdf4llm's page chunks.
            number = int(metadata.get("page", len(extracted) + 1))
            # Superscripts arrive as HTML tags; turn the simple ones into
            # inline LaTeX so the page is both readable and math-detectable.
            markdown = repair_html_scripts((item.get("text") or "").strip())
            extracted.append(ExtractedPage(page_number=number, markdown=markdown))

        extracted.sort(key=lambda p: p.page_number)
        return ExtractionResult(
            engine=self.name,
            engine_version=self.version(),
            pages=extracted,
            duration_s=time.perf_counter() - started,
        )
