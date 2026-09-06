r"""Line-level repairs to extracted text.

Handwritten notes break words across lines with a hyphen, and OCR handles that
inconsistently: sometimes the two halves arrive split, sometimes the word is
joined correctly but the trailing fragment is emitted a second time anyway.
Both leave the text unsearchable at exactly the word the writer thought
important enough to keep going.

The rules here are conservative and purely mechanical -- they join or drop a
fragment, never guess at a spelling.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# A continuation line: optional indent, a hyphen, then a lowercase word.
_CONTINUATION = re.compile(r"^\s*[-‐‑–]\s?(?=[^\W\dA-ZΑ-Ω_])")
# The word a line ends on, ignoring trailing punctuation.
_TRAILING_WORD = re.compile(r"([^\W\d_]{2,})\s*$")
_ENDS_HYPHENATED = re.compile(r"([^\W\d_]{2,})[-‐‑–]\s*$")


@dataclass
class TextRepairReport:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def _fragment(line: str) -> str | None:
    """The continuation word a line carries, if it is a continuation line."""
    match = _CONTINUATION.match(line)
    if not match:
        return None
    rest = line[match.end() :]
    word = re.match(r"[^\W\d_]+", rest)
    return word.group(0) if word else None


def rejoin_hyphenation(text: str) -> TextRepairReport:
    """Repair words broken across a line.

    Two shapes appear, and they need opposite treatment:

    * ``παραγο-`` / ``-ντική`` -- a genuine split, so the halves are joined.
    * ``ολοκλήρωμα`` / ``-ρωμα`` -- the word was already joined and the tail
      was emitted again, so the duplicate is dropped.
    """
    lines = text.split("\n")
    out: list[str] = []
    counts: dict[str, int] = {}
    index = 0

    while index < len(lines):
        line = lines[index]
        fragment = _fragment(line)
        if fragment is None:
            out.append(line)
            index += 1
            continue

        # Find the last non-blank line already emitted.
        previous = len(out) - 1
        while previous >= 0 and not out[previous].strip():
            previous -= 1
        if previous < 0:
            out.append(line)
            index += 1
            continue

        head = out[previous]
        split = _ENDS_HYPHENATED.search(head)
        if split:
            rest = line[_CONTINUATION.match(line).end() :]
            out[previous] = head[: split.start()] + split.group(1) + rest
            # Drop the blank lines that separated the two halves.
            del out[previous + 1 :]
            counts["hyphen_rejoined"] = counts.get("hyphen_rejoined", 0) + 1
            index += 1
            continue

        tail = _TRAILING_WORD.search(head)
        if tail and tail.group(1).endswith(fragment) and len(fragment) >= 2:
            rest = line[_CONTINUATION.match(line).end() + len(fragment) :]
            if rest.strip():
                # Keep whatever followed the duplicated fragment.
                out.append(rest.lstrip())
            counts["hyphen_duplicate_dropped"] = (
                counts.get("hyphen_duplicate_dropped", 0) + 1
            )
            index += 1
            continue

        out.append(line)
        index += 1

    repaired = "\n".join(out)
    repaired = re.sub(r"\n{3,}", "\n\n", repaired)
    return TextRepairReport(text=repaired, counts=counts)
