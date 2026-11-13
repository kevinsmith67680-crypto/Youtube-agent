from __future__ import annotations

import json
import logging
import math
from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from tests.helpers import make_settings
from trend_agent.config import redact_url_password
from trend_agent.logging import JsonFormatter, json_safe, redact, redact_text, storable
from trend_agent.utils.codes import (
    CodeValidationError,
    is_valid_language,
    normalize_language,
    normalize_region,
)
from trend_agent.utils.text import QueryValidationError, canonicalize_query, clean_original_query
from trend_agent.utils.time import (
    NaiveDatetimeError,
    ensure_utc,
    format_rfc3339,
    local_day_window,
    parse_iso8601_duration,
    parse_rfc3339,
)

# ---------------------------------------------------------------------------- codes


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("gb", "GB"), (" us ", "US"), ("world", "WORLD"), ("gb-eng", "GB-ENG"), ("US-CA", "US-CA")],
)
def test_normalize_region_valid(raw: str, expected: str) -> None:
    assert normalize_region(raw) == expected


@pytest.mark.parametrize("raw", ["ZZ", "", "GBR", "XX-ENG", "12"])
def test_normalize_region_invalid(raw: str) -> None:
    with pytest.raises(CodeValidationError):
        normalize_region(raw)


def test_region_hint_for_uk() -> None:
    with pytest.raises(CodeValidationError, match="did you mean 'GB'"):
        normalize_region("UK")


def test_region_world_and_subdivision_can_be_disallowed() -> None:
    with pytest.raises(CodeValidationError):
        normalize_region("WORLD", allow_world=False)
    with pytest.raises(CodeValidationError):
        normalize_region("GB-ENG", allow_subdivision=False)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("en", "en"), ("EN", "en"), ("pt_br", "pt-BR"), ("zh-hans", "zh-Hans"), ("es-419", "es-419")],
)
def test_normalize_language_valid(raw: str, expected: str) -> None:
    assert normalize_language(raw) == expected


@pytest.mark.parametrize("raw", ["english", "xx", "en-ZZ", "", "e"])
def test_normalize_language_invalid(raw: str) -> None:
    assert not is_valid_language(raw)


# ---------------------------------------------------------------------------- text


def test_canonicalize_query_is_conservative() -> None:
    assert canonicalize_query("  AI​   Agents\t") == "ai agents"
    assert canonicalize_query("\uff21\uff29 agents") == "ai agents"  # NFKC full-width
    assert canonicalize_query("C++ tutorial") == "c++ tutorial"  # punctuation kept
    assert canonicalize_query("Straße") == "strasse"  # casefold
    assert clean_original_query("  AI   Agents ") == "AI Agents"


@pytest.mark.parametrize("raw", ["", "   ", "​", "x" * 201, "bad\x00query"])
def test_canonicalize_query_rejects(raw: str) -> None:
    with pytest.raises(QueryValidationError):
        canonicalize_query(raw)


# ---------------------------------------------------------------------------- time


def test_ensure_utc_rejects_naive_and_converts_aware() -> None:
    with pytest.raises(NaiveDatetimeError):
        ensure_utc(datetime(2026, 1, 1))
    plus_two = timezone(timedelta(hours=2))
    assert ensure_utc(datetime(2026, 1, 1, 2, tzinfo=plus_two)) == datetime(2026, 1, 1, tzinfo=UTC)


def test_rfc3339_round_trip() -> None:
    parsed = parse_rfc3339("2026-02-01T10:00:00.123Z")
    assert parsed.tzinfo is UTC
    assert format_rfc3339(datetime(2026, 2, 1, 10, tzinfo=UTC)) == "2026-02-01T10:00:00Z"


@pytest.mark.parametrize(
    ("raw", "seconds"),
    [
        ("PT15M33S", 933),
        ("PT1H2M3S", 3723),
        ("P0D", 0),
        ("P1DT1S", 86401),
        ("PT4M", 240),
        ("P1W", 604800),
        (None, None),
        ("", None),
        ("PT", None),
        ("P1Y", None),  # ambiguous length -> unknown, never guessed
        ("garbage", None),
    ],
)
def test_parse_duration(raw: str | None, seconds: int | None) -> None:
    assert parse_iso8601_duration(raw) == seconds


def test_pacific_day_window_handles_dst() -> None:
    # Winter: midnight PST = 08:00 UTC
    start, end = local_day_window(datetime(2026, 1, 15, 12, tzinfo=UTC), "America/Los_Angeles")
    assert start == datetime(2026, 1, 15, 8, tzinfo=UTC)
    assert end == datetime(2026, 1, 16, 8, tzinfo=UTC)
    # Summer: midnight PDT = 07:00 UTC
    start, _ = local_day_window(datetime(2026, 7, 15, 12, tzinfo=UTC), "America/Los_Angeles")
    assert start == datetime(2026, 7, 15, 7, tzinfo=UTC)
    # DST start day (2026-03-08) is 23 hours long
    start, end = local_day_window(datetime(2026, 3, 8, 12, tzinfo=UTC), "America/Los_Angeles")
    assert end - start == timedelta(hours=23)
    # 05:00 UTC on Jan 16 is still Jan 15 in Pacific time
    start, _ = local_day_window(datetime(2026, 1, 16, 5, tzinfo=UTC), "America/Los_Angeles")
    assert start == datetime(2026, 1, 15, 8, tzinfo=UTC)


# ---------------------------------------------------------------------------- config


def test_settings_defaults_are_conservative() -> None:
    s = make_settings()
    assert s.youtube_api_key is None
    assert s.youtube_persist_statistics is False
    assert s.google_trends_retention_days is None
    assert s.youtube_metadata_retention_days == 30


def test_settings_reject_invalid_values() -> None:
    with pytest.raises(ValidationError):
        make_settings(youtube_metadata_retention_days=31)  # beyond the 30-day policy
    with pytest.raises(ValidationError):
        make_settings(youtube_api_base_url="http://insecure.example")
    with pytest.raises(ValidationError):
        make_settings(log_level="LOUD")
    with pytest.raises(ValidationError):
        make_settings(http_backoff_base_seconds=10, http_backoff_max_seconds=1)


def test_blank_api_key_is_treated_as_missing() -> None:
    assert make_settings(youtube_api_key="   ").youtube_api_key is None


def test_describe_never_contains_secrets() -> None:
    s = make_settings(
        youtube_api_key="AIzaSECRETSECRETSECRETSECRET123",
        database_url="postgresql+psycopg://user:hunter2@db:5432/x",
    )
    text = json.dumps(s.describe())
    assert "SECRET" not in text
    assert "hunter2" not in text
    assert s.describe()["youtube_api_key"] == "set"
    assert "SECRET" not in repr(s)


def test_redact_url_password() -> None:
    assert redact_url_password("postgresql://u:pw@h:5432/d") == "postgresql://u:***@h:5432/d"
    assert redact_url_password("postgresql://h/d") == "postgresql://h/d"


# ---------------------------------------------------------------------------- logging


def test_redact_text_patterns() -> None:
    text = (
        "GET https://x/y?key=AIzaSyA1234567890abcdefghijklmn&part=id "
        "Authorization: Bearer abc.def.ghi postgresql+psycopg://u:pw@host/db"
    )
    out = redact_text(text)
    assert "AIzaSy" not in out
    assert "abc.def.ghi" not in out
    assert ":pw@" not in out
    assert "part=id" in out


def test_redact_nested_keys() -> None:
    data = {"api_key": "x", "nested": {"Authorization": "y", "ok": 1}, "items": [{"token": "z"}]}
    out = redact(data)
    assert out["api_key"] == out["nested"]["Authorization"] == out["items"][0]["token"]
    assert out["nested"]["ok"] == 1


def test_json_safe_handles_non_finite() -> None:
    assert json_safe({"v": math.nan, "w": [math.inf, 1.5]}) == {"v": "nan", "w": ["inf", 1.5]}
    json.dumps(storable({"value": math.nan, "key": "secret"}), allow_nan=False)


def test_json_formatter_emits_redacted_json() -> None:
    record = logging.makeLogRecord(
        {"name": "trend_agent.x", "levelname": "INFO", "msg": "call key=AIzaSECRETSECRETSECRET1234"}
    )
    record.api_key = "AIzaSECRET"
    payload = json.loads(JsonFormatter().format(record))
    assert payload["event"].endswith("***REDACTED***")
    assert payload["api_key"] == "***REDACTED***"
    assert payload["level"] == "INFO"
