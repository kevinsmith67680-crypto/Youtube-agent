"""UTC time helpers. All timestamps inside the application are timezone-aware UTC."""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class NaiveDatetimeError(ValueError):
    pass


def ensure_utc(value: datetime) -> datetime:
    """Convert an aware datetime to UTC. Naive datetimes are rejected, never assumed."""
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise NaiveDatetimeError(f"timezone-naive datetime is not allowed: {value.isoformat()}")
    return value.astimezone(UTC)


def parse_rfc3339(value: str) -> datetime:
    """Parse an RFC 3339 timestamp (as returned by Google APIs) into aware UTC."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return ensure_utc(parsed)


def format_rfc3339(value: datetime) -> str:
    return ensure_utc(value).isoformat().replace("+00:00", "Z")


def start_of_utc_day(value: datetime) -> datetime:
    value = ensure_utc(value)
    return datetime.combine(value.date(), time.min, tzinfo=UTC)


def utc_day_window(day: date) -> tuple[datetime, datetime]:
    start = datetime.combine(day, time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def local_day_window(moment: datetime, tz_name: str) -> tuple[datetime, datetime]:
    """Return the [start, end) of the local calendar day containing ``moment``, in UTC.

    Used for quota windows that reset at local midnight (e.g. Pacific Time). Handles DST
    transitions by computing each midnight in local time independently.
    """
    tz = ZoneInfo(tz_name)
    local = ensure_utc(moment).astimezone(tz)
    start_local = datetime.combine(local.date(), time.min, tzinfo=tz)
    end_local = datetime.combine(local.date() + timedelta(days=1), time.min, tzinfo=tz)
    return start_local.astimezone(UTC), end_local.astimezone(UTC)


_DURATION = re.compile(
    r"^P(?:(?P<weeks>\d+)W)?(?:(?P<days>\d+)D)?"
    r"(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


def parse_iso8601_duration(value: str | None) -> int | None:
    """Parse an ISO 8601 duration (YouTube ``contentDetails.duration``) into seconds.

    Returns ``None`` for missing or unparseable values (including year/month components,
    whose length is ambiguous), so callers can distinguish "unknown" from zero.
    """
    if not value:
        return None
    match = _DURATION.match(value.strip())
    if not match or value.strip() in {"P", "PT"}:
        return None
    parts = {k: float(v) if v else 0.0 for k, v in match.groupdict().items()}
    total = (
        parts["weeks"] * 7 * 86400
        + parts["days"] * 86400
        + parts["hours"] * 3600
        + parts["minutes"] * 60
        + parts["seconds"]
    )
    return int(total)
