"""SQLite schema and connection handling.

The MCP server and the background upgrade worker both write, so every
connection runs in WAL mode with a busy timeout.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1
# Bumped whenever the text written into the FTS index changes shape, which
# makes every existing index stale until the documents are reindexed.
INDEX_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    id              TEXT PRIMARY KEY,
    sha256          TEXT NOT NULL UNIQUE,
    filename        TEXT NOT NULL,
    title           TEXT,
    source_path     TEXT NOT NULL,
    page_count      INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'pending',
    kind            TEXT,
    language        TEXT,
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    processed_at    TEXT,
    engine          TEXT,
    engine_version  TEXT,
    quality_tier    TEXT NOT NULL DEFAULT 'fast',
    schema_version  INTEGER NOT NULL DEFAULT 1,
    error           TEXT
);

CREATE TABLE IF NOT EXISTS pages (
    id            INTEGER PRIMARY KEY,
    document_id   TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_number   INTEGER NOT NULL,
    page_hash     TEXT,
    char_count    INTEGER NOT NULL DEFAULT 0,
    engine        TEXT,
    quality_tier  TEXT NOT NULL DEFAULT 'fast',
    ocr_used      INTEGER NOT NULL DEFAULT 0,
    ocr_reason    TEXT,
    needs_ocr     INTEGER NOT NULL DEFAULT 0,
    quality_score REAL,
    quality_state TEXT,
    quality_issues TEXT,
    math_count    INTEGER NOT NULL DEFAULT 0,
    table_count   INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT,
    UNIQUE (document_id, page_number)
);

CREATE TABLE IF NOT EXISTS chunks (
    id            INTEGER PRIMARY KEY,
    document_id   TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_start    INTEGER NOT NULL,
    page_end      INTEGER NOT NULL,
    ordinal       INTEGER NOT NULL,
    heading       TEXT,
    chunk_type    TEXT NOT NULL DEFAULT 'paragraph',
    content       TEXT NOT NULL,
    search_text   TEXT NOT NULL DEFAULT '',
    fuzzy         TEXT NOT NULL DEFAULT '',
    char_count    INTEGER NOT NULL DEFAULT 0,
    token_estimate INTEGER NOT NULL DEFAULT 0,
    math_count    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(document_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_chunks_pages ON chunks(document_id, page_start);
CREATE INDEX IF NOT EXISTS idx_pages_doc ON pages(document_id, page_number);

-- Indexed text is normalised (accent-folded, math stripped); the readable
-- Markdown lives in chunks.content and is never handed to the tokeniser.
-- External-content mode keeps a single copy of the text, and the triggers
-- below keep the index honest when a document is reprocessed.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    search_text,
    heading,
    fuzzy,
    content='chunks',
    content_rowid='id',
    tokenize='unicode61 remove_diacritics 2'
);

CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
    INSERT INTO chunks_fts(rowid, search_text, heading, fuzzy)
    VALUES (new.id, new.search_text, new.heading, new.fuzzy);
END;

CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, search_text, heading, fuzzy)
    VALUES ('delete', old.id, old.search_text, old.heading, old.fuzzy);
END;

CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
    INSERT INTO chunks_fts(chunks_fts, rowid, search_text, heading, fuzzy)
    VALUES ('delete', old.id, old.search_text, old.heading, old.fuzzy);
    INSERT INTO chunks_fts(rowid, search_text, heading, fuzzy)
    VALUES (new.id, new.search_text, new.heading, new.fuzzy);
END;

-- Laid-out regions of a page, when the engine reports them. These make chunk
-- typing reliable and let a reader be shown the original of one equation
-- rather than a whole page.
CREATE TABLE IF NOT EXISTS blocks (
    id           INTEGER PRIMARY KEY,
    document_id  TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    page_number  INTEGER NOT NULL,
    ordinal      INTEGER NOT NULL,
    block_type   TEXT NOT NULL,
    x0           REAL,
    y0           REAL,
    x1           REAL,
    y1           REAL,
    page_width   REAL,
    page_height  REAL,
    char_count   INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_blocks_page
    ON blocks(document_id, page_number, ordinal);

CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    document_id  TEXT REFERENCES documents(id) ON DELETE CASCADE,
    kind         TEXT NOT NULL,
    state        TEXT NOT NULL DEFAULT 'queued',
    progress     REAL NOT NULL DEFAULT 0.0,
    detail       TEXT,
    error        TEXT,
    created_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_doc ON jobs(document_id, created_at);

CREATE TABLE IF NOT EXISTS engine_runs (
    id            INTEGER PRIMARY KEY,
    document_id   TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    engine        TEXT NOT NULL,
    engine_version TEXT,
    pages         TEXT,
    duration_s    REAL,
    ok            INTEGER NOT NULL DEFAULT 1,
    error         TEXT,
    created_at    TEXT NOT NULL
);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def _migrate(conn: sqlite3.Connection) -> bool:
    """Bring an older database up to the current shape.

    Columns are added in place; the FTS table cannot gain a column, so it is
    dropped and recreated. Nothing is lost -- it is an external-content index
    over `chunks` -- but it does have to be rebuilt, which `index_is_stale`
    then asks the user to do.
    """
    tables = {
        row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "chunks" not in tables:
        return False

    columns = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
    if "fuzzy" not in columns:
        conn.execute("ALTER TABLE chunks ADD COLUMN fuzzy TEXT NOT NULL DEFAULT ''")

    fts_columns = {row[1] for row in conn.execute("PRAGMA table_info(chunks_fts)")}
    if fts_columns and "fuzzy" not in fts_columns:
        for trigger in ("chunks_ai", "chunks_ad", "chunks_au"):
            conn.execute(f"DROP TRIGGER IF EXISTS {trigger}")
        conn.execute("DROP TABLE IF EXISTS chunks_fts")
        conn.execute("DELETE FROM meta WHERE key = 'index_version'")
        return True
    return False


def rebuild_index(conn: sqlite3.Connection) -> None:
    """Repopulate the search index from the chunks table.

    An external-content FTS5 index and its content table have to agree:
    deleting a chunk issues a 'delete' command carrying the old values, and if
    the index holds no such entry SQLite reports the database as malformed.
    A newly created index is empty, so it must be rebuilt before anything
    touches `chunks`. Note that counting rows in the index is no test of this
    -- for an external-content table those rows are read from the content.
    """
    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")


def index_is_populated(conn: sqlite3.Connection) -> bool:
    """Whether the FTS index actually holds anything.

    Counting rows in the index itself is useless here: for an external-content
    table those rows are served from `chunks`. The shadow data table is the
    only honest measure, and an empty index leaves it with just its structure
    row.
    """
    try:
        rows = conn.execute("SELECT COUNT(*) FROM chunks_fts_data").fetchone()[0]
    except sqlite3.OperationalError:
        return True
    return rows > 1


def init_db(db_path: Path) -> sqlite3.Connection:
    conn = connect(db_path)
    recreated = _migrate(conn)
    conn.executescript(_SCHEMA)

    has_chunks = conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] > 0
    if recreated or (has_chunks and not index_is_populated(conn)):
        rebuild_index(conn)
    conn.execute(
        "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
        "ON CONFLICT(key) DO NOTHING",
        (str(SCHEMA_VERSION),),
    )
    # Only stamp the index version on a database that has nothing indexed yet.
    # Stamping an existing library would declare a stale index current and
    # silently leave the old, differently-normalised text in place.
    empty = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
    if empty:
        conn.execute(
            "INSERT INTO meta(key, value) VALUES('index_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(INDEX_VERSION),),
        )
    return conn
