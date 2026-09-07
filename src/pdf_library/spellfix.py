"""Conservative, confusion-constrained repair for Greek OCR prose.

This is deliberately not a general spellchecker. A correction is made only
when a local lexicon supplies exactly one word reachable through the handful
of character confusions observed in handwritten Greek notes. The rule makes
every automatic change explainable and safe to audit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from .normalize import fold

_MATH_SPAN = re.compile(
    r"(?<!\\)\$\$(.+?)(?<!\\)\$\$|(?<!\\)\$(?!\$)(.+?)(?<!\\)\$(?!\$)",
    re.DOTALL,
)
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)
_GREEK = re.compile(r"[Ͱ-Ͽἀ-῿]")

# Pairs observed in the corpus.  They are symmetric; any other substitution
# rejects a candidate even when ordinary edit distance would accept it.
_CONFUSIONS = {
    frozenset(("γ", "χ")),
    frozenset(("η", "υ")),
    frozenset(("υ", "ι")),
    frozenset(("σ", "δ")),
    frozenset(("σ", "θ")),
    frozenset(("χ", "κ")),
    frozenset(("ν", "υ")),
}
_ALTERNATIVES: dict[str, set[str]] = {}
for pair in _CONFUSIONS:
    left, right = pair
    _ALTERNATIVES.setdefault(left, set()).add(right)
    _ALTERNATIVES.setdefault(right, set()).add(left)


@dataclass(frozen=True)
class SpellFixChange:
    original: str
    replacement: str


@dataclass
class SpellFixReport:
    text: str
    changes: list[SpellFixChange] = field(default_factory=list)
    lexicon_available: bool = True

    @property
    def counts(self) -> dict[str, int]:
        return {"greek_prose_spelling": len(self.changes)} if self.changes else {}


def load_lexicon(path: Path | None) -> dict[str, str] | None:
    """Load a plain UTF-8 wordlist, preserving the preferred spelling.

    Blank lines and `#` comments are ignored.  A wordlist may include a
    frequency after whitespace; only its first field is used.
    """
    if path is None or not path.is_file():
        return None
    words: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        word = line.strip().split(maxsplit=1)[0] if line.strip() else ""
        if word and not word.startswith("#") and _GREEK.search(word):
            words.setdefault(fold(word), word)
    return words


@lru_cache(maxsize=4)
def _hunspell_dictionary(stem: str) -> Any | None:
    """Load a local Hunspell pair when the optional pure-Python runner exists."""
    path = Path(stem)
    if path.suffix in (".aff", ".dic"):
        path = path.with_suffix("")
    if not path.with_suffix(".aff").is_file() or not path.with_suffix(".dic").is_file():
        return None
    try:
        from spylls.hunspell import Dictionary

        return Dictionary.from_files(str(path))
    except (ImportError, OSError, UnicodeError, ValueError):
        return None


def _hunspell_candidates(word: str, dictionary: Any) -> list[str]:
    """Generate only the one/two confusion substitutions Hunspell accepts."""
    if dictionary.lookup(word):
        return []
    variants: set[str] = set()
    positions = [
        (index, _ALTERNATIVES.get(fold(char), set()))
        for index, char in enumerate(word)
    ]
    for index, alternatives in positions:
        for replacement in alternatives:
            if word[index].isupper():
                replacement = replacement.upper()
            one = word[:index] + replacement + word[index + 1 :]
            variants.add(one)
            for next_index, next_alternatives in positions[index + 1 :]:
                for next_replacement in next_alternatives:
                    if word[next_index].isupper():
                        next_replacement = next_replacement.upper()
                    variants.add(
                        one[:next_index]
                        + next_replacement
                        + one[next_index + 1 :]
                    )
    return sorted(candidate for candidate in variants if dictionary.lookup(candidate))


def _reachable(source: str, candidate: str) -> bool:
    """Whether two folded words differ by at most two allowed substitutions."""
    if len(source) != len(candidate):
        return False
    substitutions = 0
    for left, right in zip(source, candidate):
        if left == right:
            continue
        substitutions += 1
        if substitutions > 2 or frozenset((left, right)) not in _CONFUSIONS:
            return False
    return substitutions > 0


def _correct_prose(
    text: str,
    lexicon: dict[str, str] | None,
    hunspell: Any | None,
    changes: list[SpellFixChange],
) -> str:
    def replace(match: re.Match[str]) -> str:
        original = match.group(0)
        if len(original) < 4 or not _GREEK.search(original):
            return original
        source = fold(original)
        if lexicon and source in lexicon:
            return original
        if hunspell and hunspell.lookup(original):
            return original
        candidates: set[str] = set()
        if lexicon:
            candidates.update(
                spelling
                for folded, spelling in lexicon.items()
                if _reachable(source, folded)
            )
        if hunspell:
            candidates.update(_hunspell_candidates(original, hunspell))
        if len(candidates) != 1:
            return original
        replacement = next(iter(candidates))
        changes.append(SpellFixChange(original, replacement))
        return replacement

    return _WORD.sub(replace, text)


def repair_greek_prose(
    markdown: str,
    lexicon_path: Path | None = None,
    hunspell_path: Path | None = None,
) -> SpellFixReport:
    """Repair only prose segments, retaining all inline and display math exactly."""
    lexicon = load_lexicon(lexicon_path)
    hunspell = _hunspell_dictionary(str(hunspell_path)) if hunspell_path else None
    if not lexicon and not hunspell:
        return SpellFixReport(
            markdown, lexicon_available=lexicon is not None or hunspell is not None
        )

    changes: list[SpellFixChange] = []
    parts: list[str] = []
    cursor = 0
    for match in _MATH_SPAN.finditer(markdown):
        parts.append(
            _correct_prose(markdown[cursor : match.start()], lexicon, hunspell, changes)
        )
        parts.append(match.group(0))
        cursor = match.end()
    parts.append(_correct_prose(markdown[cursor:], lexicon, hunspell, changes))
    return SpellFixReport("".join(parts), changes=changes)
