"""Structure-aware chunking.

Chunks are the unit of search, so they have to be readable on their own. Two
rules drive the splitting:

* never cut inside a ``$$ ... $$`` block, a fenced code block or a table -- a
  half equation is worse than no result;
* prefer to break at a heading, and keep a theorem-like heading attached to the
  paragraphs that follow it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .quality import count_math

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")
_FENCE = re.compile(r"^\s*(```|~~~)")
_DISPLAY_OPEN = re.compile(r"(?<!\\)\$\$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")

_TYPE_WORDS = {
    "theorem": ("theorem", "θεώρημα"),
    "lemma": ("lemma", "λήμμα"),
    "proof": ("proof", "απόδειξη"),
    "definition": ("definition", "ορισμός", "ορισμοσ"),
    "example": ("example", "παράδειγμα"),
    "corollary": ("corollary", "πόρισμα"),
    "remark": ("remark", "παρατήρηση"),
    "proposition": ("proposition", "πρόταση"),
    "exercise": ("exercise", "άσκηση"),
}


@dataclass
class Chunk:
    ordinal: int
    page_start: int
    page_end: int
    heading: str | None
    chunk_type: str
    content: str

    @property
    def char_count(self) -> int:
        return len(self.content)

    @property
    def math_count(self) -> int:
        return count_math(self.content)


def classify(heading: str | None, body: str) -> str:
    """Label a chunk by the mathematical object it appears to hold."""
    probe = f"{heading or ''}\n{body[:160]}".lower()
    for label, words in _TYPE_WORDS.items():
        if any(w in probe for w in words):
            return label
    if body.count("$$") >= 2 and len(body.strip()) < 400:
        return "equation"
    if sum(1 for line in body.splitlines() if _TABLE_ROW.match(line)) >= 3:
        return "table"
    return "paragraph"


@dataclass
class _Block:
    """An atomic run of lines that must not be split."""

    lines: list[str]
    page: int
    heading: str | None
    is_heading: bool


def _blocks(page_number: int, markdown: str) -> list[_Block]:
    """Split one page into atomic blocks, keeping math and code intact."""
    out: list[_Block] = []
    buffer: list[str] = []
    in_fence = False
    in_display = False

    def flush() -> None:
        if buffer and any(line.strip() for line in buffer):
            out.append(_Block(list(buffer), page_number, None, False))
        buffer.clear()

    for line in markdown.splitlines():
        if not in_display and _FENCE.match(line):
            in_fence = not in_fence
            buffer.append(line)
            continue

        if not in_fence:
            # An odd number of $$ on this line toggles display-math state.
            if len(_DISPLAY_OPEN.findall(line)) % 2 == 1:
                in_display = not in_display
                buffer.append(line)
                continue

        if in_fence or in_display:
            buffer.append(line)
            continue

        heading = _HEADING.match(line)
        if heading:
            flush()
            out.append(_Block([line], page_number, heading.group(2).strip(), True))
            continue

        if not line.strip():
            flush()
            continue

        buffer.append(line)

    flush()
    return out


def chunk_pages(
    pages: dict[int, str],
    target_chars: int = 1800,
    max_chars: int = 4000,
) -> list[Chunk]:
    """Chunk a whole document, given ``{page_number: markdown}``.

    Blocks accumulate until the target size is reached; a heading closes the
    current chunk so sections start cleanly. Oversized single blocks (a long
    equation array, say) are emitted whole rather than cut.
    """
    blocks: list[_Block] = []
    for page_number in sorted(pages):
        blocks.extend(_blocks(page_number, pages[page_number]))

    chunks: list[Chunk] = []
    current: list[_Block] = []
    heading: str | None = None
    ordinal = 0

    def emit() -> None:
        nonlocal current, ordinal
        if not current:
            return
        text = "\n\n".join("\n".join(b.lines).strip() for b in current).strip()
        if text:
            chunks.append(
                Chunk(
                    ordinal=ordinal,
                    page_start=min(b.page for b in current),
                    page_end=max(b.page for b in current),
                    heading=heading,
                    chunk_type=classify(heading, text),
                    content=text,
                )
            )
            ordinal += 1
        current = []

    for block in blocks:
        size = sum(len(line) for line in block.lines)
        current_size = sum(len(l) for b in current for l in b.lines)

        if block.is_heading:
            # A new heading starts a new chunk, unless the current one holds
            # nothing but the previous heading line.
            if current and not all(b.is_heading for b in current):
                emit()
                heading = block.heading
            elif current:
                heading = block.heading
            else:
                heading = block.heading
            current.append(block)
            continue

        if current_size and current_size + size > max_chars:
            emit()

        current.append(block)

        if sum(len(l) for b in current for l in b.lines) >= target_chars:
            emit()

    emit()
    return chunks
