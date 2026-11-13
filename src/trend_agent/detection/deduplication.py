"""Episode identity and timing rules used for alert deduplication.

An episode key identifies "the same continuing event": one monitoring target, one
provider, one evidence category, one region/language, and the synthetic flag. Every new
observation of that series family updates the open episode for the key instead of
raising a new alert; per-request scaled series of the same target share the key, so a
surge seen in several normalisation batches is still one episode. Keyword-level episodes
stay distinct even when keywords share a topic cluster (``topic_view`` groups them for
presentation only, adding no evidence).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta

from trend_agent.discovery.models import Category
from trend_agent.schemas.evidence import SurgeClassification
from trend_agent.schemas.monitoring import MeasurementCategory


@dataclass(frozen=True)
class EpisodeMeta:
    """What an episode is about (copied onto the episode and the output contract)."""

    target_id: int
    query: str
    query_id: int
    keyword_id: int | None
    topic_cluster_id: int | None
    category: Category
    region: str
    language: str | None
    provider: str
    measurement: MeasurementCategory  # effective evidence category
    is_proxy: bool
    classification: SurgeClassification
    is_synthetic: bool
    step: timedelta  # one period of the series granularity

    @property
    def key(self) -> str:
        return episode_key(
            self.target_id,
            self.provider,
            self.measurement,
            self.region,
            self.language,
            self.is_synthetic,
        )


def episode_key(
    target_id: int,
    provider: str,
    measurement: MeasurementCategory,
    region: str,
    language: str | None,
    synthetic: bool,
) -> str:
    raw = f"{target_id}|{provider}|{measurement.value}|{region}|{language or ''}|{int(synthetic)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def periods_between(earlier: datetime, later: datetime, step: timedelta) -> float:
    return (later - earlier) / step
