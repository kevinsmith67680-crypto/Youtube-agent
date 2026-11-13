"""Shared builders for monitoring unit tests (no database, no network)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from trend_agent.monitoring.config import load_monitoring_config
from trend_agent.schemas.enums import SampleQuality, ScaleType, TimeGranularity
from trend_agent.schemas.monitoring import MeasurementCategory, QualityStatus
from trend_agent.schemas.time_series import StoredPoint

ROOT = Path(__file__).resolve().parents[3]
LOADED = load_monitoring_config(ROOT / "config")
CONFIG = LOADED.config
MONDAY = datetime(2026, 3, 2, tzinfo=UTC)
DAY = timedelta(days=1)
KEY_A = "a" * 64
KEY_B = "b" * 64

_ids = iter(range(1, 10_000_000))


def point(day: int = 0, value: float | None = 50.0, **kw: Any) -> StoredPoint:
    start = MONDAY + DAY * day
    base: dict[str, Any] = {
        "id": next(_ids),
        "series_id": 1,
        "target_id": 1,
        "provider_name": "p",
        "measurement_category": MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST,
        "simulated_category": None,
        "period_start": start,
        "period_end": start + DAY,
        "granularity": TimeGranularity.DAY,
        "value": value,
        "sample_quality": SampleQuality.COMPLETE if value is not None else SampleQuality.MISSING,
        "scale_type": ScaleType.CONSISTENT_INDEX,
        "normalization_key": KEY_A,
        "unit": "index",
        "region": "US",
        "language": "en",
        "provider_version": "1",
        "provider_published_at": start + DAY + timedelta(hours=36),
        "first_available_at": None,
        "collected_at": start + DAY * 3,
        "ingested_at": start + DAY * 3,
        "revision": 1,
        "revision_kind": "initial",
        "is_current": True,
        "supersedes_id": None,
        "quality_status": QualityStatus.VALID,
        "is_synthetic": False,
        "is_proxy": False,
        "source_reference": "ref",
        "data_policy_id": "x@1",
    }
    base.update(kw)
    return StoredPoint(**base)


def series(days: int, **kw: Any) -> list[StoredPoint]:
    return [point(i, 40.0 + i % 5, **kw) for i in range(days)]
