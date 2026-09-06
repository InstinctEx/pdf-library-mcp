"""Background jobs.

Importing an 800-page book takes long enough that it cannot be done inside an
MCP tool call: the client would sit blocked on stdio for minutes. So the tools
start a job, return its id at once, and the caller polls ``document_status``.

Jobs run one at a time. Extraction is CPU-bound and the quality engine is
memory-hungry, so there is nothing to gain from running several at once, and
serialising them keeps database contention trivial.
"""

from __future__ import annotations

import threading
import traceback
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .config import Config
from .db import connect
from .library import Library

JobBody = Callable[[Library, Callable[[float, str], None]], dict[str, Any]]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobRunner:
    """Runs job bodies on a single worker thread.

    Each job gets its own Library, and therefore its own SQLite connection,
    because a connection may not be shared across threads.
    """

    def __init__(self, config: Config) -> None:
        self.config = config
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pdflib")
        self._futures: dict[str, Future[dict[str, Any]]] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def submit(self, kind: str, document_id: str | None, body: JobBody) -> str:
        job_id = uuid.uuid4().hex[:12]
        conn = connect(self.config.db_path)
        try:
            conn.execute(
                "INSERT INTO jobs (id, document_id, kind, state, created_at) "
                "VALUES (?,?,?,'queued',?)",
                (job_id, document_id, kind, _now()),
            )
        finally:
            conn.close()

        future = self._pool.submit(self._run, job_id, body)
        with self._lock:
            self._futures[job_id] = future
        return job_id

    # ------------------------------------------------------------------
    def _run(self, job_id: str, body: JobBody) -> dict[str, Any]:
        conn = connect(self.config.db_path)

        def report(progress: float, detail: str) -> None:
            conn.execute(
                "UPDATE jobs SET progress=?, detail=? WHERE id=?",
                (round(float(progress), 3), detail, job_id),
            )

        conn.execute(
            "UPDATE jobs SET state='running', started_at=? WHERE id=?",
            (_now(), job_id),
        )
        library = Library(self.config)
        try:
            result = body(library, report)
        except Exception as exc:
            conn.execute(
                "UPDATE jobs SET state='failed', error=?, finished_at=?, detail=? "
                "WHERE id=?",
                (f"{type(exc).__name__}: {exc}", _now(), "failed", job_id),
            )
            # The traceback is useful in the server log but must never reach
            # the model as tool output.
            traceback.print_exc()
            raise
        else:
            conn.execute(
                "UPDATE jobs SET state='complete', progress=1.0, finished_at=?, "
                "detail='done' WHERE id=?",
                (_now(), job_id),
            )
            return result
        finally:
            library.close()
            conn.close()

    # ------------------------------------------------------------------
    def job(self, job_id: str) -> dict[str, Any] | None:
        conn = connect(self.config.db_path)
        try:
            row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def wait(self, job_id: str, timeout: float | None = None) -> dict[str, Any] | None:
        """Block until a job finishes. Used by the CLI, never by the server."""
        with self._lock:
            future = self._futures.get(job_id)
        if future is None:
            return self.job(job_id)
        try:
            future.result(timeout=timeout)
        except Exception:
            pass
        return self.job(job_id)

    def active_count(self) -> int:
        conn = connect(self.config.db_path)
        try:
            return conn.execute(
                "SELECT COUNT(*) FROM jobs WHERE state IN ('queued','running')"
            ).fetchone()[0]
        finally:
            conn.close()

    def shutdown(self, wait: bool = False) -> None:
        self._pool.shutdown(wait=wait, cancel_futures=not wait)


# ----------------------------------------------------------------------
# job bodies
# ----------------------------------------------------------------------
def import_job(pdf_path: Path, force: bool = False, auto_upgrade: bool = False) -> JobBody:
    def body(library: Library, report: Callable[[float, str], None]) -> dict[str, Any]:
        result = library.import_pdf(pdf_path, force=force, progress=report)
        payload: dict[str, Any] = {"import": result.document_id}
        if auto_upgrade and result.upgrade_candidates:
            report(0.0, "upgrading")
            payload["upgrade"] = library.upgrade_pages(
                result.document_id, result.upgrade_candidates, progress=report
            )
        return payload

    return body


def upgrade_job(
    document_id: str, pages: list[int] | None, engine: str | None
) -> JobBody:
    def body(library: Library, report: Callable[[float, str], None]) -> dict[str, Any]:
        return library.upgrade_pages(
            document_id, pages=pages, engine_name=engine, progress=report
        )

    return body
