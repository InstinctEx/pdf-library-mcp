"""The central promise: a PDF is processed once and never again."""

from __future__ import annotations

import shutil
from pathlib import Path

from pdf_library.library import Library


def test_first_import_processes(library: Library, math_pdf: Path) -> None:
    result = library.import_pdf(math_pdf)
    assert not result.cache_hit
    assert result.page_count == 2
    assert result.chunk_count > 0


def test_second_import_is_a_cache_hit(library: Library, math_pdf: Path) -> None:
    first = library.import_pdf(math_pdf)
    second = library.import_pdf(math_pdf)
    assert second.cache_hit
    assert second.document_id == first.document_id


def test_cache_hit_runs_no_engine(library: Library, math_pdf: Path) -> None:
    library.import_pdf(math_pdf)
    runs_before = library.conn.execute("SELECT COUNT(*) FROM engine_runs").fetchone()[0]
    library.import_pdf(math_pdf)
    runs_after = library.conn.execute("SELECT COUNT(*) FROM engine_runs").fetchone()[0]
    assert runs_after == runs_before


def test_import_is_idempotent(library: Library, math_pdf: Path) -> None:
    for _ in range(5):
        library.import_pdf(math_pdf)
    assert len(library.list_documents()) == 1


def test_same_content_different_name_is_one_document(
    library: Library, math_pdf: Path, tmp_path: Path
) -> None:
    copy = tmp_path / "renamed.pdf"
    shutil.copy2(math_pdf, copy)
    first = library.import_pdf(math_pdf)
    second = library.import_pdf(copy)
    assert second.cache_hit
    assert second.document_id == first.document_id


def test_force_reprocesses(library: Library, math_pdf: Path) -> None:
    library.import_pdf(math_pdf)
    forced = library.import_pdf(math_pdf, force=True)
    assert not forced.cache_hit
    assert library.conn.execute("SELECT COUNT(*) FROM engine_runs").fetchone()[0] == 2


def test_different_content_is_a_different_document(
    library: Library, math_pdf: Path, greek_pdf: Path
) -> None:
    a = library.import_pdf(math_pdf)
    b = library.import_pdf(greek_pdf)
    assert a.document_id != b.document_id
    assert len(library.list_documents()) == 2


def test_reindex_is_idempotent(library: Library, math_pdf: Path) -> None:
    result = library.import_pdf(math_pdf)
    counts = {library.reindex_document(result.document_id) for _ in range(3)}
    assert counts == {result.chunk_count}
    # The FTS index must stay consistent with the chunks table.
    library.conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('integrity-check')")
