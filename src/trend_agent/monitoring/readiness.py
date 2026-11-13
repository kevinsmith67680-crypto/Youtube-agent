"""Baseline readiness: is there enough comparable history for later statistical analysis?

This module counts and measures history; it computes no surge score and makes no
prediction. Requirements are configurable per provider and temporal resolution
(``readiness.profiles`` in ``monitoring.yaml``): e.g. ``short_term`` (14 daily values)
versus ``weekly_seasonal`` (56 daily values, enough to see weekly patterns repeat).

Only one comparable group (one series: same scale) counts towards readiness. History split
across incompatible normalisation contexts is reported as INCOMPATIBLE_HISTORY instead of
being combined. Missing periods and missing values reduce coverage; they are never
filled or treated as zero.
"""

from __future__ import annotations

import itertools
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from trend_agent.monitoring.config import ReadinessProfile
from trend_agent.monitoring.scheduler import period_step
from trend_agent.schemas.enums import SampleQuality, TimeGranularity
from trend_agent.schemas.monitoring import (
    PROXY_CATEGORIES,
    FreshnessStatus,
    ReadinessState,
)
from trend_agent.schemas.time_series import StoredPoint

USABLE_QUALITIES = frozenset({SampleQuality.COMPLETE, SampleQuality.LOW_VOLUME})
STALE_FRESHNESS = frozenset({FreshnessStatus.PROVIDER_DELAYED, FreshnessStatus.COLLECTION_DELAYED})


@dataclass
class SeriesReadiness:
    series_id: int
    provider: str
    category: str
    is_proxy: bool
    is_synthetic: bool
    granularity: str
    normalization_key: str
    profile: str
    state: ReadinessState
    reasons: list[str] = field(default_factory=list)
    comparable_observations: int = 0
    span_days: float = 0.0
    expected_periods: int = 0
    coverage: float = 0.0
    missing_periods: int = 0
    missing_values: int = 0
    partial_periods: int = 0
    gap_distribution: dict[str, int] = field(default_factory=dict)
    first_period: datetime | None = None
    last_period_end: datetime | None = None
    staleness_periods: int | None = None
    freshness: str | None = None
    usable_for_search_demand: bool = False

    def as_dict(self) -> dict[str, Any]:
        data = dict(self.__dict__)
        data["state"] = self.state.value
        for key in ("first_period", "last_period_end"):
            if data[key] is not None:
                data[key] = data[key].isoformat()
        return data


def _gaps(starts: Sequence[datetime], step: timedelta) -> tuple[int, dict[str, int]]:
    """(missing periods, distribution of gap lengths in periods)."""
    missing = 0
    dist: Counter[str] = Counter()
    for a, b in itertools.pairwise(starts):
        gap = round((b - a) / step) - 1
        if gap > 0:
            missing += gap
            dist[str(gap) if gap < 7 else "7+"] += 1
    return missing, dict(sorted(dist.items()))


def evaluate_series(
    points: Sequence[StoredPoint],
    *,
    series_id: int,
    profile_name: str,
    profile: ReadinessProfile,
    now: datetime,
    freshness: FreshnessStatus | None,
    provider_available: bool,
) -> SeriesReadiness:
    current = sorted((p for p in points if p.is_current), key=lambda p: p.period_start)
    head = current[0] if current else (points[0] if points else None)
    out = SeriesReadiness(
        series_id=series_id,
        provider=head.provider_name if head else "?",
        category=head.effective_category.value if head else "?",
        is_proxy=bool(head and (head.is_proxy or head.effective_category in PROXY_CATEGORIES)),
        is_synthetic=bool(head and head.is_synthetic),
        granularity=head.granularity.value if head else "?",
        normalization_key=head.normalization_key[:12] if head else "",
        profile=profile_name,
        state=ReadinessState.INSUFFICIENT_HISTORY,
        freshness=freshness.value if freshness else None,
    )
    out.usable_for_search_demand = not out.is_proxy
    if not current:
        out.state = (
            ReadinessState.SOURCE_UNAVAILABLE
            if not provider_available
            else ReadinessState.INSUFFICIENT_HISTORY
        )
        out.reasons.append("no observations stored")
        return out
    usable = [p for p in current if p.value is not None and p.sample_quality in USABLE_QUALITIES]
    out.comparable_observations = len(usable)
    out.missing_values = sum(1 for p in current if p.value is None)
    out.partial_periods = sum(
        1 for p in current if p.sample_quality is SampleQuality.PARTIAL_PERIOD
    )
    out.first_period = current[0].period_start
    out.last_period_end = current[-1].period_end
    span = out.last_period_end - out.first_period
    out.span_days = round(span.total_seconds() / 86400.0, 2)
    step = period_step(current[0].granularity)
    if step is not None:
        out.expected_periods = max(1, round(span / step))
        out.missing_periods, out.gap_distribution = _gaps([p.period_start for p in current], step)
        out.coverage = round(len(usable) / out.expected_periods, 4)
        out.staleness_periods = max(0, int((now - out.last_period_end) / step))
    else:
        out.expected_periods = len(current)
        out.coverage = round(len(usable) / max(1, len(current)), 4)

    if out.is_proxy:
        out.reasons.append("proxy series (event/community/suggestion signals): not search interest")
    if current[0].granularity is not profile.granularity:
        out.state = ReadinessState.INCOMPATIBLE_HISTORY
        out.reasons.append(
            f"granularity {current[0].granularity.value} does not match the "
            f"{profile_name} requirement ({profile.granularity.value})"
        )
        return out
    if not provider_available and freshness in (None, FreshnessStatus.SOURCE_UNAVAILABLE):
        out.reasons.append("provider currently unavailable; history kept")
    unmet: list[str] = []
    if len(usable) < profile.min_observations:
        unmet.append(f"{len(usable)} usable observations < {profile.min_observations} required")
    if span < profile.min_span:
        unmet.append(f"span {out.span_days} days < {profile.min_span.days} required")
    if out.coverage < profile.min_coverage:
        unmet.append(
            f"coverage {out.coverage:.0%} < {profile.min_coverage:.0%} "
            f"({out.missing_periods} missing period(s), {out.missing_values} missing value(s))"
        )
    stale = (
        out.staleness_periods is not None
        and out.staleness_periods > profile.max_staleness_periods
        and freshness in STALE_FRESHNESS | {FreshnessStatus.SOURCE_UNAVAILABLE, None}
    )
    if len(usable) < profile.partial_min_observations:
        out.state = ReadinessState.INSUFFICIENT_HISTORY
    elif stale:
        out.state = ReadinessState.STALE_HISTORY
        unmet.append(
            f"latest period ended {out.staleness_periods} period(s) ago "
            f"(> {profile.max_staleness_periods}); freshness {out.freshness}"
        )
    elif unmet:
        out.state = ReadinessState.PARTIALLY_READY
    else:
        out.state = ReadinessState.READY
    out.reasons.extend(unmet)
    if out.state is ReadinessState.READY:
        out.reasons.append(
            f"meets {profile_name}: {len(usable)} observations over {out.span_days} days, "
            f"coverage {out.coverage:.0%}"
        )
    return out


@dataclass
class TargetReadiness:
    target_id: int
    query: str
    provider: str | None
    state: ReadinessState
    reasons: list[str]
    best_series: SeriesReadiness | None
    series: list[SeriesReadiness]

    def as_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "query": self.query,
            "provider": self.provider,
            "state": self.state.value,
            "reasons": self.reasons,
            "best_series": self.best_series.as_dict() if self.best_series else None,
            "series": [s.as_dict() for s in self.series],
        }


_RANK = [
    ReadinessState.READY,
    ReadinessState.PARTIALLY_READY,
    ReadinessState.STALE_HISTORY,
    ReadinessState.INCOMPATIBLE_HISTORY,
    ReadinessState.INSUFFICIENT_HISTORY,
    ReadinessState.SOURCE_UNAVAILABLE,
]


def combine_target(
    target_id: int,
    query: str,
    provider: str | None,
    series: list[SeriesReadiness],
    profile: ReadinessProfile,
    provider_available: bool,
) -> TargetReadiness:
    if not series:
        state = (
            ReadinessState.SOURCE_UNAVAILABLE
            if not provider_available
            else ReadinessState.INSUFFICIENT_HISTORY
        )
        reason = "provider unavailable; no history" if not provider_available else "no history yet"
        return TargetReadiness(target_id, query, provider, state, [reason], None, [])
    ranked = sorted(series, key=lambda s: (_RANK.index(s.state), -s.comparable_observations))
    best = ranked[0]
    reasons = list(best.reasons)
    state = best.state
    total = sum(s.comparable_observations for s in series if not s.is_proxy)
    search = [s for s in series if not s.is_proxy]
    if (
        state in (ReadinessState.INSUFFICIENT_HISTORY, ReadinessState.PARTIALLY_READY)
        and len(search) > 1
        and total >= profile.min_observations
    ):
        state = ReadinessState.INCOMPATIBLE_HISTORY
        reasons.insert(
            0,
            f"history split across {len(search)} incompatible series (normalisation contexts "
            f"or providers); largest comparable group has {best.comparable_observations} of "
            f"{total} observations - they are not combined",
        )
    return TargetReadiness(target_id, query, provider, state, reasons, best, series)


def readiness_granularity(profile: ReadinessProfile) -> TimeGranularity:
    return profile.granularity
