"""Search has to work in Greek and English, and stay narrow."""

from __future__ import annotations

from pathlib import Path

import pytest

from pdf_library.library import Library, LibraryError
from pdf_library.normalize import normalize_for_index, normalize_query


def test_keyword_search(library: Library, math_pdf: Path) -> None:
    library.import_pdf(math_pdf)
    assert library.search("convergence")


def test_phrase_search(library: Library, math_pdf: Path) -> None:
    library.import_pdf(math_pdf)
    assert library.search('"dominated convergence"')


def test_greek_search_ignores_accents(library: Library, greek_pdf: Path) -> None:
    library.import_pdf(greek_pdf)
    accented = library.search("σύγκλισης")
    bare = library.search("συγκλισης")
    assert accented and bare
    assert {r["chunk_id"] for r in accented} == {r["chunk_id"] for r in bare}


def test_final_sigma_matches_medial() -> None:
    assert normalize_for_index("συνάρτησης") == normalize_query("ΣΥΝΆΡΤΗΣΗΣ")


def test_latex_does_not_pollute_the_index() -> None:
    indexed = normalize_for_index(r"Let $\int_{-\infty}^{\infty} f(x)dx$ converge.")
    assert "$" not in indexed and "\\" not in indexed
    assert "converge" in indexed


def test_document_filter(library: Library, math_pdf: Path, greek_pdf: Path) -> None:
    math = library.import_pdf(math_pdf)
    library.import_pdf(greek_pdf)
    results = library.search("a", document_id=math.document_id)
    assert all(r["document_id"] == math.document_id for r in results)


def test_page_filter(library: Library, math_pdf: Path) -> None:
    library.import_pdf(math_pdf)
    results = library.search("matrix", pages=(2, 2))
    assert all(r["pages"][0] <= 2 <= r["pages"][1] for r in results)


def test_limit_is_respected(library: Library, math_pdf: Path) -> None:
    library.import_pdf(math_pdf)
    assert len(library.search("the", limit=1)) <= 1


def test_results_carry_snippets_not_full_text(library: Library, math_pdf: Path) -> None:
    library.import_pdf(math_pdf)
    limit = library.config.response.max_snippet_chars
    for result in library.search("convergence"):
        assert len(result["snippet"]) <= limit + 4  # room for the ellipsis


def test_query_operators_do_not_crash_search(library: Library, math_pdf: Path) -> None:
    library.import_pdf(math_pdf)
    for query in ["(", "AND", "a OR", '"unterminated', "NEAR("]:
        library.search(query)  # must not raise


def test_missing_document_raises(library: Library) -> None:
    with pytest.raises(LibraryError):
        library.get_pages("nope", [1])


def test_section_retrieval(library: Library, math_pdf: Path) -> None:
    result = library.import_pdf(math_pdf)
    section = library.get_section(result.document_id, "Convergence")
    assert "Convergence" in section["heading"]
    assert section["content"]


def test_greek_inflection_is_stemmed_away() -> None:
    """Greek declines heavily; "κλάσματα" must find "κλασμάτων"."""
    assert normalize_query("μερικά κλάσματα") == normalize_for_index(
        "μερικών κλασμάτων"
    )


def test_greek_search_across_cases(library: Library, greek_pdf: Path) -> None:
    library.import_pdf(greek_pdf)
    nominative = library.search("ακολουθία")
    genitive = library.search("ακολουθίας")
    assert nominative
    assert {r["chunk_id"] for r in nominative} == {r["chunk_id"] for r in genitive}


def test_stemming_leaves_latin_alone() -> None:
    from pdf_library.greek import stem_word

    for word in ("quicksort", "convergence", "theorem", "dominated"):
        assert stem_word(word) == word


def test_short_greek_words_are_not_mangled() -> None:
    from pdf_library.greek import stem_word

    for word in ("και", "το", "τα", "της"):
        assert stem_word(word) == word


def test_reindex_all_marks_the_index_current(library: Library, math_pdf: Path) -> None:
    library.import_pdf(math_pdf)
    library.conn.execute("DELETE FROM meta WHERE key = 'index_version'")
    assert library.index_is_stale()
    library.reindex_all()
    assert not library.index_is_stale()


def test_a_fresh_library_is_never_stale(library: Library) -> None:
    assert not library.index_is_stale()
