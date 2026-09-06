"""A light stemmer for Greek search terms.

Greek is heavily inflected, and SQLite's unicode61 tokeniser does no stemming
at all, so "μερικά κλάσματα" fails to find "μερικών κλασμάτων" -- the same
phrase in a different case. Accent folding alone does not help, because the
endings genuinely differ.

This strips the common inflectional endings so that every form of a word
reaches the index as one token. It is deliberately shallow: it removes a
suffix only when a stem of at least three characters survives, which keeps
short words intact and avoids collapsing unrelated words together. Latin-script
tokens are left exactly as they are.

Input is expected to be already accent-folded and lower-cased by
``normalize.fold`` -- in particular final sigma is already ``σ``.
"""

from __future__ import annotations

import re

_MIN_STEM = 3

# Longest first: the first match wins, so "ατων" is tried before "ων".
_SUFFIXES: tuple[str, ...] = (
    # -μα / -ματος family: ολοκληρωμα, ολοκληρωματοσ, ολοκληρωματων
    "ατοσ", "ατων", "ατα", "ατι",
    # participles and verb endings
    "οντασ", "ωντασ", "ουμε", "ουσα", "ουσεσ", "ονται", "ονταν",
    "ησουμε", "ισουμε", "ετε", "ουν", "ει", "εισ",
    # nouns and adjectives
    "εων", "εωσ", "ιων", "ιωσ", "ουσ", "ων", "ησ", "εσ", "οσ", "ου",
    "ασ", "οι", "εί", "α", "ε", "η", "ι", "ο", "υ", "ω", "σ",
)

_HAS_GREEK = re.compile(r"[α-ωΑ-Ω]")
_TOKEN = re.compile(r"\w+", re.UNICODE)


def stem_word(word: str) -> str:
    """Strip one inflectional ending from an accent-folded Greek word."""
    if len(word) <= _MIN_STEM or not _HAS_GREEK.search(word):
        return word
    for suffix in _SUFFIXES:
        if word.endswith(suffix) and len(word) - len(suffix) >= _MIN_STEM:
            return word[: -len(suffix)]
    return word


def stem_text(text: str) -> str:
    """Stem every token in a piece of already-folded text."""
    return _TOKEN.sub(lambda m: stem_word(m.group(0)), text)
