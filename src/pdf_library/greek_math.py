r"""Repairs for Greek mathematical notation.

Greek textbooks write the trigonometric functions with Greek names -- ημ for
sine, συν for cosine, εφ for tangent -- and no OCR model trained on Latin
mathematics knows them. Two things go wrong:

* The characters are misread. In handwriting the σ of συν looks like a 6 and
  the υν like 0v, so ``συνx`` is transcribed faithfully but wrongly as
  ``60vx``, which is not mathematics at all.
* Even when the characters are right, they are typeset as separate variables:
  ``ημx`` becomes ``\eta \mu x``, which renders as the product η·μ·x rather
  than sin(x).

Both are deterministic to fix, because the set of Greek function names is
small and closed. The repairs apply only inside math spans, so ordinary words
that merely start with these letters -- ημέρα, εφαρμογή -- are never touched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# The closed set of Greek function names, longest first so that τοξημ is
# matched before ημ.
FUNCTIONS: tuple[str, ...] = (
    "τοξσυν",
    "τοξημ",
    "τοξεφ",
    "τοξσφ",
    "συν",
    "εφ",
    "σφ",
    "ημ",
    "σεχ",
)

_DISPLAY_MATH = re.compile(r"(?<!\\)\$\$(.+?)(?<!\\)\$\$", re.DOTALL)
_INLINE_MATH = re.compile(r"(?<!\\)\$(?!\$)(.+?)(?<!\\)\$(?!\$)", re.DOTALL)

# Something a function name can legitimately be applied to.
_ARGUMENT = r"(?=\s*(?:\\left)?[\(\[a-zA-Zα-ωΑ-Ω0-9\\])"

# συν misread from handwriting: the sigma becomes 6 and the upsilon a 0 or o,
# with the nu arriving as v, ν or \nu. Requiring a following argument keeps
# ordinary numbers ("6th", "6e") out of it.
_CORRUPT_COS = re.compile(r"6[0OoΟο]?\s*(?:\\nu\b|[vν])" + _ARGUMENT)

# Greek letters that TeX-ified into commands: \eta\mu, \sigma\upsilon\nu, ...
_LETTER_COMMANDS = {
    "eta": "η",
    "mu": "μ",
    "sigma": "σ",
    "upsilon": "υ",
    "nu": "ν",
    "epsilon": "ε",
    "varepsilon": "ε",
    "phi": "φ",
    "varphi": "φ",
    "tau": "τ",
    "omicron": "ο",
    "xi": "ξ",
    "chi": "χ",
}
_COMMAND_RUN = re.compile(
    r"(?:\\(?:" + "|".join(_LETTER_COMMANDS) + r")\s*){2,}"
)

_ALREADY_WRAPPED = re.compile(r"\\(?:operatorname|mathrm|text)\{[^}]*\}")

# The Greek article η is a single letter, and OCR reads it as a Latin h or n
# and then, because it stands alone, files it as a mathematical variable:
# "η δυσκολία" becomes "$h$ δυσκολία". A bare one-letter math span followed by
# an ordinary Greek word is the article, not a variable -- a real variable in
# this position would be applied to something, as h(x) or h_1.
_GREEK_LOWER = "α-ωάέήίόύώϊϋΐΰ"
_ARTICLE_AS_VARIABLE = re.compile(
    r"(?<![\\\w])\$\s*h\s*\$(?=\s+[" + _GREEK_LOWER + r"]{4,})"
)


@dataclass
class RepairReport:
    text: str
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())


def looks_greek_mathematical(text: str) -> bool:
    """True when a document plausibly uses Greek function names.

    Used as a guard: the corrupted-cosine repair is only safe in a document
    that demonstrably works in this notation.
    """
    # Deliberately does not count the corrupted pattern itself: using it as
    # evidence would make the gate self-fulfilling, letting any "6v" in an
    # English document authorise its own rewrite.
    return bool(re.search(r"\\eta\s*\\mu|ημ|συν|\bεφ\b", text))


def _join_letter_commands(fragment: str) -> tuple[str, int]:
    """Turn a run like ``\\eta \\mu`` into the plain letters ``ημ``."""
    count = 0

    def repl(match: re.Match[str]) -> str:
        nonlocal count
        letters = "".join(
            _LETTER_COMMANDS[name]
            for name in re.findall(r"\\([a-z]+)", match.group(0))
        )
        # Only collapse when the result is one of the function names; an
        # unrelated run of Greek variables must stay as it is.
        if letters in FUNCTIONS:
            count += 1
            return letters
        return match.group(0)

    return _COMMAND_RUN.sub(repl, fragment), count


def _wrap_functions(fragment: str) -> tuple[str, int]:
    """Mark function names as operators so they render upright, not as a product."""
    count = 0
    pattern = re.compile("(" + "|".join(FUNCTIONS) + ")" + _ARGUMENT)

    def repl(match: re.Match[str]) -> str:
        nonlocal count
        count += 1
        return f"\\operatorname{{{match.group(1)}}}"

    pieces: list[str] = []
    cursor = 0
    # Skip anything already inside \operatorname{...} or \text{...}.
    for guard in _ALREADY_WRAPPED.finditer(fragment):
        pieces.append(pattern.sub(repl, fragment[cursor : guard.start()]))
        pieces.append(guard.group(0))
        cursor = guard.end()
    pieces.append(pattern.sub(repl, fragment[cursor:]))
    return "".join(pieces), count


def _repair_fragment(fragment: str, allow_cosine: bool) -> tuple[str, dict[str, int]]:
    counts: dict[str, int] = {}

    if allow_cosine:
        fragment, cos_count = _CORRUPT_COS.subn("συν", fragment)
        if cos_count:
            counts["misread_cosine"] = cos_count

    fragment, joined = _join_letter_commands(fragment)
    if joined:
        counts["split_function_name"] = joined

    fragment, wrapped = _wrap_functions(fragment)
    if wrapped:
        counts["operator_markup"] = wrapped

    return fragment, counts


def repair_article(markdown: str) -> tuple[str, int]:
    """Turn a lone ``$h$`` back into the Greek article it was."""
    return _ARTICLE_AS_VARIABLE.subn("η", markdown)


def repair(markdown: str, allow_cosine: bool | None = None) -> RepairReport:
    """Repair Greek function names inside the math spans of one page.

    ``allow_cosine`` guards the aggressive misread-cosine rule; when left as
    None it is decided from this text alone, but a caller with the whole
    document should decide from that instead.
    """
    if allow_cosine is None:
        allow_cosine = looks_greek_mathematical(markdown)

    counts: dict[str, int] = {}

    def repl(match: re.Match[str], delimiter: str) -> str:
        body, found = _repair_fragment(match.group(1), allow_cosine)
        for key, value in found.items():
            counts[key] = counts.get(key, 0) + value
        return f"{delimiter}{body}{delimiter}"

    text = _DISPLAY_MATH.sub(lambda m: repl(m, "$$"), markdown)
    text = _INLINE_MATH.sub(lambda m: repl(m, "$"), text)

    if allow_cosine:
        text, articles = repair_article(text)
        if articles:
            counts["article_as_variable"] = articles

    return RepairReport(text=text, counts=counts)
