"""The library: import, cache, index, search and retrieve.

Everything the CLI and the MCP server do goes through this class. It owns the
database connection and the on-disk document store, and it is the only place
that decides whether work can be skipped.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

from .chunking import chunk_pages
from .config import Config, load_config
from .db import INDEX_VERSION, SCHEMA_VERSION, init_db
from .engines import EngineUnavailable, get_engine
from .engines.base import ExtractionResult
from .hashing import document_id as make_document_id
from .greek_math import looks_greek_mathematical
from .greek_math import repair as repair_greek_math
from .hashing import file_sha256, text_sha256
from .normalize import normalize_for_index, normalize_query
from .quality import assess_page, looks_mathematical
from .storage import DocumentStore
from .tokens import estimate_tokens

ProgressFn = Callable[[float, str], None]


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

        started = time.perf_counter()
        progress(0.02, "hashing")
        sha256 = file_sha256(pdf_path)

        existing = self.document_by_hash(sha256)
        if existing is not None and not force and existing["status"] == "complete":
            return self._cache_hit_result(existing, time.perf_counter() - started)

        document_id = existing["id"] if existing else make_document_id(sha256)
        store = self.store(document_id)
        store.ensure()
        store.store_source(pdf_path)

        engine = get_engine(self.config.extraction.fast_engine, self.config)
        progress(0.08, "inspecting")
        inspection = engine.inspect(store.source_pdf)

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
        repairs: dict[str, int] = {}
        math_pages: list[int] = []
        low_quality: list[int] = []
        equation_count = 0
        table_count = 0
        low, high = progress_span
        total = max(len(result.pages), 1)

        for index, page in enumerate(result.pages, start=1):
            if repair_math:
                report = repair_greek_math(page.markdown, allow_cosine=True)
                page.markdown = report.text
                for key, value in report.counts.items():
                    repairs[key] = repairs.get(key, 0) + value

            quality = assess_page(page.markdown)
            is_scanned = page.page_number in scanned_pages
            needs_ocr = is_scanned and not quality.has_text
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
            self.conn.execute(
                """
                INSERT INTO pages (
                    document_id, page_number, page_hash, char_count, engine,
                    quality_tier, ocr_used, ocr_reason, needs_ocr, quality_score,
                    quality_state, quality_issues, math_count, table_count, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(document_id, page_number) DO UPDATE SET
                    page_hash=excluded.page_hash, char_count=excluded.char_count,
                    engine=excluded.engine, quality_tier=excluded.quality_tier,
                    ocr_used=excluded.ocr_used, ocr_reason=excluded.ocr_reason,
                    needs_ocr=excluded.needs_ocr, quality_score=excluded.quality_score,
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

    @staticmethod
    def _is_greek(result: ExtractionResult) -> bool:
        sample = "".join(p.markdown for p in result.pages[:20])[:20000]
        if not sample:
            return False
        greek = sum(1 for ch in sample if "Ͱ" <= ch <= "Ͽ" or "ἀ" <= ch <= "῿")
        letters = sum(1 for ch in sample if ch.isalpha())
        return letters > 0 and greek / letters > 0.3

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

        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
            self.conn.executemany(
                """
                INSERT INTO chunks (
                    document_id, page_start, page_end, ordinal, heading,
                    chunk_type, content, search_text, char_count,
                    token_estimate, math_count
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        document_id,
                        c.page_start,
                        c.page_end,
                        c.ordinal,
                        c.heading,
                        c.chunk_type,
                        c.content,
                        normalize_for_index(c.content),
                        c.char_count,
                        estimate_tokens(c.content),
                        c.math_count,
                    )
                    for c in chunks
                ],
            )
            self.conn.execute("COMMIT")
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        return len(chunks)

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
        if not looks_greek_mathematical(whole):
            return {
                "document_id": row["id"],
                "pages_changed": 0,
                "counts": {},
                "detail": "no Greek mathematical notation found",
            }

        counts: dict[str, int] = {}
        changed: list[int] = []
        for number, markdown in pages.items():
            report = repair_greek_math(markdown, allow_cosine=True)
            if report.text == markdown:
                continue
            store.write_page(number, report.text)
            changed.append(number)
            for key, value in report.counts.items():
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

        name = engine_name or self.config.extraction.quality_engine
        if not name:
            raise LibraryError("no quality engine configured")
        engine = get_engine(name, self.config)
        ok, reason = engine.available()
        if not ok:
            raise EngineUnavailable(reason)

        targets = sorted(set(pages)) if pages else self._upgrade_candidates(document_id)
        if not targets:
            return {"document_id": row["id"], "pages": [], "detail": "nothing to upgrade"}

        store = self.store(row["id"])
        progress(0.05, f"{name}: {len(targets)} pages")
        result = engine.extract(store.source_pdf, pages=targets)
        self._record_run(row["id"], result, pages=targets)

        scanned = set(self._pages_where("needs_ocr = 1", row["id"]))
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
        self.conn.execute(
            "UPDATE documents SET quality_tier=? WHERE id=?",
            ("high" if remaining == 0 else "mixed", row["id"]),
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
        limit = limit or self.config.search.max_results
        normalized = normalize_query(query)
        if not normalized:
            return []

        sql = [
            "SELECT c.id, c.document_id, c.page_start, c.page_end, c.heading,",
            "       c.chunk_type, c.content, c.token_estimate, c.math_count,",
            "       d.title, d.filename, bm25(chunks_fts) AS score",
            "  FROM chunks_fts",
            "  JOIN chunks c ON c.id = chunks_fts.rowid",
            "  JOIN documents d ON d.id = c.document_id",
            " WHERE chunks_fts MATCH ?",
        ]
        params: list[Any] = [normalized]
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

        statement = "\n".join(sql)
        try:
            rows = self.conn.execute(statement, params).fetchall()
            if not rows:
                # FTS5 ANDs terms by default, which returns nothing as soon as
                # one word is absent. Retry as OR so a near miss still helps.
                terms = normalized.split()
                if len(terms) > 1 and '"' not in normalized:
                    params[0] = " OR ".join(terms)
                    rows = self.conn.execute(statement, params).fetchall()
        except sqlite3.OperationalError as exc:
            raise LibraryError(f"bad search query: {exc}") from exc

        snippet_chars = self.config.response.max_snippet_chars
        return [
            {
                "chunk_id": r["id"],
                "document_id": r["document_id"],
                "document": r["title"] or r["filename"],
                "pages": [r["page_start"], r["page_end"]],
                "heading": r["heading"],
                "type": r["chunk_type"],
                "math_count": r["math_count"],
                "token_estimate": r["token_estimate"],
                "score": round(-r["score"], 3),
                "snippet": _snippet(r["content"], snippet_chars),
            }
            for r in rows
        ]

    def get_pages(self, document_ref: str, pages: Iterable[int]) -> dict[str, Any]:
        row = self.resolve(document_ref)
        store = self.store(row["id"])
        wanted = sorted({int(p) for p in pages})
        out: list[dict[str, Any]] = []
        missing: list[int] = []
        for number in wanted:
            markdown = store.read_page(number)
            if markdown is None:
                missing.append(number)
                continue
            out.append({"page": number, "markdown": markdown})
        return {
            "document_id": row["id"],
            "document": row["title"] or row["filename"],
            "pages": out,
            "missing": missing,
            "page_count": row["page_count"],
        }

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
            "SELECT DISTINCT heading FROM chunks "
            "WHERE document_id = ? AND heading IS NOT NULL",
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
            "SELECT * FROM chunks WHERE document_id = ? AND heading = ? ORDER BY ordinal",
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
                "scanned_pages": self._pages_where("needs_ocr = 1", row["id"]),
                "low_quality_pages": self._pages_where(
                    "quality_state IN ('warning','bad')", row["id"]
                ),
                "upgrade_candidates": self._upgrade_candidates(row["id"]),
                "chunks": self.conn.execute(
                    "SELECT COUNT(*) FROM chunks WHERE document_id = ?", (row["id"],)
                ).fetchone()[0],
                "error": row["error"],
                "latest_job": dict(job) if job else None,
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
