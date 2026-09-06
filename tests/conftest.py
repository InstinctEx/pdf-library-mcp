from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from pdf_library.config import Config
from pdf_library.library import Library

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return dataclasses.replace(Config(), root=tmp_path / "library")


@pytest.fixture
def library(config: Config) -> Library:
    lib = Library(config)
    yield lib
    lib.close()


@pytest.fixture
def math_pdf() -> Path:
    path = FIXTURES / "math.pdf"
    if not path.is_file():
        pytest.skip("run tests/fixtures/make_fixtures.py first")
    return path


@pytest.fixture
def greek_pdf() -> Path:
    path = FIXTURES / "greek.pdf"
    if not path.is_file():
        pytest.skip("run tests/fixtures/make_fixtures.py first")
    return path


@pytest.fixture
def scanned_pdf() -> Path:
    path = FIXTURES / "scanned.pdf"
    if not path.is_file():
        pytest.skip("run tests/fixtures/make_fixtures.py first")
    return path
