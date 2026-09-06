r"""Light repairs applied to extracted Markdown.

Both engines report some superscripts as HTML ``<sup>`` tags, so ``O(n^2)``
arrives as ``O(n<sup>2</sup> )``. That is readable but it is not LaTeX, it does
not survive Markdown rendering well, and it makes the math detector blind to
pages that are in fact full of formulas.

The conversion here is deliberately conservative. It only touches sup/sub tags
whose body is short and simple; anything else is left exactly as extracted.
Real LaTeX still comes from the quality engine.
"""

from __future__ import annotations

import re

_SUP = re.compile(r"<sup>\s*(.{1,24}?)\s*</sup>", re.DOTALL)
_SUB = re.compile(r"<sub>\s*(.{1,24}?)\s*</sub>", re.DOTALL)
# The atom a script attaches to: a short identifier, a number, or a closing
# bracket. The length cap and the lookbehind stop a footnote marker after an
# ordinary word ("note<sup>1</sup>") from being read as an exponent.
_ATOM = re.compile(r"(?<![^\W\d_])([A-Za-zͰ-Ͽ]\w{0,2}|\d+|\))\s*$")
# Bodies we are willing to convert: no nested markup, no prose.
_SIMPLE_BODY = re.compile(r"^[\w+\-*/.,^_{}()−\s]{1,16}$")
# An exponent is "2", "n", "1.5", "4/3", "n+1" -- never a word. A run of four
# or more letters means the extractor swept prose into the tag.
_WORDLIKE = re.compile(r"[^\W\d_]{4,}")
_HAS_SCRIPT_TAG = re.compile(r"</?su[bp]>")


def _script_body(raw: str) -> str | None:
    body = raw.strip()
    if not body or not _SIMPLE_BODY.match(body):
        return None
    if _HAS_SCRIPT_TAG.search(body):
        return None
    if _WORDLIKE.search(body):
        return None
    return body


def _convert(text: str, pattern: re.Pattern[str], operator: str) -> str:
    out: list[str] = []
    cursor = 0
    for match in pattern.finditer(text):
        body = _script_body(match.group(1))
        if body is None:
            continue
        head = text[cursor : match.start()]
        atom = _ATOM.search(head)
        if atom:
            # Pull the base into the math span: n<sup>2</sup> -> $n^{2}$
            out.append(head[: atom.start()])
            out.append(f"${atom.group(1)}{operator}{{{body}}}$")
        else:
            out.append(head)
            out.append(f"${operator}{{{body}}}$")
        cursor = match.end()
    out.append(text[cursor:])
    return "".join(out)


def _merge_adjacent(text: str) -> str:
    """Join ``$a^{2}$$_{i}$`` produced by a base carrying both scripts."""
    return re.sub(r"\$\$(?=[\^_])", "", text)


def repair_html_scripts(markdown: str) -> str:
    """Convert simple HTML scripts to inline LaTeX, leaving the rest alone."""
    if "<sup>" not in markdown and "<sub>" not in markdown:
        return markdown
    text = _convert(markdown, _SUP, "^")
    text = _convert(text, _SUB, "_")
    return _merge_adjacent(text)
