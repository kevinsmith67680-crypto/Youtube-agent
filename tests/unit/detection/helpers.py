"""Builders for detection unit tests (no database, no network, fixed seeds)."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import numpy as np

from tests.unit.monitoring.helpers import DAY, MONDAY, ROOT, point
from trend_agent.detection.config import LoadedDetectionConfig, load_detection_config
from trend_agent.detection.deduplication import EpisodeMeta
from trend_agent.detection.engine import EngineOutput, SeriesEngine
from trend_agent.detection.preprocessing import prepare
from trend_agent.detection.significance import NullLookup
from trend_agent.discovery.models import Category
from trend_agent.schemas.evidence import classify
from trend_agent.schemas.monitoring import MeasurementCategory
from trend_agent.schemas.time_series import StoredPoint

LOADED = load_detection_config(ROOT / "config")
CFG = LOADED.config

__all__ = ["CFG", "DAY", "LOADED", "MONDAY", "meta", "noisy", "point", "run_engine"]


def noisy(
    days: int, *, base: float = 40.0, sd: float = 3.0, seed: int = 1, **kw: Any
) -> list[StoredPoint]:
    rng = np.random.default_rng(seed)
    return [point(i, float(round(base + rng.normal(0, sd))), **kw) for i in range(days)]


def run_engine(
    points: list[StoredPoint],
    loaded: LoadedDetectionConfig = LOADED,
    nulls: NullLookup | None = None,
    **kw: Any,
) -> EngineOutput:
    series = prepare(points, loaded.config, already_known=True)
    return SeriesEngine(loaded, nulls).run(series, None, **kw)


def meta(
    measurement: MeasurementCategory = MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST,
    *,
    target_id: int = 1,
    synthetic: bool = False,
    proxy: bool = False,
) -> EpisodeMeta:
    return EpisodeMeta(
        target_id=target_id,
        query="test query",
        query_id=1,
        keyword_id=None,
        topic_cluster_id=None,
        category=Category.AI,
        region="US",
        language="en",
        provider="p",
        measurement=measurement,
        is_proxy=proxy,
        classification=classify(measurement, is_proxy=proxy),
        is_synthetic=synthetic,
        step=timedelta(days=1),
    )


_ = (DAY, MONDAY)
