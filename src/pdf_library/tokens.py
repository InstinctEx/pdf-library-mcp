"""Cheap token estimation.

The point is not to match any tokeniser exactly, it is to stop a tool result
from silently dumping 40k tokens into the model's context. Estimates run
slightly high, which is the safe direction for a budget check.
"""

from __future__ import annotations

import re

_WORDS = re.compile(r"\w+", re.UNICODE)
_NON_LATIN = re.compile(r"[^\x00-\x7F]")


def estimate_tokens(text: str) -> int:
    """Approximate the token count of a piece of text.

    Latin text runs near 4 characters per token. Greek and mathematical
    symbols cost far more, often close to one token per character, so their
    share is counted separately.
    """
    if not text:
        return 0
    non_latin = len(_NON_LATIN.findall(text))
    latin = len(text) - non_latin
    return max(1, int(latin / 3.6 + non_latin * 0.85))


def truncate_to_tokens(text: str, max_tokens: int) -> tuple[str, bool]:
    """Cut text to a token budget at a paragraph or line boundary."""
    if estimate_tokens(text) <= max_tokens:
        return text, False

    # Binary search on characters, then back off to a clean break.
    low, high = 0, len(text)
    while low < high:
        mid = (low + high + 1) // 2
        if estimate_tokens(text[:mid]) <= max_tokens:
            low = mid
        else:
            high = mid - 1

    cut = text[:low]
    for boundary in ("\n\n", "\n", " "):
        index = cut.rfind(boundary)
        if index > low * 0.6:
            cut = cut[:index]
            break
    return cut.rstrip(), True
