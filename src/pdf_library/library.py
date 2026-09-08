"""The library: import, cache, index, search and retrieve.

Everything the CLI and the MCP server do goes through this class. It owns the
database connection and the on-disk document store, and it is the only place
that decides whether work can be skipped.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .chunking import Chunk, chunk_pages
from .config import Config, load_config
from .db import INDEX_VERSION, SCHEMA_VERSION, init_db, rebuild_index
from .engines import EngineUnavailable, get_engine
from .engines.base import ExtractedPage, ExtractionResult
from .hashing import document_id as make_document_id
from .greek_math import looks_greek_mathematical
from .greek_math import repair as repair_greek_math
from .hashing import file_sha256, text_sha256
from .normalize import normalize_for_index, normalize_query, trigrams
from .quality import assess_page, looks_mathematical
from .rendering import RenderedRegion, render_region
from .storage import DocumentStore
from .spellfix import repair_greek_prose
from .textfix import rejoin_hyphenation
from .tokens import estimate_tokens
from .vision import VisionEngine

ProgressFn = Callable[[float, str], None]

# Display and inline math, removed before judging what language a page is in.
_MATH_SPAN = re.compile(r"\$\$.+?\$\$|(?<!\\)\$.+?(?<!\\)\$", re.DOTALL)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _noop(progress: float, detail: str) -> None:  # pragma: no cover - default
    return None


class LibraryError(RuntimeError):
    pass


@dataclass
class ImportResult:
    document_id: str
    filename: str
    title: str | None
    page_count: int
    cache_hit: bool
    engine: str
    quality_tier: str
    kind: str
    duration_s: float
    math_pages: list[int]
    scanned_pages: list[int]
    low_quality_pages: list[int]
    equation_count: int
    table_count: int
    chunk_count: int
    upgrade_candidates: list[int]


class Library:
    def __init__(self, config: Config | None = None) -> None:
        self.config = config or load_config()
        self.config.ensure_dirs()
        self.conn: sqlite3.Connection = init_db(self.config.db_path)

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "Library":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # lookup helpers
    # ------------------------------------------------------------------
    def store(self, document_id: str) -> DocumentStore:
        return DocumentStore(self.config.documents_dir, document_id)

    def document_by_hash(self, sha256: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM documents WHERE sha256 = ?", (sha256,)
        ).fetchone()

    def document(self, document_id: str) -> sqlite3.Row | None:
        row = self.conn.execute(
            "SELECT * FROM documents WHERE id = ?", (document_id,)
        ).fetchone()
        if row is not None:
            return row
        # Accept an unambiguous id prefix, which is far easier to type.
        rows = self.conn.execute(
            "SELECT * FROM documents WHERE id LIKE ? LIMIT 2", (f"{document_id}%",)
        ).fetchall()
        if len(rows) == 1:
            return rows[0]
        return None

    def resolve(self, document_ref: str) -> sqlite3.Row:
        """Resolve an id, id prefix, sha256 or filename to one document."""
        row = self.document(document_ref)
        if row is not None:
            return row
        row = self.document_by_hash(document_ref)
        if row is not None:
            return row
        rows = self.conn.execute(
            "SELECT * FROM documents WHERE filename = ? OR title = ? LIMIT 2",
            (document_ref, document_ref),
        ).fetchall()
        if len(rows) == 1:
            return rows[0]
        if len(rows) > 1:
            raise LibraryError(f"ambiguous document reference: {document_ref!r}")
        rows = self.conn.execute(
            "SELECT * FROM documents WHERE filename LIKE ? LIMIT 5",
            (f"%{document_ref}%",),
        ).fetchall()
        if len(rows) == 1:
            return rows[0]
        if len(rows) > 1:
            names = ", ".join(r["filename"] for r in rows)
            raise LibraryError(f"ambiguous document reference {document_ref!r}: {names}")
        raise LibraryError(f"no such document: {document_ref!r}")

    def list_documents(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM documents ORDER BY created_at DESC"
        ).fetchall()
        return [self._document_summary(r) for r in rows]

    # ------------------------------------------------------------------
    # import
    # ------------------------------------------------------------------
    def import_pdf(
        self,
        pdf_path: Path,
        force: bool = False,
        progress: ProgressFn = _noop,
    ) -> ImportResult:
        """Import a PDF, skipping all work when its content hash is known.

        The fast engine always runs first so the document becomes searchable
        immediately; pages that would benefit from the quality engine are
        reported as upgrade candidates rather than processed here.
        """
        pdf_path = Path(pdf_path).expanduser().resolve()
        if not pdf_path.is_file():
            raise LibraryError(f"not a file: {pdf_path}")
        if pdf_path.suffix.lower() != ".pdf":
            raise LibraryError(f"not a PDF: {pdf_path.name}")
        max_bytes = self.config.safety.max_pdf_bytes
        if max_bytes and pdf_path.stat().st_size > max_bytes:
            raise LibraryError(
                f"PDF is {pdf_path.stat().st_size} bytes; limit is {max_bytes} bytes"
            )

        started = time.perf_counter()
        progress(0.02, "hashing")
        sha256 = file_sha256(pdf_path)

        existing = self.document_by_hash(sha256)
        if existing is not None and not force and existing["status"] == "complete":
            return self._cache_hit_result(existing, time.perf_counter() - started)

        engine = get_engine(self.config.extraction.fast_engine, self.config)
        progress(0.08, "inspecting")
        # Inspect the caller's file before copying it into the persistent
        # store, so a page-limit rejection leaves no orphaned library data.
        inspection = engine.inspect(pdf_path)
        max_pages = self.config.safety.max_pdf_pages
        if max_pages and inspection.page_count > max_pages:
            raise LibraryError(
                f"PDF has {inspection.page_count} pages; limit is {max_pages} pages"
            )

        document_id = existing["id"] if existing else make_document_id(sha256)
        store = self.store(document_id)
        store.ensure()
        store.store_source(pdf_path)

        title = inspection.title or pdf_path.stem
        self.conn.execute(
            """
            INSERT INTO documents (
                id, sha256, filename, title, source_path, page_count, status,
                kind, size_bytes, created_at, engine, engine_version,
                quality_tier, schema_version
            ) VALUES (?,?,?,?,?,?,'processing',?,?,?,?,?,'fast',?)
            ON CONFLICT(id) DO UPDATE SET
                status='processing', kind=excluded.kind,
                page_count=excluded.page_count, error=NULL
            """,
            (
                document_id,
                sha256,
                pdf_path.name,
                title,
                str(pdf_path),
                inspection.page_count,
                inspection.kind,
                pdf_path.stat().st_size,
                _now(),
                engine.name,
                engine.version(),
                SCHEMA_VERSION,
            ),
        )

        progress(0.15, f"extracting {inspection.page_count} pages")
        try:
            result = engine.extract(store.source_pdf)
        except Exception as exc:
            self.conn.execute(
                "UPDATE documents SET status='failed', error=? WHERE id=?",
                (str(exc), document_id),
            )
            raise

        self._record_run(document_id, result, pages=None)
        scanned = set(inspection.scanned_pages)
        math_font_pages = {p.page_number for p in inspection.pages if p.math_fonts}
        stats = self._store_pages(
            document_id=document_id,
            store=store,
            result=result,
            scanned_pages=scanned,
            math_font_pages=math_font_pages,
            progress=progress,
            progress_span=(0.2, 0.75),
        )

        progress(0.8, "indexing")
        chunk_count = self.reindex_document(document_id)
        store.rebuild_document_md(title=title)

        self.conn.execute(
            """
            UPDATE documents
               SET status='complete', processed_at=?, quality_tier='fast',
                   page_count=?, language=?
             WHERE id=?
            """,
            (_now(), max(inspection.page_count, len(result.pages)),
             stats["language"], document_id),
        )

        duration = time.perf_counter() - started
        upgrade_candidates = self._upgrade_candidates(document_id)
        row = self.document(document_id)
        assert row is not None

        self._write_metadata(row, stats, chunk_count, duration)
        progress(1.0, "done")

        return ImportResult(
            document_id=document_id,
            filename=pdf_path.name,
            title=title,
            page_count=row["page_count"],
            cache_hit=False,
            engine=engine.name,
            quality_tier="fast",
            kind=inspection.kind,
            duration_s=round(duration, 2),
            math_pages=stats["math_pages"],
            scanned_pages=sorted(scanned),
            low_quality_pages=stats["low_quality_pages"],
            equation_count=stats["equation_count"],
            table_count=stats["table_count"],
            chunk_count=chunk_count,
            upgrade_candidates=upgrade_candidates,
        )

    # ------------------------------------------------------------------
    def _cache_hit_result(self, row: sqlite3.Row, duration: float) -> ImportResult:
        stats = self.conn.execute(
            """
            SELECT COALESCE(SUM(math_count),0) AS eq,
                   COALESCE(SUM(table_count),0) AS tbl
              FROM pages WHERE document_id = ?
            """,
            (row["id"],),
        ).fetchone()
        chunk_count = self.conn.execute(
            "SELECT COUNT(*) FROM chunks WHERE document_id = ?", (row["id"],)
        ).fetchone()[0]
        return ImportResult(
            document_id=row["id"],
            filename=row["filename"],
            title=row["title"],
            page_count=row["page_count"],
            cache_hit=True,
            engine=row["engine"] or "",
            quality_tier=row["quality_tier"],
            kind=row["kind"] or "",
            duration_s=round(duration, 3),
            math_pages=self._pages_where("math_count > 0", row["id"]),
            scanned_pages=self._pages_where("needs_ocr = 1", row["id"]),
            low_quality_pages=self._pages_where(
                "quality_state IN ('warning','bad')", row["id"]
            ),
            equation_count=stats["eq"],
            table_count=stats["tbl"],
            chunk_count=chunk_count,
            upgrade_candidates=self._upgrade_candidates(row["id"]),
        )

    def _pages_where(self, condition: str, document_id: str) -> list[int]:
        rows = self.conn.execute(
            f"SELECT page_number FROM pages WHERE document_id = ? AND {condition} "
            "ORDER BY page_number",
            (document_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def _record_run(
        self, document_id: str, result: ExtractionResult, pages: list[int] | None
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO engine_runs
                (document_id, engine, engine_version, pages, duration_s, ok, created_at)
            VALUES (?,?,?,?,?,1,?)
            """,
            (
                document_id,
                result.engine,
                result.engine_version,
                json.dumps(pages) if pages else None,
                round(result.duration_s, 3),
                _now(),
            ),
        )

    def _store_pages(
        self,
        document_id: str,
        store: DocumentStore,
        result: ExtractionResult,
        scanned_pages: set[int],
        math_font_pages: set[int] | None = None,
        progress: ProgressFn = _noop,
        progress_span: tuple[float, float] = (0.0, 1.0),
        quality_tier: str = "fast",
    ) -> dict[str, Any]:
        """Write page Markdown to disk and upsert the matching page rows."""
        greek_document = self._is_greek(result)
        math_font_pages = math_font_pages or set()
        # Greek function names (ημ, συν, εφ) are unknown to every OCR model, so
        # they arrive misread or split into separate variables. The repair is
        # deterministic but only safe in a document that uses that notation, so
        # decide once from the whole document rather than page by page.
        repair_math = greek_document and looks_greek_mathematical(
            "".join(page.markdown for page in result.pages)
        )
        lexicon = (
            Path(self.config.repair.greek_lexicon).expanduser()
            if greek_document and self.config.repair.greek_lexicon
            else None
        )
        hunspell = self._greek_hunspell_path() if greek_document else None
        repairs: dict[str, int] = {}
        math_pages: list[int] = []
        low_quality: list[int] = []
        equation_count = 0
        table_count = 0
        low, high = progress_span
        total = max(len(result.pages), 1)

        for index, page in enumerate(result.pages, start=1):
            # Hyphenation repair is language-independent and purely mechanical,
            # so it always runs.
            joined = rejoin_hyphenation(page.markdown)
            page.markdown = joined.text
            for key, value in joined.counts.items():
                repairs[key] = repairs.get(key, 0) + value

            if greek_document:
                prose = repair_greek_prose(page.markdown, lexicon, hunspell)
                page.markdown = prose.text
                for key, value in prose.counts.items():
                    repairs[key] = repairs.get(key, 0) + value

            if repair_math:
                report = repair_greek_math(page.markdown, allow_cosine=True)
                page.markdown = report.text
                for key, value in report.counts.items():
                    repairs[key] = repairs.get(key, 0) + value

            quality = assess_page(page.markdown)
            is_scanned = page.page_number in scanned_pages
            # An engine that tells us how it read the page is believed; only
            # when it says nothing do we fall back to inferring from the scan
            # inspection.
            needs_ocr = is_scanned and not quality.has_text and not page.ocr_used
            mathematical = looks_mathematical(page.markdown, greek_document)

            # A page typeset with TeX's large-operator and extensible-delimiter
            # fonts, yet with no display equation in the extracted text, means
            # the extractor silently dropped the display math. It is the single
            # most damaging failure for this library and it is invisible to any
            # check that only reads the output.
            if page.page_number in math_font_pages and quality.display_math_count == 0:
                quality.issues.append("display_math_missing")
                quality.score = round(min(quality.score, 0.4), 3)
                quality.state = "bad" if quality.score < 0.45 else "warning"
                mathematical = True

            store.write_page(page.page_number, page.markdown)
            self._store_blocks(document_id, page)
            self.conn.execute(
                """
                INSERT INTO pages (
                    document_id, page_number, page_hash, char_count, engine,
                    quality_tier, ocr_used, ocr_reason, needs_ocr, source_scanned,
                    verification_state,
                    quality_score,
                    quality_state, quality_issues, math_count, table_count, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(document_id, page_number) DO UPDATE SET
                    page_hash=excluded.page_hash, char_count=excluded.char_count,
                    engine=excluded.engine, quality_tier=excluded.quality_tier,
                    ocr_used=excluded.ocr_used, ocr_reason=excluded.ocr_reason,
                    needs_ocr=excluded.needs_ocr,
                    source_scanned=excluded.source_scanned,
                    verification_state=CASE
                        WHEN pages.page_hash IS NOT excluded.page_hash
                         AND (excluded.source_scanned=1 OR excluded.ocr_used=1)
                        THEN 'pending'
                        WHEN excluded.source_scanned=0 AND excluded.ocr_used=0
                        THEN 'not_needed'
                        ELSE pages.verification_state
                    END,
                    verification_note=CASE
                        WHEN pages.page_hash IS NOT excluded.page_hash
                        THEN NULL ELSE pages.verification_note END,
                    verified_at=CASE
                        WHEN pages.page_hash != excluded.page_hash
                        THEN NULL ELSE pages.verified_at END,
                    quality_score=excluded.quality_score,
                    quality_state=excluded.quality_state,
                    quality_issues=excluded.quality_issues,
                    math_count=excluded.math_count, table_count=excluded.table_count,
                    updated_at=excluded.updated_at
                """,
                (
                    document_id,
                    page.page_number,
                    text_sha256(page.markdown),
                    quality.char_count,
                    result.engine,
                    quality_tier,
                    int(page.ocr_used),
                    page.ocr_reason,
                    int(needs_ocr),
                    int(is_scanned),
                    "pending" if is_scanned or page.ocr_used else "not_needed",
                    quality.score,
                    quality.state,
                    json.dumps(quality.issues, ensure_ascii=False),
                    quality.math_count,
                    quality.table_count,
                    _now(),
                ),
            )

            if mathematical:
                math_pages.append(page.page_number)
            if quality.state != "good":
                low_quality.append(page.page_number)
            equation_count += quality.math_count
            table_count += quality.table_count

            if index % 25 == 0 or index == total:
                progress(low + (high - low) * index / total, f"page {index}/{total}")

        return {
            "math_pages": math_pages,
            "low_quality_pages": low_quality,
            "equation_count": equation_count,
            "table_count": table_count,
            "language": "ell" if greek_document else "eng",
            "greek_math_repairs": repairs,
        }

    def _store_blocks(self, document_id: str, page: ExtractedPage) -> None:
        """Replace the stored blocks for one page."""
        self.conn.execute(
            "DELETE FROM blocks WHERE document_id = ? AND page_number = ?",
            (document_id, page.page_number),
        )
        if not page.blocks:
            return
        self.conn.executemany(
            """
            INSERT INTO blocks (
                document_id, page_number, ordinal, block_type,
                x0, y0, x1, y1, page_width, page_height, char_count
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (
                    document_id,
                    page.page_number,
                    ordinal,
                    block.block_type,
                    *(block.bbox or (None, None, None, None)),
                    page.page_width or None,
                    page.page_height or None,
                    len(block.markdown),
                )
                for ordinal, block in enumerate(page.blocks)
            ],
        )

    @staticmethod
    def _is_greek(result: ExtractionResult) -> bool:
        r"""Decide the document's language from its prose, not its formulas.

        LaTeX is written in Latin letters -- ``\operatorname``, ``\int``,
        ``aligned`` -- and a mathematical page carries enough of it to drown
        out the Greek around it, so math spans are removed before counting.
        """
        sample = "".join(p.markdown for p in result.pages[:20])[:40000]
        if not sample:
            return False
        prose = _MATH_SPAN.sub(" ", sample)
        return Library._text_is_greek(prose)

    @staticmethod
    def _text_is_greek(text: str) -> bool:
        """Whether prose is predominately Greek after callers remove math."""
        if not text:
            return False
        greek = sum(1 for ch in text if "Ͱ" <= ch <= "Ͽ" or "ἀ" <= ch <= "῿")
        letters = sum(1 for ch in text if ch.isalpha())
        return letters > 0 and greek / letters > 0.3

    def _greek_hunspell_path(self) -> Path | None:
        """Locate the optional local Greek Hunspell dictionary."""
        configured = self.config.repair.greek_hunspell
        path = (
            Path(configured).expanduser()
            if configured
            else self.config.root / "dictionaries" / "Greek"
        )
        stem = path.with_suffix("") if path.suffix in (".aff", ".dic") else path
        return stem if stem.with_suffix(".aff").is_file() and stem.with_suffix(".dic").is_file() else None

    # ------------------------------------------------------------------
    # indexing
    # ------------------------------------------------------------------
    def reindex_document(self, document_id: str) -> int:
        """Rebuild chunks and the search index for one document.

        Safe to run repeatedly: old chunks are deleted first and the FTS
        triggers keep the index in step.
        """
        store = self.store(document_id)
        pages = store.read_pages()
        chunks = chunk_pages(
            pages,
            target_chars=self.config.chunking.target_chars,
            max_chars=self.config.chunking.max_chars,
        )
        self._number_repeated_headings(chunks)

        try:
            self._write_chunks(document_id, chunks)
        except sqlite3.DatabaseError:
            # The index is derived data. If it has fallen out of step with the
            # chunks table -- an interrupted migration, say -- deleting a chunk
            # reports the database as malformed. Rebuilding costs nothing that
            # cannot be recreated, so heal and retry once.
            rebuild_index(self.conn)
            self._write_chunks(document_id, chunks)
        return len(chunks)

    @staticmethod
    def _number_repeated_headings(chunks: list[Chunk]) -> None:
        """Give a run of equal headings useful, stable display labels."""
        start = 0
        while start < len(chunks):
            raw = chunks[start].raw_heading or chunks[start].heading
            end = start + 1
            while end < len(chunks):
                other = chunks[end].raw_heading or chunks[end].heading
                if not raw or other != raw:
                    break
                end += 1
            if raw and end - start > 1:
                total = end - start
                for number, chunk in enumerate(chunks[start:end], start=1):
                    chunk.raw_heading = raw
                    chunk.heading = f"{raw} ({number}/{total})"
            start = end

    def _write_chunks(self, document_id: str, chunks: list[Chunk]) -> None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            prepared = [(c, normalize_for_index(c.content)) for c in chunks]
            self.conn.executemany(
                """
                INSERT INTO chunks (
                    document_id, page_start, page_end, ordinal, heading, raw_heading,
                    chunk_type, content, search_text, fuzzy, char_count,
                    token_estimate, math_count
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        document_id,
                        c.page_start,
                        c.page_end,
                        c.ordinal,
                        c.heading,
                        c.raw_heading,
                        c.chunk_type,
                        c.content,
                        search_text,
                        trigrams(search_text),
                        c.char_count,
                        estimate_tokens(c.content),
                        c.math_count,
                    )
                    for c, search_text in prepared
                ],
            )
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise

    def repair_document(self, document_ref: str) -> dict[str, Any]:
        """Re-apply the Greek notation repairs to a document already on disk.

        No extraction and no OCR: the stored page Markdown is rewritten in
        place and the index rebuilt, which takes seconds rather than minutes.
        """
        row = self.resolve(document_ref)
        store = self.store(row["id"])
        pages = store.read_pages()
        if not pages:
            raise LibraryError(f"{row['filename']} has no extracted pages")

        whole = "".join(pages.values())
        greek_math = looks_greek_mathematical(whole)
        greek_document = row["language"] == "ell" or self._text_is_greek(whole)
        lexicon = (
            Path(self.config.repair.greek_lexicon).expanduser()
            if self.config.repair.greek_lexicon
            else None
        )
        hunspell = self._greek_hunspell_path()

        counts: dict[str, int] = {}
        changed: list[int] = []
        changes: list[dict[str, Any]] = []
        for number, markdown in pages.items():
            joined = rejoin_hyphenation(markdown)
            prose = (
                repair_greek_prose(joined.text, lexicon, hunspell)
                if greek_document
                else None
            )
            report = repair_greek_math(
                prose.text if prose else joined.text, allow_cosine=greek_math
            )
            if report.text == markdown:
                continue
            for key, value in joined.counts.items():
                counts[key] = counts.get(key, 0) + value
            store.write_page(number, report.text)
            changed.append(number)
            for key, value in report.counts.items():
                counts[key] = counts.get(key, 0) + value
            if prose:
                for change in prose.changes:
                    changes.append(
                        {
                            "page": number,
                            "original": change.original,
                            "replacement": change.replacement,
                        }
                    )
                for key, value in prose.counts.items():
                    counts[key] = counts.get(key, 0) + value

            quality = assess_page(report.text)
            self.conn.execute(
                """
                UPDATE pages
                   SET page_hash=?, char_count=?, quality_score=?, quality_state=?,
                       quality_issues=?, math_count=?, updated_at=?
                 WHERE document_id=? AND page_number=?
                """,
                (
                    text_sha256(report.text),
                    quality.char_count,
                    quality.score,
                    quality.state,
                    json.dumps(quality.issues, ensure_ascii=False),
                    quality.math_count,
                    _now(),
                    row["id"],
                    number,
                ),
            )

        chunk_count = self.reindex_document(row["id"])
        store.rebuild_document_md(title=row["title"])
        return {
            "document_id": row["id"],
            "pages_changed": len(changed),
            "pages": changed,
            "counts": counts,
            "changes": changes,
            "chunk_count": chunk_count,
        }

    def index_is_stale(self) -> bool:
        """True when the stored index predates the current text normalisation."""
        row = self.conn.execute(
            "SELECT value FROM meta WHERE key = 'index_version'"
        ).fetchone()
        stored = int(row[0]) if row else 0
        return stored != INDEX_VERSION

    def reindex_all(self, progress: ProgressFn = _noop) -> int:
        """Rebuild every document's chunks and search index.

        Needed after the index format changes; safe to run at any time.
        """
        documents = self.conn.execute("SELECT id FROM documents").fetchall()
        total = 0
        for index, row in enumerate(documents, start=1):
            total += self.reindex_document(row["id"])
            progress(index / max(len(documents), 1), f"{index}/{len(documents)}")
        self.conn.execute(
            "INSERT INTO meta(key, value) VALUES('index_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(INDEX_VERSION),),
        )
        return total

    def _write_metadata(
        self,
        row: sqlite3.Row,
        stats: dict[str, Any],
        chunk_count: int,
        duration: float,
    ) -> None:
        store = self.store(row["id"])
        store.write_metadata(
            {
                "document_id": row["id"],
                "filename": row["filename"],
                "title": row["title"],
                "sha256": row["sha256"],
                "size_bytes": row["size_bytes"],
                "page_count": row["page_count"],
                "kind": row["kind"],
                "language": stats["language"],
                "created_at": row["created_at"],
                "processed_at": row["processed_at"],
                "engine": row["engine"],
                "engine_version": row["engine_version"],
                "quality_tier": row["quality_tier"],
                "schema_version": SCHEMA_VERSION,
                "extraction_duration_s": round(duration, 2),
                "equation_count": stats["equation_count"],
                "table_count": stats["table_count"],
                "chunk_count": chunk_count,
                "math_pages": stats["math_pages"],
                "low_quality_pages": stats["low_quality_pages"],
                "status": row["status"],
            }
        )

    # ------------------------------------------------------------------
    # upgrade
    # ------------------------------------------------------------------
    def _upgrade_candidates(self, document_id: str) -> list[int]:
        """Pages worth re-extracting with the LaTeX-capable engine."""
        settings = self.config.extraction
        conditions = ["quality_tier = 'fast'"]
        clauses = [f"quality_score < {float(settings.upgrade_below_score)}"]
        if settings.upgrade_math_pages:
            clauses.append("math_count > 0")
        clauses.append("needs_ocr = 1")
        conditions.append("(" + " OR ".join(clauses) + ")")
        rows = self.conn.execute(
            "SELECT page_number FROM pages WHERE document_id = ? AND "
            + " AND ".join(conditions)
            + " ORDER BY page_number",
            (document_id,),
        ).fetchall()
        pages = [r[0] for r in rows]
        cap = settings.max_upgrade_pages
        return pages[:cap] if cap else pages

    def upgrade_pages(
        self,
        document_id: str,
        pages: Iterable[int] | None = None,
        engine_name: str | None = None,
        progress: ProgressFn = _noop,
    ) -> dict[str, Any]:
        """Re-extract selected pages with the quality engine.

        Only the named pages are touched; every other page keeps its cached
        Markdown, so this never means reprocessing the whole book.
        """
        row = self.document(document_id)
        if row is None:
            raise LibraryError(f"no such document: {document_id}")

        provided = list(pages) if pages is not None else []
        if provided:
            invalid = [
                page
                for page in provided
                if not isinstance(page, int)
                or isinstance(page, bool)
                or page < 1
                or page > row["page_count"]
            ]
            if invalid:
                raise LibraryError(
                    f"invalid page(s) {invalid}; valid range is 1-{row['page_count']}"
                )
        targets = sorted(set(provided)) if provided else self._upgrade_candidates(document_id)
        if not targets:
            return {"document_id": row["id"], "pages": [], "detail": "nothing to upgrade"}

        name = engine_name or self.config.extraction.quality_engine
        if not name:
            raise LibraryError("no quality engine configured")
        engine = get_engine(name, self.config)
        ok, reason = engine.available()
        if not ok:
            raise EngineUnavailable(reason)

        store = self.store(row["id"])
        progress(0.05, f"{name}: {len(targets)} pages")
        result = engine.extract(store.source_pdf, pages=targets)
        self._record_run(row["id"], result, pages=targets)

        scanned = set(self._pages_where("source_scanned = 1", row["id"]))
        stats = self._store_pages(
            document_id=row["id"],
            store=store,
            result=result,
            scanned_pages=scanned,
            math_font_pages=set(),
            progress=progress,
            progress_span=(0.6, 0.9),
            quality_tier=engine.quality_tier,
        )

        progress(0.92, "indexing")
        chunk_count = self.reindex_document(row["id"])
        store.rebuild_document_md(title=row["title"])

        remaining = self.conn.execute(
            "SELECT COUNT(*) FROM pages WHERE document_id=? AND quality_tier='fast'",
            (row["id"],),
        ).fetchone()[0]
        # A scanned document yields no text at all on the fast tier, so its
        # language could not be judged at import. Now that there is prose to
        # look at, record what it actually is.
        self.conn.execute(
            "UPDATE documents SET quality_tier=?, language=? WHERE id=?",
            (
                "high" if remaining == 0 else "mixed",
                stats["language"],
                row["id"],
            ),
        )
        progress(1.0, "done")

        return {
            "document_id": row["id"],
            "engine": engine.name,
            "pages": targets,
            "duration_s": round(result.duration_s, 2),
            "equation_count": stats["equation_count"],
            "chunk_count": chunk_count,
        }

    # ------------------------------------------------------------------
    # retrieval
    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        document_id: str | None = None,
        pages: tuple[int, int] | None = None,
        limit: int | None = None,
        chunk_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Search the library, widening only as far as it has to.

        Three stages, each tried only when the previous found nothing: all
        terms present, any term present, then a character-trigram match that
        tolerates OCR damage. Results carry the stage that found them, because
        an approximate match deserves to be read as one.
        """
        limit = limit or self.config.search.max_results
        normalized = normalize_query(query)
        if not normalized:
            return []

        phrase = '"' in normalized
        terms = normalized.split()

        attempts: list[tuple[str, str]] = [
            ("exact", f"{{search_text heading}} : ({normalized})")
        ]
        if not phrase and len(terms) > 1:
            # FTS5 ANDs terms by default, which returns nothing as soon as one
            # word is absent from an otherwise perfect passage.
            attempts.append(
                ("partial", "{search_text heading} : (" + " OR ".join(terms) + ")")
            )
        if not phrase:
            grams = trigrams(normalized).split()
            if grams:
                attempts.append(("approximate", "{fuzzy} : (" + " OR ".join(grams) + ")"))

        for mode, expression in attempts:
            rows = self._run_search(
                expression, document_id, pages, chunk_type, limit
            )
            if rows:
                return [self._search_result(row, mode) for row in rows]
        return []

    def _run_search(
        self,
        expression: str,
        document_id: str | None,
        pages: tuple[int, int] | None,
        chunk_type: str | None,
        limit: int,
    ) -> list[sqlite3.Row]:
        sql = [
            "SELECT c.id, c.document_id, c.page_start, c.page_end, c.heading,",
            "       c.chunk_type, c.content, c.token_estimate, c.math_count,",
            "       d.title, d.filename,",
            # Weights: a hit in a heading counts double, and the trigram column
            # is scored low so it can never outrank a real word match.
            "       bm25(chunks_fts, 1.0, 2.0, 0.1) AS score",
            "  FROM chunks_fts",
            "  JOIN chunks c ON c.id = chunks_fts.rowid",
            "  JOIN documents d ON d.id = c.document_id",
            " WHERE chunks_fts MATCH ?",
        ]
        params: list[Any] = [expression]
        if document_id:
            sql.append("   AND c.document_id = ?")
            params.append(self.resolve(document_id)["id"])
        if pages:
            sql.append("   AND c.page_end >= ? AND c.page_start <= ?")
            params.extend([pages[0], pages[1]])
        if chunk_type:
            sql.append("   AND c.chunk_type = ?")
            params.append(chunk_type)
        sql.append(" ORDER BY score LIMIT ?")
        params.append(limit)

        try:
            return self.conn.execute("\n".join(sql), params).fetchall()
        except sqlite3.OperationalError as exc:
            raise LibraryError(f"bad search query: {exc}") from exc

    def _search_result(self, row: sqlite3.Row, mode: str) -> dict[str, Any]:
        return {
            "chunk_id": row["id"],
            "document_id": row["document_id"],
            "document": row["title"] or row["filename"],
            "pages": [row["page_start"], row["page_end"]],
            "heading": row["heading"],
            "type": row["chunk_type"],
            "math_count": row["math_count"],
            "token_estimate": row["token_estimate"],
            "score": round(-row["score"], 3),
            "match": mode,
            "snippet": _snippet(row["content"], self.config.response.max_snippet_chars),
        }

    def get_pages(self, document_ref: str, pages: Iterable[int]) -> dict[str, Any]:
        row = self.resolve(document_ref)
        store = self.store(row["id"])
        wanted = sorted({int(p) for p in pages})
        provenance = {
            r["page_number"]: r
            for r in self.conn.execute(
                "SELECT page_number, ocr_used, quality_state, quality_score "
                "  FROM pages WHERE document_id = ?",
                (row["id"],),
            )
        }
        out: list[dict[str, Any]] = []
        missing: list[int] = []
        for number in wanted:
            markdown = store.read_page(number)
            if markdown is None:
                missing.append(number)
                continue
            info = provenance.get(number)
            out.append(
                {
                    "page": number,
                    "markdown": markdown,
                    "ocr_used": bool(info["ocr_used"]) if info else False,
                    "quality": info["quality_state"] if info else None,
                    "score": info["quality_score"] if info else None,
                    "blocks": self.page_blocks(row["id"], number)
                    if info and info["ocr_used"]
                    else [],
                }
            )
        return {
            "document_id": row["id"],
            "document": row["title"] or row["filename"],
            "pages": out,
            "missing": missing,
            "page_count": row["page_count"],
        }

    def ocr_review_queue(
        self, document_ref: str, limit: int = 10
    ) -> dict[str, Any]:
        """Return pages that need image-to-text verification, highest risk first.

        This is a queue for a vision-capable AI or a human reviewer, not a
        second OCR pass.  The reviewer compares the source page to the saved
        Markdown and records an explicit verdict with :meth:`record_ocr_review`.
        """
        row = self.resolve(document_ref)
        limit = max(1, min(int(limit), 100))
        rows = self.conn.execute(
            """
            SELECT page_number, source_scanned, ocr_used, quality_score,
                   quality_state, quality_issues, math_count, verification_state
              FROM pages
             WHERE document_id = ?
               AND verification_state IN ('pending', 'needs_correction', 'unreadable')
             ORDER BY CASE verification_state
                        WHEN 'needs_correction' THEN 0
                        WHEN 'unreadable' THEN 1
                        ELSE 2 END,
                      quality_score ASC, math_count DESC, page_number ASC
             LIMIT ?
            """,
            (row["id"], limit),
        ).fetchall()
        pages = []
        for item in rows:
            reasons: list[str] = []
            if item["source_scanned"]:
                reasons.append("source is a scan")
            if item["ocr_used"]:
                reasons.append("OCR used")
            reasons.extend(json.loads(item["quality_issues"] or "[]"))
            if item["math_count"]:
                reasons.append(f"{item['math_count']} equation(s)")
            pages.append(
                {
                    "page": item["page_number"],
                    "state": item["verification_state"],
                    "score": item["quality_score"],
                    "reasons": reasons,
                }
            )
        return {
            "document_id": row["id"],
            "document": row["title"] or row["filename"],
            "pages": pages,
        }

    def correct_ocr_pages(
        self,
        document_ref: str,
        pages: Iterable[int] | None = None,
        apply: bool = True,
        progress: ProgressFn = _noop,
    ) -> dict[str, Any]:
        """Verify scanned/OCR pages with the configured local vision model."""
        row = self.resolve(document_ref)
        provided = sorted({int(p) for p in pages}) if pages is not None else []
        if provided:
            invalid = [p for p in provided if p < 1 or p > row["page_count"]]
            if invalid:
                raise LibraryError(f"invalid page(s) {invalid}; valid range is 1-{row['page_count']}")
            targets = provided
        else:
            targets = [r[0] for r in self.conn.execute(
                "SELECT page_number FROM pages WHERE document_id=? AND (source_scanned=1 OR ocr_used=1) ORDER BY page_number",
                (row["id"],),
            ).fetchall()]
        if not targets:
            return {"document_id": row["id"], "pages": [], "applied": []}
        vision = VisionEngine(self.config.vision)
        ok, reason = vision.available()
        if not ok:
            store = self.store(row["id"])
            for page in targets:
                original = store.read_page(page) or ""
                self.conn.execute(
                    "INSERT INTO vision_corrections (document_id, page_number, original_markdown, proposed_markdown, model, confidence, warnings, applied, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (row["id"], page, original, "", self.config.vision.model, 0.0,
                     json.dumps([reason], ensure_ascii=False), 0, _now()),
                )
                self.conn.execute(
                    "UPDATE pages SET verification_state='pending', verification_note=? WHERE document_id=? AND page_number=?",
                    (reason, row["id"], page),
                )
            return {
                "document_id": row["id"], "pages": targets, "applied": [],
                "corrections": [
                    {"page": page, "confidence": 0.0, "applied": False, "warnings": [reason]}
                    for page in targets
                ],
                "warning": reason,
            }
        store = self.store(row["id"])
        corrections = []
        applied = []
        total = max(len(targets), 1)
        for index, page in enumerate(targets, start=1):
            original = store.read_page(page) or ""
            try:
                correction = vision.correct(store.source_pdf, page, original)
            except EngineUnavailable as exc:
                warning = str(exc)
                self.conn.execute(
                    "INSERT INTO vision_corrections (document_id, page_number, original_markdown, proposed_markdown, model, confidence, warnings, applied, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (row["id"], page, original, "", self.config.vision.model, 0.0,
                     json.dumps([warning], ensure_ascii=False), 0, _now()),
                )
                self.conn.execute(
                    "UPDATE pages SET verification_state='pending', verification_note=? WHERE document_id=? AND page_number=?",
                    (warning, row["id"], page),
                )
                corrections.append({"page": page, "confidence": 0.0, "applied": False, "warnings": [warning]})
                progress(index / total, f"vision page {index}/{total} unavailable")
                continue
            greek_document = row["language"] == "ell" or self._text_is_greek(original)
            warnings = VisionEngine.validate(
                original, correction, greek_document=greek_document,
                min_greek_ratio=self.config.vision.min_greek_ratio,
            )
            if warnings and self.config.vision.crop_retry:
                try:
                    retry = vision.correct(store.source_pdf, page, original, enhanced=True)
                except EngineUnavailable:
                    retry = None
                if retry is not None:
                    retry_warnings = VisionEngine.validate(
                        original, retry, greek_document=greek_document,
                        min_greek_ratio=self.config.vision.min_greek_ratio,
                    )
                    if len(retry_warnings) < len(warnings):
                        correction, warnings = retry, retry_warnings
            accepted = (
                not warnings
                and correction.confidence >= self.config.vision.apply_min_confidence
            )
            correction.validation_warnings = warnings
            self.conn.execute(
                "INSERT INTO vision_corrections (document_id, page_number, original_markdown, proposed_markdown, model, confidence, warnings, applied, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (row["id"], page, original, correction.markdown, correction.model,
                 correction.confidence, json.dumps(warnings, ensure_ascii=False),
                 int(accepted and apply), _now()),
            )
            if apply and accepted:
                store.write_page(page, correction.markdown)
                quality = assess_page(correction.markdown)
                self.conn.execute(
                    "UPDATE pages SET page_hash=?, char_count=?, quality_score=?, quality_state=?, quality_issues=?, math_count=?, verification_state='approved', verification_note=?, verified_at=?, updated_at=? WHERE document_id=? AND page_number=?",
                    (text_sha256(correction.markdown), quality.char_count, quality.score, quality.state,
                     json.dumps(quality.issues, ensure_ascii=False), quality.math_count,
                     "MLX-VLM automatic correction", _now(), _now(), row["id"], page),
                )
                applied.append(page)
            if warnings and not apply:
                self.conn.execute(
                    "UPDATE pages SET verification_state='needs_correction', verification_note=? WHERE document_id=? AND page_number=?",
                    ("; ".join(warnings), row["id"], page),
                )
            elif warnings:
                self.conn.execute(
                    "UPDATE pages SET verification_state='needs_correction', verification_note=? WHERE document_id=? AND page_number=?",
                    ("; ".join(warnings), row["id"], page),
                )
            corrections.append({"page": page, "confidence": correction.confidence, "applied": accepted and apply, "warnings": warnings})
            progress(index / total, f"vision page {index}/{total}")
        if applied:
            self.reindex_document(row["id"])
            store.rebuild_document_md(title=row["title"])
        return {"document_id": row["id"], "pages": targets, "applied": applied, "corrections": corrections}

    def vision_correction_history(
        self, document_ref: str, page: int | None = None
    ) -> list[dict[str, Any]]:
        """Return the append-only vision correction audit trail."""
        row = self.resolve(document_ref)
        sql = "SELECT * FROM vision_corrections WHERE document_id=?"
        params: list[Any] = [row["id"]]
        if page is not None:
            sql += " AND page_number=?"
            params.append(int(page))
        sql += " ORDER BY id"
        return [
            {
                "id": item["id"], "page": item["page_number"],
                "model": item["model"], "confidence": item["confidence"],
                "warnings": json.loads(item["warnings"] or "[]"),
                "applied": bool(item["applied"]),
                "original_markdown": item["original_markdown"],
                "proposed_markdown": item["proposed_markdown"],
                "created_at": item["created_at"],
            }
            for item in self.conn.execute(sql, params).fetchall()
        ]

    def record_ocr_review(
        self,
        document_ref: str,
        page: int,
        verdict: str,
        note: str = "",
    ) -> dict[str, Any]:
        """Persist a visual OCR-review verdict without changing the source text.

        A reviewer may approve a transcription, flag it for a correction, or
        say that the scan cannot be read.  Corrections remain deliberate: this
        method never lets a review result silently rewrite mathematical text.
        """
        states = {"approved", "needs_correction", "unreadable"}
        if verdict not in states:
            choices = ", ".join(sorted(states))
            raise LibraryError(f"invalid review verdict {verdict!r}; use {choices}")
        row = self.resolve(document_ref)
        page_row = self.conn.execute(
            "SELECT page_number FROM pages WHERE document_id=? AND page_number=?",
            (row["id"], int(page)),
        ).fetchone()
        if page_row is None:
            raise LibraryError(
                f"page {page} is outside this document (1-{row['page_count']})"
            )
        cleaned_note = note.strip()
        if verdict != "approved" and not cleaned_note:
            raise LibraryError("a note is required for a non-approved review")
        self.conn.execute(
            """
            UPDATE pages
               SET verification_state=?, verification_note=?, verified_at=?
             WHERE document_id=? AND page_number=?
            """,
            (verdict, cleaned_note or None, _now(), row["id"], int(page)),
        )
        return {
            "document_id": row["id"],
            "page": int(page),
            "verdict": verdict,
            "note": cleaned_note or None,
        }

    def page_blocks(self, document_ref: str, page: int) -> list[dict[str, Any]]:
        """The laid-out regions of one page, when the engine reported them."""
        row = self.resolve(document_ref)
        rows = self.conn.execute(
            "SELECT ordinal, block_type, x0, y0, x1, y1, char_count "
            "  FROM blocks WHERE document_id = ? AND page_number = ? "
            " ORDER BY ordinal",
            (row["id"], page),
        ).fetchall()
        return [
            {
                "block": r["ordinal"],
                "type": r["block_type"],
                "bbox": (
                    None
                    if r["x0"] is None
                    else (r["x0"], r["y0"], r["x1"], r["y1"])
                ),
                "chars": r["char_count"],
            }
            for r in rows
        ]

    def render_page_region(
        self,
        document_ref: str,
        page: int,
        block: int | None = None,
        max_tokens: int = 900,
    ) -> RenderedRegion:
        """Render the original of a page, or of one block on it.

        Cropping is the point: at the same token budget a single equation is
        rendered several times larger than the whole page around it.
        """
        row = self.resolve(document_ref)
        store = self.store(row["id"])
        bbox = None
        note = ""

        if block is not None:
            found = self.conn.execute(
                "SELECT block_type, x0, y0, x1, y1 FROM blocks "
                " WHERE document_id = ? AND page_number = ? AND ordinal = ?",
                (row["id"], page, block),
            ).fetchone()
            if found is None:
                available = self.page_blocks(row["id"], page)
                if not available:
                    note = (
                        "this document has no block layout recorded; "
                        "reprocess it with the quality engine to get one. "
                        "Showing the whole page."
                    )
                else:
                    raise LibraryError(
                        f"page {page} has no block {block}; "
                        f"blocks are 0-{len(available) - 1}"
                    )
            elif found["x0"] is None:
                note = "that block has no recorded position; showing the whole page."
            else:
                bbox = (found["x0"], found["y0"], found["x1"], found["y1"])

        try:
            region = render_region(
                store.source_pdf, page, bbox=bbox, max_tokens=max_tokens
            )
        except ValueError as exc:
            raise LibraryError(str(exc)) from exc
        region.note = note
        return region

    def get_chunk(self, chunk_id: int) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT c.*, d.title, d.filename
              FROM chunks c JOIN documents d ON d.id = c.document_id
             WHERE c.id = ?
            """,
            (chunk_id,),
        ).fetchone()
        if row is None:
            raise LibraryError(f"no such chunk: {chunk_id}")
        return {
            "chunk_id": row["id"],
            "document_id": row["document_id"],
            "document": row["title"] or row["filename"],
            "pages": [row["page_start"], row["page_end"]],
            "heading": row["heading"],
            "type": row["chunk_type"],
            "content": row["content"],
            "token_estimate": row["token_estimate"],
        }

    def get_section(self, document_ref: str, section: str) -> dict[str, Any]:
        """Return the chunks under the heading that best matches ``section``."""
        row = self.resolve(document_ref)
        target = normalize_query(section)
        candidates = self.conn.execute(
            "SELECT DISTINCT COALESCE(raw_heading, heading) AS heading FROM chunks "
            "WHERE document_id = ? AND COALESCE(raw_heading, heading) IS NOT NULL",
            (row["id"],),
        ).fetchall()

        best: tuple[int, str] | None = None
        for candidate in candidates:
            heading = candidate["heading"]
            folded = normalize_query(heading)
            if target == folded:
                best = (1000, heading)
                break
            if target in folded:
                score = 500 - len(folded)
                if best is None or score > best[0]:
                    best = (score, heading)
        if best is None:
            raise LibraryError(
                f"no section matching {section!r} in {row['title'] or row['filename']}"
            )

        chunks = self.conn.execute(
            "SELECT * FROM chunks WHERE document_id = ? "
            "AND COALESCE(raw_heading, heading) = ? ORDER BY ordinal",
            (row["id"], best[1]),
        ).fetchall()
        return {
            "document_id": row["id"],
            "document": row["title"] or row["filename"],
            "heading": best[1],
            "pages": [
                min(c["page_start"] for c in chunks),
                max(c["page_end"] for c in chunks),
            ],
            "content": "\n\n".join(c["content"] for c in chunks),
        }

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------
    def _document_summary(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "document_id": row["id"],
            "title": row["title"] or row["filename"],
            "filename": row["filename"],
            "pages": row["page_count"],
            "status": row["status"],
            "kind": row["kind"],
            "language": row["language"],
            "quality_tier": row["quality_tier"],
            "engine": row["engine"],
            "created_at": row["created_at"],
        }

    def status(self, document_ref: str) -> dict[str, Any]:
        row = self.resolve(document_ref)
        summary = self._document_summary(row)
        aggregate = self.conn.execute(
            """
            SELECT COUNT(*) AS n,
                   COALESCE(SUM(math_count),0) AS equations,
                   COALESCE(SUM(table_count),0) AS tables,
                   COALESCE(AVG(quality_score),0) AS avg_score,
                   COALESCE(SUM(needs_ocr),0) AS scanned,
                   COALESCE(SUM(quality_tier='high'),0) AS high_pages
              FROM pages WHERE document_id = ?
            """,
            (row["id"],),
        ).fetchone()
        job = self.conn.execute(
            "SELECT id, kind, state, progress, detail, error FROM jobs "
            "WHERE document_id = ? ORDER BY created_at DESC LIMIT 1",
            (row["id"],),
        ).fetchone()

        summary.update(
            {
                "sha256": row["sha256"],
                "pages_processed": aggregate["n"],
                "pages_high_quality": aggregate["high_pages"],
                "equations": aggregate["equations"],
                "tables": aggregate["tables"],
                "avg_quality": round(aggregate["avg_score"], 3),
                "source_scanned_pages": self._pages_where(
                    "source_scanned = 1", row["id"]
                ),
                "pending_ocr_pages": self._pages_where("needs_ocr = 1", row["id"]),
                # Compatibility alias. New callers should use pending_ocr_pages.
                "scanned_pages": self._pages_where("needs_ocr = 1", row["id"]),
                "ocr_pages": self._pages_where("ocr_used = 1", row["id"]),
                "verification_pending_pages": self._pages_where(
                    "verification_state IN ('pending', 'needs_correction', 'unreadable')",
                    row["id"],
                ),
                "verification_approved_pages": self._pages_where(
                    "verification_state = 'approved'", row["id"]
                ),
                "vision_corrections": self.conn.execute(
                    "SELECT COUNT(*) FROM vision_corrections WHERE document_id=?",
                    (row["id"],),
                ).fetchone()[0],
                "vision_corrections_applied": self.conn.execute(
                    "SELECT COUNT(*) FROM vision_corrections WHERE document_id=? AND applied=1",
                    (row["id"],),
                ).fetchone()[0],
                "low_quality_pages": self._pages_where(
                    "quality_state IN ('warning','bad')", row["id"]
                ),
                "upgrade_candidates": self._upgrade_candidates(row["id"]),
                "chunks": self.conn.execute(
                    "SELECT COUNT(*) FROM chunks WHERE document_id = ?", (row["id"],)
                ).fetchone()[0],
                "error": row["error"],
                "latest_job": dict(job) if job else None,
                "engines": [
                    item[0]
                    for item in self.conn.execute(
                        "SELECT DISTINCT engine FROM engine_runs WHERE document_id=? "
                        "ORDER BY id",
                        (row["id"],),
                    ).fetchall()
                ],
            }
        )
        return summary

    def page_report(self, document_ref: str) -> list[dict[str, Any]]:
        row = self.resolve(document_ref)
        rows = self.conn.execute(
            "SELECT * FROM pages WHERE document_id = ? ORDER BY page_number",
            (row["id"],),
        ).fetchall()
        return [
            {
                "page": r["page_number"],
                "engine": r["engine"],
                "tier": r["quality_tier"],
                "score": r["quality_score"],
                "state": r["quality_state"],
                "math": r["math_count"],
                "chars": r["char_count"],
                "needs_ocr": bool(r["needs_ocr"]),
                "verification": r["verification_state"],
                "verification_note": r["verification_note"],
                "issues": json.loads(r["quality_issues"] or "[]"),
            }
            for r in rows
        ]

    def delete_document(self, document_ref: str) -> str:
        row = self.resolve(document_ref)
        self.store(row["id"]).delete()
        self.conn.execute("DELETE FROM documents WHERE id = ?", (row["id"],))
        return row["id"]

    def stats(self) -> dict[str, Any]:
        row = self.conn.execute(
            """
            SELECT (SELECT COUNT(*) FROM documents) AS documents,
                   (SELECT COUNT(*) FROM pages) AS pages,
                   (SELECT COUNT(*) FROM chunks) AS chunks,
                   (SELECT COALESCE(SUM(math_count),0) FROM pages) AS equations
            """
        ).fetchone()
        return dict(row) | {"root": str(self.config.root)}


def _snippet(content: str, limit: int) -> str:
    text = " ".join(content.split())
    if len(text) <= limit:
        return text
    return text[:limit].rsplit(" ", 1)[0] + " ..."
