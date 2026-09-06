"""Greek function names: ημ, συν, εφ.

No OCR model knows this notation, so it arrives either misread from the
handwriting or split into separate Greek variables. Both repairs are
deterministic, and both must leave ordinary Greek prose alone.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from pdf_library.greek_math import (
    RepairReport,
    looks_greek_mathematical,
    repair,
)
from pdf_library.library import Library


def test_misread_cosine_is_recovered() -> None:
    result = repair(r"$\int x \cdot 60vx dx$", allow_cosine=True)
    assert r"\operatorname{συν}" in result.text
    assert "60v" not in result.text


@pytest.mark.parametrize("corrupt", ["60vx", r"60\nu x", "6vx", "60νx", "6ovx"])
def test_every_observed_corruption_form(corrupt: str) -> None:
    assert "συν" in repair(f"${corrupt}$", allow_cosine=True).text


def test_coefficient_before_the_function_is_kept() -> None:
    result = repair("$26vx$", allow_cosine=True)
    assert result.text == r"$2\operatorname{συν}x$"


def test_split_function_name_is_rejoined() -> None:
    result = repair(r"$\eta \mu x$", allow_cosine=True)
    assert result.text == r"$\operatorname{ημ}x$"


def test_unrelated_greek_variables_are_left_alone() -> None:
    source = r"$\alpha \beta \gamma$"
    assert repair(source, allow_cosine=True).text == source


def test_prose_is_never_touched() -> None:
    prose = "Η ημέρα και η εφαρμογή της μεθόδου, σφάλμα στη συνάρτηση."
    assert repair(prose, allow_cosine=True).text == prose


@pytest.mark.parametrize("safe", ["$6$", "$6th$", "$6e$", "$x = 60$"])
def test_plain_numbers_are_not_mistaken_for_cosine(safe: str) -> None:
    assert "συν" not in repair(safe, allow_cosine=True).text


def test_already_marked_operators_are_not_double_wrapped() -> None:
    source = r"$\operatorname{συν}x + \operatorname{ημ}x$"
    assert repair(source, allow_cosine=True).text == source


def test_cosine_repair_is_gated_on_greek_notation() -> None:
    """In a document with no Greek functions the aggressive rule stays off."""
    assert not looks_greek_mathematical("A plain English page about $6v$ olts.")
    assert "συν" not in repair("$6vx$").text


def test_context_detection_finds_greek_notation() -> None:
    assert looks_greek_mathematical(r"$\eta \mu x$")
    assert looks_greek_mathematical("η συνάρτηση συν x")


def test_report_counts_what_it_changed() -> None:
    report: RepairReport = repair(r"$\eta \mu x + 60vx$", allow_cosine=True)
    assert report.counts["misread_cosine"] == 1
    assert report.counts["split_function_name"] == 1
    assert report.total >= 2


def test_repair_document_needs_no_reextraction(
    library: Library, tmp_path: Path
) -> None:
    """Repairing a stored document must not run any engine."""
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "math")
    pdf = tmp_path / "greekmath.pdf"
    doc.save(pdf)
    doc.close()

    result = library.import_pdf(pdf)
    store = library.store(result.document_id)
    store.write_page(1, r"Έστω $\eta \mu x$ και $60vx$ τότε.")
    library.reindex_document(result.document_id)

    runs_before = library.conn.execute("SELECT COUNT(*) FROM engine_runs").fetchone()[0]
    outcome = library.repair_document(result.document_id)
    runs_after = library.conn.execute("SELECT COUNT(*) FROM engine_runs").fetchone()[0]

    assert runs_after == runs_before
    assert outcome["pages_changed"] == 1
    assert r"\operatorname{συν}" in store.read_page(1)


def test_repair_is_idempotent(library: Library, tmp_path: Path) -> None:
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), "math")
    pdf = tmp_path / "idem.pdf"
    doc.save(pdf)
    doc.close()

    result = library.import_pdf(pdf)
    store = library.store(result.document_id)
    store.write_page(1, r"$\eta \mu x + 60vx$")

    library.repair_document(result.document_id)
    once = store.read_page(1)
    assert library.repair_document(result.document_id)["pages_changed"] == 0
    assert store.read_page(1) == once
