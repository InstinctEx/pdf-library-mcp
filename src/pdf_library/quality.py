"""Deterministic per-page quality checks.

No model is involved. The gate answers one question: is this page's Markdown
good enough to keep, or should the quality engine have a go at it?
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_DISPLAY_MATH = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_INLINE_MATH = re.compile(r"(?<!\\)(?<!\$)\$(?!\$)(.+?)(?<!\\)\$(?!\$)", re.DOTALL)
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$", re.MULTILINE)
# Replacement char and the private-use range that broken CID fonts land in.
_GARBLED = re.compile(r"[�-]")
_LETTERS = re.compile(r"[^\W\d_]", re.UNICODE)
_LATEX_COMMAND = re.compile(r"\\[a-zA-Z]+")

# A function label as it is written under a term: f(x), g(x), g'(x), h(x).
_LABEL = r"[a-zA-Zφψ]'?\(x\)"
# Underbrace annotations are the one error that produces valid LaTeX meaning
# something else entirely: "x·cos(2x) with f(x) and g'(x) written underneath"
# is extracted as a fraction over those labels. Two or more labels multiplied
# together in a denominator is the signature -- dividing by f(x)·g'(x) is a
# thing almost no text actually does.
_LABEL_DENOMINATOR = re.compile(
    r"\\frac\{[^{}]*\}\{\s*" + _LABEL + r"(?:\s*\\cdot\s*" + _LABEL + r")+\s*\}"
)
# The weaker shape: a denominator that is nothing but one label.
_SINGLE_LABEL_DENOMINATOR = re.compile(
    r"\\frac\{[^{}]*\}\{\s*" + _LABEL + r"\s*\}"
)


@dataclass
class PageQuality:
    score: float
    state: str  # good | warning | bad
    issues: list[str] = field(default_factory=list)
    math_count: int = 0
    table_count: int = 0
    char_count: int = 0
    has_text: bool = True
    display_math_count: int = 0


def count_math(markdown: str) -> int:
    return len(_DISPLAY_MATH.findall(markdown)) + len(_INLINE_MATH.findall(markdown))


def _unmatched_dollars(text: str) -> bool:
    """True when $ delimiters cannot pair up.

    Escaped ``\\$`` (a literal dollar sign) is removed first so prices do not
    read as broken math.
    """
    stripped = re.sub(r"\\\$", "", text)
    without_display = _DISPLAY_MATH.sub("", stripped)
    if without_display.count("$$") % 2 != 0:
        return True
    return without_display.count("$") % 2 != 0


def assess_page(markdown: str) -> PageQuality:
    issues: list[str] = []
    text = markdown.strip()
    char_count = len(text)
    math_count = count_math(markdown)
    display_count = len(_DISPLAY_MATH.findall(markdown))
    table_count = len(_TABLE_ROW.findall(markdown))

    if not text:
        return PageQuality(
            score=0.0,
            state="bad",
            issues=["empty_page"],
            char_count=0,
            has_text=False,
        )

    score = 1.0

    letters = len(_LETTERS.findall(text))
    if letters < 20:
        issues.append("almost_no_text")
        score -= 0.5

    garbled = len(_GARBLED.findall(text))
    garbled_ratio = garbled / max(char_count, 1)
    if garbled_ratio > 0.02:
        issues.append(f"garbled_chars:{garbled_ratio:.2%}")
        score -= min(0.6, garbled_ratio * 10)

    if _unmatched_dollars(markdown):
        issues.append("unbalanced_math_delimiters")
        score -= 0.3

    for match in _DISPLAY_MATH.finditer(markdown):
        body = match.group(1)
        if body.count("{") != body.count("}"):
            issues.append("unbalanced_braces_in_display_math")
            score -= 0.15
            break

    # A LaTeX command running straight into prose usually means the equation
    # boundary was lost, e.g. "\\int f dx converges" outside any $ pair.
    outside = _DISPLAY_MATH.sub(" ", markdown)
    outside = _INLINE_MATH.sub(" ", outside)
    stray = len(_LATEX_COMMAND.findall(outside))
    if stray > 2:
        issues.append(f"latex_outside_math:{stray}")
        score -= min(0.3, stray * 0.05)

    # Underbrace annotations misread as fractions. Once one is confirmed on a
    # page, the ambiguous single-label ones on the same page are the same
    # artefact, so they are counted too.
    confirmed = len(_LABEL_DENOMINATOR.findall(markdown))
    if confirmed:
        total = confirmed + len(_SINGLE_LABEL_DENOMINATOR.findall(markdown))
        issues.append(f"underbrace_as_fraction:{total}")
        score -= min(0.45, 0.15 * total)

    score = max(0.0, min(1.0, score))
    if score >= 0.75:
        state = "good"
    elif score >= 0.45:
        state = "warning"
    else:
        state = "bad"

    return PageQuality(
        score=round(score, 3),
        state=state,
        issues=issues,
        math_count=math_count,
        table_count=table_count,
        char_count=char_count,
        has_text=letters >= 20,
        display_math_count=display_count,
    )


# Unicode ranges that signal mathematics in plain-text extraction, where no
# LaTeX is produced. Used to decide which pages deserve the quality engine.
_MATH_UNICODE = re.compile(
    "[∀-⋿"  # mathematical operators
    "⟀-⟯"  # misc mathematical symbols A
    "⦀-⧿"  # misc mathematical symbols B
    "⨀-⫿"  # supplemental mathematical operators
    "℀-⅏"  # letterlike symbols
    "⁰-₟"  # super/subscripts
    "Ͱ-Ͽ"  # Greek, which is ambiguous in Greek documents
    "]"
)
_MATH_STRONG = re.compile("[∀-⋿⨀-⫿⁰-₟]")


def math_density(text: str, greek_document: bool = False) -> float:
    """Rough share of characters that look mathematical.

    In a Greek document the Greek block is ordinary prose, so only the strong
    ranges (operators, super/subscripts) count.
    """
    if not text:
        return 0.0
    pattern = _MATH_STRONG if greek_document else _MATH_UNICODE
    return len(pattern.findall(text)) / len(text)


def looks_mathematical(markdown: str, greek_document: bool = False) -> bool:
    """True when a page is worth sending to the LaTeX-capable engine."""
    if count_math(markdown) > 0:
        return True
    return math_density(markdown, greek_document) > 0.004
