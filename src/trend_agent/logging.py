"""Structured JSON logging with secret redaction.

Logs go to stderr so that CLI output on stdout stays machine-readable. Use
:func:`log_event` to emit an event name plus structured fields; anything that looks like a
credential is redacted before it is serialised.
"""

from __future__ import annotations

import json
import logging
import math
import re
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

_SENSITIVE_KEY = re.compile(
    r"(api[_-]?key|^key$|token|secret|password|passwd|authorization|credential|cookie)",
    re.IGNORECASE,
)
# key=..., access_token=..., Bearer ... in free text (URLs, exception messages).
_SENSITIVE_TEXT = [
    re.compile(r"(?i)\b((?:api_?)?key|access_token|token|password)=([^&\s\"']+)"),
    re.compile(r"(?i)\b(bearer)\s+([A-Za-z0-9._~+/=-]+)"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),  # Google API key shape
    # GitHub token shapes: classic/OAuth/user/server/refresh tokens and fine-grained PATs
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"(?i)(postgres(?:ql)?(?:\+\w+)?://[^:/\s]+:)([^@\s]+)(@)"),
]
REDACTED = "***REDACTED***"

_RESERVED_ATTRS = frozenset(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


def redact_text(text: str) -> str:
    """Remove credential-like substrings from free text."""
    for pattern in _SENSITIVE_TEXT:
        if pattern.groups >= 3:
            text = pattern.sub(rf"\g<1>{REDACTED}\g<3>", text)
        elif pattern.groups == 2:
            text = pattern.sub(rf"\g<1>={REDACTED}", text)
        else:
            text = pattern.sub(REDACTED, text)
    return text


def redact(value: Any, *, _key: str | None = None) -> Any:
    """Recursively redact sensitive keys and credential-like strings."""
    if _key is not None and _SENSITIVE_KEY.search(_key):
        return REDACTED
    if isinstance(value, Mapping):
        return {str(k): redact(v, _key=str(k)) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def json_safe(value: Any) -> Any:
    """Make a structure storable as JSON/JSONB: non-finite floats become strings.

    PostgreSQL JSONB (and strict JSON) cannot represent NaN or +/-Infinity, which is
    exactly what quarantined records may contain.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, Mapping):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [json_safe(v) for v in value]
    return value


def storable(value: Any) -> Any:
    """Redact secrets and make JSON-safe: use for every JSONB payload we persist."""
    return json_safe(redact(value))


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_type"] = record.exc_info[0].__name__
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(redact(payload), default=str, sort_keys=False)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extras = {
            k: v
            for k, v in record.__dict__.items()
            if k not in _RESERVED_ATTRS and not k.startswith("_")
        }
        base = f"{record.levelname:<7} {record.name}: {record.getMessage()}"
        if extras:
            base += " " + json.dumps(redact(extras), default=str)
        return redact_text(base)


# Correlation ID of the current request / scheduled run; added to every log record.
_CORRELATION: ContextVar[str | None] = ContextVar("trend_agent_correlation_id", default=None)
_CORRELATION_RE = re.compile(r"^[A-Za-z0-9._:-]{8,64}$")


def current_correlation_id() -> str | None:
    return _CORRELATION.get()


def valid_correlation_id(value: str | None) -> bool:
    return bool(value and _CORRELATION_RE.match(value))


@contextmanager
def correlation(cid: str) -> Iterator[str]:
    """Bind ``cid`` to log records emitted inside the block (task- and thread-local)."""
    token = _CORRELATION.set(cid)
    try:
        yield cid
    finally:
        _CORRELATION.reset(token)


class CorrelationFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        cid = _CORRELATION.get()
        if cid is not None and not hasattr(record, "correlation_id"):
            record.correlation_id = cid
        return True


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    root = logging.getLogger("trend_agent")
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(CorrelationFilter())
    root.addHandler(handler)
    root.setLevel(level)
    root.propagate = False
    # httpx logs full request URLs (which would include the API key) at INFO.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    if not name.startswith("trend_agent"):
        name = f"trend_agent.{name}"
    return logging.getLogger(name)


def log_event(logger: logging.Logger, event: str, level: int = logging.INFO, **fields: Any) -> None:
    logger.log(level, event, extra={k: v for k, v in fields.items() if k not in _RESERVED_ATTRS})
