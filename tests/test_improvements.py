"""Acceptance tests for deterministic OCR repair and operational hardening."""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from pdf_library.config import Config
from pdf_library.jobs import JobRunner
from pdf_library.library import Library, LibraryError
from pdf_library.quality import assess_page


def test_contradictory_definition_is_flagged_without_rewriting() -> None:
    page = r"$$g(x) = \phi(x) \to g(x) = -\operatorname{συν}x$$"
    quality = assess_page(page)
    assert "contradictory_definition:1" in quality.issues
    assert page == r"$$g(x) = \phi(x) \to g(x) = -\operatorname{συν}x$$"


def test_ordinary_equality_chain_is_not_a_contradictory_definition() -> None:
    page = r"$$a = b, \quad b = c$$"
    assert not any(
        issue.startswith("contradictory_definition")
        for issue in assess_page(page).issues
    )


def test_repair_uses_a_unique_confusion_candidate_and_preserves_math(
    tmp_path: Path,
) -> None:
    from pdf_library.spellfix import repair_greek_prose

    lexicon = tmp_path / "greek.txt"
    lexicon.write_text("παραγοντική\n", encoding="utf-8")
    report = repair_greek_prose(
        "Η παραχουτική μέθοδος έχει $παραχουτική(x)$.", lexicon
    )
    assert report.text == "Η παραγοντική μέθοδος έχει $παραχουτική(x)$."
    assert [(change.original, change.replacement) for change in report.changes] == [
        ("παραχουτική", "παραγοντική")
    ]


def test_repair_leaves_ambiguous_and_missing_lexicon_words_unchanged(
    tmp_path: Path,
) -> None:
    from pdf_library.spellfix import repair_greek_prose

    lexicon = tmp_path / "greek.txt"
    lexicon.write_text("παραγοντική\nπαραχοντική\n", encoding="utf-8")
    ambiguous = repair_greek_prose("παραχουτική", lexicon)
    absent = repair_greek_prose("παραχουτική", tmp_path / "missing.txt")
    assert ambiguous.text == "παραχουτική"
    assert not ambiguous.changes
    assert absent.text == "παραχουτική"
    assert absent.lexicon_available is False


def test_repair_uses_a_full_hunspell_dictionary_when_installed(tmp_path: Path) -> None:
    pytest.importorskip("spylls")
    from pdf_library.spellfix import repair_greek_prose

    stem = tmp_path / "Greek"
    stem.with_suffix(".aff").write_text("SET UTF-8\n", encoding="utf-8")
    stem.with_suffix(".dic").write_text("1\nπαραγοντική\n", encoding="utf-8")
    report = repair_greek_prose("Η παραχουτική.", hunspell_path=stem)
    assert report.text == "Η παραγοντική."


def test_library_repair_returns_an_audit_trail(
    config: Config, greek_pdf: Path, tmp_path: Path
) -> None:
    lexicon = tmp_path / "greek.txt"
    lexicon.write_text("παραγοντική\n", encoding="utf-8")
    config = dataclasses.replace(
        config,
        repair=dataclasses.replace(config.repair, greek_lexicon=str(lexicon)),
    )
    with Library(config) as library:
        document = library.import_pdf(greek_pdf)
        store = library.store(document.document_id)
        store.write_page(1, "Η παραχουτική μέθοδος.")
        result = library.repair_document(document.document_id)

    assert result["changes"] == [
        {"page": 1, "original": "παραχουτική", "replacement": "παραγοντική"}
    ]


def test_repeated_consecutive_headings_are_unique_but_section_stays_grouped(
    library: Library,
) -> None:
    row_id = "a" * 36
    library.conn.execute(
        """
        INSERT INTO documents (id, sha256, filename, title, source_path, created_at, status)
        VALUES (?, ?, 'notes.pdf', 'notes', 'notes.pdf', '2026-01-01T00:00:00+00:00', 'complete')
        """,
        (row_id, "b" * 64),
    )
    store = library.store(row_id)
    store.ensure()
    store.write_page(1, "Βήμα 3 πρώτο σώμα.")
    store.write_page(2, "Βήμα 3 δεύτερο σώμα.")
    store.write_page(3, "Βήμα 3 τρίτο σώμα.")
    library.reindex_document(row_id)

    headings = [
        row[0]
        for row in library.conn.execute(
            "SELECT heading FROM chunks WHERE document_id=? ORDER BY ordinal", (row_id,)
        )
    ]
    assert headings == ["Βήμα 3 (1/3)", "Βήμα 3 (2/3)", "Βήμα 3 (3/3)"]
    section = library.get_section(row_id, "Βήμα 3")
    assert section["content"].count("Βήμα 3") == 3


def test_runner_marks_interrupted_jobs_failed(config: Config) -> None:
    library = Library(config)
    try:
        library.conn.execute(
            "INSERT INTO jobs (id, kind, state, created_at) VALUES "
            "('stale', 'import', 'running', '2026-01-01T00:00:00+00:00')"
        )
    finally:
        library.close()

    runner = JobRunner(config)
    try:
        job = runner.job("stale")
        assert job is not None
        assert job["state"] == "failed"
        assert "server restarted" in job["error"]
    finally:
        runner.shutdown()


def test_job_is_queryable_by_returned_id(config: Config) -> None:
    runner = JobRunner(config)
    try:
        job_id = runner.submit("import", None, lambda _library, _report: {})
        assert runner.job(job_id)["id"] == job_id
        runner.wait(job_id, timeout=5)
        assert runner.job(job_id)["state"] == "complete"
    finally:
        runner.shutdown(wait=True)


@pytest.mark.parametrize("pages", ([0], [-1], [999]))
def test_reprocess_rejects_invalid_pages_before_invoking_engine(
    library: Library, math_pdf: Path, pages: list[int]
) -> None:
    document = library.import_pdf(math_pdf)
    with pytest.raises(LibraryError, match="invalid page"):
        library.upgrade_pages(document.document_id, pages=pages)


def test_status_distinguishes_source_scans_from_pending_ocr(
    library: Library, scanned_pdf: Path
) -> None:
    document = library.import_pdf(scanned_pdf)
    library.conn.execute(
        "UPDATE pages SET ocr_used=1, needs_ocr=0 WHERE document_id=?",
        (document.document_id,),
    )
    status = library.status(document.document_id)
    assert status["source_scanned_pages"] == list(range(1, document.page_count + 1))
    assert status["pending_ocr_pages"] == []
    assert status["ocr_pages"] == list(range(1, document.page_count + 1))


def test_visual_ocr_review_queue_and_verdicts_are_persisted(
    library: Library, scanned_pdf: Path
) -> None:
    document = library.import_pdf(scanned_pdf)
    queue = library.ocr_review_queue(document.document_id)
    assert queue["pages"]
    page = queue["pages"][0]["page"]
    with pytest.raises(LibraryError, match="note is required"):
        library.record_ocr_review(document.document_id, page, "needs_correction")

    library.record_ocr_review(document.document_id, page, "approved")
    status = library.status(document.document_id)
    assert page in status["verification_approved_pages"]
    assert page not in status["verification_pending_pages"]


def test_import_enforces_configured_resource_limits(
    config: Config, math_pdf: Path
) -> None:
    byte_limited = dataclasses.replace(
        config,
        safety=dataclasses.replace(config.safety, max_pdf_bytes=1),
    )
    with Library(byte_limited) as library:
        with pytest.raises(LibraryError, match="bytes; limit"):
            library.import_pdf(math_pdf)

    page_limited = dataclasses.replace(
        config,
        safety=dataclasses.replace(config.safety, max_pdf_pages=1),
    )
    with Library(page_limited) as library:
        with pytest.raises(LibraryError, match="pages; limit"):
            library.import_pdf(math_pdf)
        assert list(library.config.documents_dir.iterdir()) == []
