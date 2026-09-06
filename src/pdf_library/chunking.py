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

from .normalize import fold
from .quality import count_math

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")

# Lecture notes and handwritten material carry no Markdown headings at all;
# their structure is carried by words. These are the words Greek and English
# mathematical texts use to open a unit of thought, matched on their folded
# stems so that accents and case do not matter.
_SECTION_WORDS: tuple[str, ...] = (
    "παραδειγμ", "εφαρμογ", "ασκησ", "λυσ", "περιπτωσ", "παρατηρησ",
    "θεωρημ", "ορισμ", "αποδειξ", "προτασ", "λημμ", "πορισμ", "σημειωσ",
    "μεθοδ", "κριτηρι", "βημ", "ερωτησ", "συμπερασμ",
    "example", "exercise", "solution", "theorem", "definition", "proof",
    "lemma", "corollary", "remark", "proposition", "method", "step", "case",
)
# The marker word, an optional number ("Περίπτωση 2", "Θεώρημα 2.5"), then a
# separator or the end of the line.
_SECTION_MARKER = re.compile(
    r"^\s{0,3}[*_>#\s]{0,4}"
    r"(?P<word>[^\W\d_]{3,20})"
    r"(?P<number>\s+\d+(?:\.\d+)*)?"
    r"\s*(?P<sep>[:.\u0387)\]]|\s|$)"
)
_FENCE = re.compile(r"^\s*(```|~~~)")
_DISPLAY_OPEN = re.compile(r"(?<!\\)\$\$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")

# Chunk type by the word that opens the passage, matched on folded stems so
# that case, accents and inflection do not matter.
_TYPE_STEMS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("theorem", ("θεωρημ", "theorem")),
    ("lemma", ("λημμ", "lemma")),
    ("proof", ("αποδειξ", "proof")),
    ("definition", ("ορισμ", "definition")),
    ("example", ("παραδειγμ", "example")),
    ("corollary", ("πορισμ", "corollary")),
    ("remark", ("παρατηρησ", "σημειωσ", "remark", "note")),
    ("proposition", ("προτασ", "proposition")),
    ("exercise", ("ασκησ", "ερωτησ", "exercise")),
    ("solution", ("λυσ", "solution")),
    ("case", ("περιπτωσ", "case")),
    ("step", ("βημ", "step")),
    ("method", ("μεθοδ", "κριτηρι", "method")),
    ("application", ("εφαρμογ", "application")),
)


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
    """Label a chunk by the mathematical object it appears to hold.

    The heading decides when there is one, since a passage opening with
    "Απόδειξη" is a proof whatever else it mentions; only then does the body
    get a say.
    """
    if heading:
        folded = fold(heading)
        for label, stems in _TYPE_STEMS:
            if any(folded.startswith(stem) for stem in stems):
                return label

    probe = fold(f"{heading or ''}\n{body[:160]}")
    for label, stems in _TYPE_STEMS:
        if any(stem in probe for stem in stems):
            return label
    if body.count("$$") >= 2 and len(body.strip()) < 400:
        return "equation"
    if sum(1 for line in body.splitlines() if _TABLE_ROW.match(line)) >= 3:
        return "table"
    return "paragraph"


def section_marker(line: str) -> str | None:
    """Return the heading a line opens, if it opens one.

    Returns the marker phrase itself ("Περίπτωση 2") rather than the whole
    line, so that a heading stays short enough to be useful in a search result.
    """
    stripped = line.strip()
    if not stripped or len(stripped) > 200:
        return None
    match = _SECTION_MARKER.match(stripped)
    if not match:
        return None
    word = match.group("word")
    folded = fold(word)
    if not any(folded.startswith(root) for root in _SECTION_WORDS):
        return None
    number = (match.group("number") or "").strip()
    return f"{word} {number}".strip()


@dataclass
class _Block:
    """An atomic run of lines that must not be split."""

    lines: list[str]
    page: int
    heading: str | None
    # A line that is nothing but a heading, such as "## Proof".
    is_heading: bool
    # A line that announces a section and also carries its first sentence,
    # which is how unstructured lecture notes are written: "Λύση Θέτουμε u = x".
    starts_section: bool = False


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

        marker = section_marker(line)
        if marker:
            # The line is both the heading and the start of the body, so it is
            # kept whole and only tagged with the section it announces.
            flush()
            out.append(
                _Block([line], page_number, marker, False, starts_section=True)
            )
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

        if block.starts_section:
            # This line carries content, so it always opens a fresh chunk.
            emit()
            heading = block.heading
            current.append(block)
            continue

        if block.is_heading:
            # A bare heading opens a new chunk, unless the current one holds
            # nothing but the previous heading line, in which case the two
            # headings belong together.
            if current and not all(b.is_heading for b in current):
                emit()
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
