"""Text normalisation helpers."""

from __future__ import annotations

import re
import unicodedata

MAX_QUERY_LENGTH = 200

_ZERO_WIDTH = re.compile("[​‌‍⁠﻿]")
_WHITESPACE = re.compile(r"\s+")


class QueryValidationError(ValueError):
    pass


def canonicalize_query(query: str) -> str:
    """Return the canonical form of a search query.

    The canonical form is used for de-duplication only; the original text is always
    preserved separately. The transformation is deliberately conservative: Unicode NFKC
    normalisation, case folding, removal of zero-width characters, and whitespace
    collapsing. Punctuation is kept because it can change meaning (``c++`` vs ``c``).
    """
    if not isinstance(query, str):
        raise QueryValidationError("query must be a string")
    text = unicodedata.normalize("NFKC", query)
    text = _ZERO_WIDTH.sub("", text)
    text = _WHITESPACE.sub(" ", text).strip().casefold()
    if not text:
        raise QueryValidationError("query is empty after normalisation")
    if len(text) > MAX_QUERY_LENGTH:
        raise QueryValidationError(f"query exceeds {MAX_QUERY_LENGTH} characters")
    if any(unicodedata.category(ch) == "Cc" for ch in text):
        raise QueryValidationError("query contains control characters")
    return text


def clean_original_query(query: str) -> str:
    """Trim and collapse whitespace while preserving case and punctuation."""
    return _WHITESPACE.sub(" ", _ZERO_WIDTH.sub("", query)).strip()
