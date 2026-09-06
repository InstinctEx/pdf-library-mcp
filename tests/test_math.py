"""Mathematics is the point of the library, so it gets its own checks."""

from __future__ import annotations

from pathlib import Path

from pdf_library.chunking import chunk_pages
from pdf_library.library import Library
from pdf_library.mathfix import repair_html_scripts
from pdf_library.quality import assess_page, count_math, looks_mathematical


def test_math_pages_are_detected(library: Library, math_pdf: Path) -> None:
    result = library.import_pdf(math_pdf)
    assert result.math_pages, "a page of integrals must register as mathematical"
    assert result.equation_count > 0


def test_greek_prose_is_not_mistaken_for_math() -> None:
    prose = "Η συνάρτηση είναι συνεχής παντού στο διάστημα."
    assert not looks_mathematical(prose, greek_document=True)


def test_greek_document_still_detects_real_math() -> None:
    assert looks_mathematical("Η σειρά $\\sum_{n=1}^{\\infty} a_n$", greek_document=True)


def test_display_equation_is_never_split() -> None:
    page = (
        "## Theorem\n\nStatement.\n\n$$\n\\begin{aligned}\n"
        + "a &= b \\\\\n" * 60
        + "\\end{aligned}\n$$\n\nAfter.\n"
    )
    chunks = chunk_pages({1: page}, target_chars=100, max_chars=200)
    joined = "\n".join(c.content for c in chunks)
    assert joined.count("$$") % 2 == 0
    holder = [c for c in chunks if "\\begin{aligned}" in c.content]
    assert len(holder) == 1
    assert "\\end{aligned}" in holder[0].content


def test_unbalanced_delimiters_are_flagged() -> None:
    assert "unbalanced_math_delimiters" in assess_page("text $a + b more text").issues


def test_escaped_dollar_is_not_math() -> None:
    quality = assess_page("The book costs \\$40 and the shipping \\$5.")
    assert "unbalanced_math_delimiters" not in quality.issues


def test_counts_inline_and_display() -> None:
    assert count_math("$a$ and $$b$$ and $c$") == 3


def test_superscript_repair_produces_latex() -> None:
    assert repair_html_scripts("O(n<sup>2</sup>)") == "O($n^{2}$)"


def test_superscript_repair_leaves_prose_alone() -> None:
    text = "footnote<sup>a long explanatory sentence here</sup>"
    assert repair_html_scripts(text) == text


def test_theorem_and_proof_are_classified(library: Library, math_pdf: Path) -> None:
    result = library.import_pdf(math_pdf)
    types = {
        row[0]
        for row in library.conn.execute(
            "SELECT chunk_type FROM chunks WHERE document_id = ?",
            (result.document_id,),
        )
    }
    assert {"theorem", "proof"} & types
