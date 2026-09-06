"""The quality gate and the OCR policy."""

from __future__ import annotations

from pathlib import Path

from pdf_library.library import Library
from pdf_library.quality import assess_page


def test_clean_page_scores_well() -> None:
    quality = assess_page("# Title\n\nA clean paragraph of ordinary readable prose.")
    assert quality.state == "good"
    assert quality.score >= 0.75


def test_empty_page_is_bad() -> None:
    quality = assess_page("   \n  ")
    assert quality.state == "bad"
    assert "empty_page" in quality.issues
    assert not quality.has_text


def test_garbled_page_is_penalised() -> None:
    quality = assess_page("Some text " + "�" * 40)
    assert quality.state in ("warning", "bad")
    assert any(issue.startswith("garbled_chars") for issue in quality.issues)


def test_digital_pdf_needs_no_ocr(library: Library, math_pdf: Path) -> None:
    result = library.import_pdf(math_pdf)
    assert result.kind == "digital_text"
    assert result.scanned_pages == []


def test_scanned_pdf_is_recognised(library: Library, scanned_pdf: Path) -> None:
    result = library.import_pdf(scanned_pdf)
    assert result.kind == "scanned"
    assert result.scanned_pages


def test_scanned_pages_are_flagged_for_upgrade(
    library: Library, scanned_pdf: Path
) -> None:
    result = library.import_pdf(scanned_pdf)
    assert set(result.scanned_pages) <= set(result.upgrade_candidates)


def test_no_ocr_is_performed_on_import(library: Library, scanned_pdf: Path) -> None:
    """Import must stay cheap: a scan is reported, not OCR'd."""
    result = library.import_pdf(scanned_pdf)
    ocr_used = library.conn.execute(
        "SELECT COALESCE(SUM(ocr_used),0) FROM pages WHERE document_id = ?",
        (result.document_id,),
    ).fetchone()[0]
    assert ocr_used == 0


def test_page_report_lists_issues(library: Library, scanned_pdf: Path) -> None:
    result = library.import_pdf(scanned_pdf)
    rows = library.page_report(result.document_id)
    assert rows and all("state" in row for row in rows)


def test_dropped_display_math_is_detected(library: Library, math_pdf: Path) -> None:
    """The fast engine loses display equations; the gate must notice.

    The signal is the page's fonts, not its text: a page typeset with TeX's
    large-operator fonts that yields no `$$` block lost its mathematics.
    """
    result = library.import_pdf(math_pdf)
    rows = library.page_report(result.document_id)
    assert any("display_math_missing" in row["issues"] for row in rows)
    assert set(result.upgrade_candidates) >= {
        row["page"] for row in rows if "display_math_missing" in row["issues"]
    }


def test_slides_without_tex_math_are_not_flagged(library: Library) -> None:
    """A page with no math fonts must never be accused of losing equations."""
    from pdf_library.engines import PyMuPDFEngine

    inspection = PyMuPDFEngine().inspect(Path("tests/fixtures/scanned.pdf"))
    assert not any(page.math_fonts for page in inspection.pages)


def test_language_is_judged_on_prose_not_latex() -> None:
    """LaTeX is written in Latin letters and must not outvote the prose."""
    from pdf_library.engines.base import ExtractedPage, ExtractionResult

    def result(text: str) -> ExtractionResult:
        return ExtractionResult("x", "1", [ExtractedPage(1, text)])

    greek = (
        r"Εφαρμόζουμε $\operatorname{συν}x$ και $$\int \frac{x}{2}\,dx$$ "
        r"στην παράγουσα συνάρτηση παρακάτω."
    )
    assert Library._is_greek(result(greek))
    assert not Library._is_greek(result("The dominated convergence theorem."))
    assert not Library._is_greek(result(""))
