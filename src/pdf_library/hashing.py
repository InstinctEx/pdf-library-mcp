"""Content addressing.

A document's identity is the SHA-256 of its bytes, so re-importing the same
file is always a cache hit no matter where it sits on disk or what it is
called. Pages carry their own hash of the extracted text so an upgrade can
tell which pages actually changed.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

_CHUNK = 1024 * 1024


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(_CHUNK):
            digest.update(block)
    return digest.hexdigest()


def text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def document_id(sha256: str) -> str:
    """A stable, readable id derived from the content hash.

    Deriving it from the hash (rather than a random uuid4) means a repeated
    import lands on the same directory even if the database was lost.
    """
    return str(uuid.UUID(sha256[:32]))
