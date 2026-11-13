"""Untrusted-content handling (prompt-injection protection, data side).

Retrieved text - YouTube titles and descriptions, comments, feeds, GitHub pages,
community posts - is **data**. The controls, in order of importance:

1. **Architecture.** Generators have no tools that act: they cannot run code, read
   credentials, write to the database or make network requests (``permissions.py``).
   Their output is parsed into a fixed schema and every factual element is verified by
   application code, which also makes the publication decision. Injected text can at
   worst make a draft wrong; the gate then removes or withholds it.
2. **Separation.** Source text is placed only inside a JSON-encoded data block with a
   per-request random nonce; ``<`` and ``>`` are escaped, so content cannot close the
   block or impersonate instructions. Trusted instructions travel in the system prompt.
3. **Sanitisation.** Control, zero-width and bidirectional-override characters are
   removed and text is length-bounded.
4. **Signals.** ``injection_signals`` flags instruction-like text. This is a heuristic
   used to label records and to forbid a flagged record from verifying claims on its
   own; it is never the only defence.
"""

from __future__ import annotations

import json
import re
import secrets
import unicodedata
from collections.abc import Iterable, Mapping
from typing import Any

DELIMITER = "untrusted_source_data"

_HIDDEN = re.compile("[​-‏‪-‮⁠-⁤⁦-⁩﻿]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

_SIGNALS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "override_instructions",
        re.compile(
            r"\b(ignore|disregard|forget|override)\b.{0,40}\b(instruction|prompt|rule|"
            r"guideline|system|previous|above)s?\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "role_injection",
        re.compile(
            r"(^|\n)\s*(system|assistant|developer)\s*:|<\s*/?\s*(system|assistant|"
            r"instructions?)\s*>|\byou are now\b|\bnew instructions\b|\bact as\b",
            re.IGNORECASE,
        ),
    ),
    (
        "verification_tampering",
        re.compile(
            r"\b(mark|label|treat|report|classify)\b.{0,40}\b(verified|confirmed|"
            r"trusted|authoritative)\b|\bverification[_ ]status\b|\bset\b.{0,20}\b("
            r"severity|score|confidence)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "statistic_injection",
        re.compile(
            r"\b(report|say|state|claim)\b.{0,60}\b\d[\d,.]*\s*(k|m|million|thousand)?\s*"
            r"(searches|search volume|queries)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "code_execution",
        re.compile(
            r"\b(execute|run|eval)\b.{0,30}\b(command|code|script|shell|following)\b|"
            r"\b(curl|wget|powershell|bash -c|rm -rf|subprocess|os\.system)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "database_modification",
        re.compile(
            r"\b(drop\s+table|delete\s+from|truncate\s+table|insert\s+into|update\s+\w+\s+set|"
            r"alter\s+table)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "credential_access",
        re.compile(
            r"\b(api[ _-]?key|password|secret[ _-]?key|client[ _-]?secret|access[ _-]?token|"
            r"credentials?|\.env\b|ssh key|private key)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "exfiltration",
        re.compile(
            r"\b(send|post|upload|forward|exfiltrate)\b.{0,40}\b(https?://|webhook|endpoint|"
            r"url)\b",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
)


def injection_signals(text: str | None) -> tuple[str, ...]:
    """Heuristic flags for instruction-like content (a signal, not a security boundary)."""
    if not text:
        return ()
    flags = [name for name, pattern in _SIGNALS if pattern.search(text)]
    if _HIDDEN.search(text):
        flags.append("hidden_characters")
    return tuple(flags)


def sanitize(text: str | None, limit: int = 4000) -> str:
    """Remove control/hidden characters, neutralise the delimiter, bound the length."""
    if not text:
        return ""
    t = unicodedata.normalize("NFKC", text)
    t = _HIDDEN.sub("", t)
    t = _CONTROL.sub(" ", t)
    t = re.sub(DELIMITER, "[removed-delimiter]", t, flags=re.IGNORECASE)
    t = re.sub(r"[ \t]+", " ", t).strip()
    if len(t) > limit:
        t = t[:limit].rstrip() + " [truncated]"
    return t


def new_nonce() -> str:
    return secrets.token_hex(8)


def encode_data(value: Any) -> str:
    """JSON with ``<``, ``>`` and ``&`` escaped so data cannot form markup or close a block."""
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, indent=1)
    return text.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")


def render_data_block(records: Iterable[Mapping[str, Any]], nonce: str) -> str:
    body = encode_data(list(records))
    return f'<{DELIMITER} nonce="{nonce}">\n{body}\n</{DELIMITER} nonce="{nonce}">'


SUSPICIOUS_OUTPUT = re.compile(
    r"\b(ignore|disregard)\b.{0,30}\b(verification|instruction|rule)s?\b|"
    r"\b(click|visit|go to)\b.{0,20}https?://|\bapi[ _-]?key\b|\bpassword\b|"
    r"\b(execute|run)\b.{0,20}\b(command|script|code)\b|\bsystem prompt\b|"
    # The generator may not describe its own output as checked or approved.
    r"\b(verified|approved|fact[- ]checked|certified|validated|cleared)\b.{0,40}"
    r"\b(system|verifier|verification|publication|publishing|moderat\w*|reviewers?)\b",
    re.IGNORECASE | re.DOTALL,
)
URL = re.compile(r"https?://[^\s)\]>\"']+", re.IGNORECASE)
