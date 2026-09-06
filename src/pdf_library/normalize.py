r"""Text normalisation for the full-text index.

Two problems make raw Markdown a poor thing to hand to FTS5 directly:

* LaTeX. A block like ``$\int_{-\infty}^{\infty} f(x)\,dx$`` tokenises into
  noise (``int``, ``infty``, ``x``, ``dx``) that pollutes ranking without ever
  being what anyone searches for. We keep the readable command names as
  searchable words and drop the punctuation soup.
* Greek. SQLite's unicode61 tokeniser keeps accents and distinguishes the final
  sigma, so "συναρτήσεις" would not match "συνάρτησης". We fold accents and
  normalise final sigma before indexing, and apply the same folding to queries.
"""

from __future__ import annotations

import re
import unicodedata

from .greek import stem_text

# Commands worth keeping as search terms: someone may well search "integral"
# by typing "int", or look for "matrix" or "sum".
_MATH_WORD = re.compile(r"\\([a-zA-Z]{2,})")
_DISPLAY_MATH = re.compile(r"\$\$(.+?)\$\$", re.DOTALL)
_INLINE_MATH = re.compile(r"(?<!\\)\$(.+?)(?<!\\)\$", re.DOTALL)
_MATH_PUNCT = re.compile(r"[\\{}\[\]()^_&$~|]+")
_MD_SYNTAX = re.compile(r"^[#>\-*+ \t]+|[*_`]+", re.MULTILINE)
_WHITESPACE = re.compile(r"\s+")


def _strip_math(text: str) -> str:
    """Replace math spans with just their command names."""

    def repl(match: re.Match[str]) -> str:
        words = _MATH_WORD.findall(match.group(1))
        return " " + " ".join(words) + " " if words else " "

    text = _DISPLAY_MATH.sub(repl, text)
    text = _INLINE_MATH.sub(repl, text)
    return text


def fold(text: str) -> str:
    """Accent-fold and case-fold, keeping letters from every script.

    Greek final sigma is mapped onto the medial form so that inflected forms
    match each other.
    """
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = unicodedata.normalize("NFC", text).lower()
    return text.replace("ς", "σ")  # final sigma -> sigma


def normalize_for_index(markdown: str) -> str:
    """Turn a Markdown page or chunk into the text stored in the FTS index.

    Tokens are stemmed so that Greek inflections collapse onto one form; the
    identical stemming is applied to queries.
    """
    text = _strip_math(markdown)
    text = _MD_SYNTAX.sub(" ", text)
    text = _MATH_PUNCT.sub(" ", text)
    text = stem_text(fold(text))
    return _WHITESPACE.sub(" ", text).strip()


# Words shorter than this carry too little signal to be worth fuzzy matching.
_FUZZY_MIN_WORD = 4
_WORD = re.compile(r"\w+", re.UNICODE)


def trigrams(text: str) -> str:
    """Character trigrams of every long word, as space-separated tokens.

    This is the fallback index. OCR of handwriting produces errors that no
    stemmer or accent rule can undo -- "παραγοντική" read as "παραχουτική"
    differs in two places at once. Trigrams still share most of their pieces,
    so a query that matches nothing exactly can still be ranked against them.
    Deriving the trigrams from the already-stemmed text keeps the two indexes
    describing the same words.
    """
    out: list[str] = []
    for word in _WORD.findall(text):
        if len(word) < _FUZZY_MIN_WORD:
            continue
        out.extend(word[i : i + 3] for i in range(len(word) - 2))
    return " ".join(out)


def normalize_query(query: str) -> str:
    """Apply the same folding to a user query, preserving FTS5 operators."""
    quoted = query.count('"') >= 2
    text = stem_text(fold(query))
    if not quoted:
        # Drop characters FTS5 would read as syntax so a stray "(" cannot
        # turn a plain search into a malformed-query error.
        text = re.sub(r'[():"*^-]', " ", text)
    return _WHITESPACE.sub(" ", text).strip()
