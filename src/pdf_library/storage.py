"""On-disk layout for a processed document.

    library/documents/<document_id>/
        source.pdf        copy of the original, so the library is self-contained
        document.md       canonical readable Markdown
        metadata.json     everything the database also knows, in plain text
        pages/0001.md     one file per page, for targeted retrieval
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

PAGE_HEADER = "<!-- page {number} -->"


class DocumentStore:
    def __init__(self, root: Path, document_id: str) -> None:
        self.dir = root / document_id
        self.pages_dir = self.dir / "pages"
        self.assets_dir = self.dir / "assets"

    # ------------------------------------------------------------------
    @property
    def source_pdf(self) -> Path:
        return self.dir / "source.pdf"

    @property
    def document_md(self) -> Path:
        return self.dir / "document.md"

    @property
    def metadata_json(self) -> Path:
        return self.dir / "metadata.json"

    def page_path(self, page_number: int) -> Path:
        return self.pages_dir / f"{page_number:04d}.md"

    # ------------------------------------------------------------------
    def ensure(self) -> None:
        self.pages_dir.mkdir(parents=True, exist_ok=True)

    def store_source(self, pdf_path: Path) -> None:
        """Copy the original in, unless it is already the stored copy."""
        self.ensure()
        if self.source_pdf.exists():
            return
        if pdf_path.resolve() == self.source_pdf.resolve():
            return
        shutil.copy2(pdf_path, self.source_pdf)

    def write_page(self, page_number: int, markdown: str) -> Path:
        self.ensure()
        path = self.page_path(page_number)
        _atomic_write(path, markdown)
        return path

    def read_page(self, page_number: int) -> str | None:
        path = self.page_path(page_number)
        if not path.is_file():
            return None
        return path.read_text(encoding="utf-8")

    def read_pages(self) -> dict[int, str]:
        out: dict[int, str] = {}
        if not self.pages_dir.is_dir():
            return out
        for path in sorted(self.pages_dir.glob("*.md")):
            try:
                number = int(path.stem)
            except ValueError:
                continue
            out[number] = path.read_text(encoding="utf-8")
        return out

    def rebuild_document_md(self, title: str | None = None) -> Path:
        """Join the page files into the canonical document.

        Page markers are HTML comments so they stay invisible in a renderer
        but still let a reader (or a future importer) find page boundaries.
        """
        pages = self.read_pages()
        parts: list[str] = []
        if title:
            parts.append(f"# {title}\n")
        for number in sorted(pages):
            parts.append(PAGE_HEADER.format(number=number))
            parts.append(pages[number].strip())
        _atomic_write(self.document_md, "\n\n".join(parts).strip() + "\n")
        return self.document_md

    def write_metadata(self, data: dict[str, Any]) -> Path:
        self.ensure()
        _atomic_write(
            self.metadata_json,
            json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        )
        return self.metadata_json

    def read_metadata(self) -> dict[str, Any]:
        if not self.metadata_json.is_file():
            return {}
        try:
            return json.loads(self.metadata_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def delete(self) -> None:
        shutil.rmtree(self.dir, ignore_errors=True)


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temporary file so a crash cannot leave a half page behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
