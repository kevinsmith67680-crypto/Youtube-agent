"""Point-in-time data access for forecasting (the leakage boundary).

``PointInTimeSeries`` answers one question: *what did this series look like at time T?*
It uses the Phase 3 availability rule (provider publication time, else the later of
collected/ingested time — ``monitoring.replay.availability``) and the Phase 4 revision
rule (newest revision available at T — ``detection.preprocessing.knowledge_at``). Future
observations, later revisions and late arrivals are invisible until they were available.

``DetectorFeed`` replays the Phase 4 ``SeriesEngine`` causally over the same views, so
detector features (anomaly z, CUSUM, change-point evidence, elevated streak) are exactly
what the detector could have computed at T. When a series has no revisions and its
observations became available in period order, one causal pass over the series yields
the same evaluations (Phase 4: "incremental = recomputed"); that fast path is used and is
checked against the strict path in the tests.
"""

from __future__ import annotations

import bisect
import itertools
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np

from trend_agent.detection.config import LoadedDetectionConfig
from trend_agent.detection.engine import PointEvaluation, SeriesEngine
from trend_agent.detection.preprocessing import knowledge_at, prepare
from trend_agent.monitoring.replay import AvailabilityMode, availability
from trend_agent.schemas.enums import ScaleType, TimeGranularity
from trend_agent.schemas.monitoring import MeasurementCategory
from trend_agent.schemas.time_series import StoredPoint

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def hours(moment: datetime) -> float:
    return (moment - EPOCH).total_seconds() / 3600.0


def from_hours(h: float) -> datetime:
    from datetime import timedelta

    return EPOCH + timedelta(hours=h)


@dataclass(frozen=True)
class SeriesMeta:
    series_id: int
    query: str
    provider: str
    measurement: MeasurementCategory  # effective category (what synthetic data simulates)
    granularity: TimeGranularity
    scale_type: ScaleType
    unit: str
    region: str
    is_proxy: bool
    is_synthetic: bool
    category: str | None = None
    target_id: int | None = None
    query_id: int | None = None
    entity_id: int | None = None
    topic_cluster_id: int | None = None
    group_key: str = ""  # topic family / episode group for unseen-group evaluation
    language: str | None = None
    keyword_first_seen: datetime | None = None
    entity_first_seen: datetime | None = None


@dataclass(frozen=True)
class ContentEvent:
    """A Phase 5 analysis, usable only from its completion time."""

    completed_at: datetime
    gap_type: str | None
    strength: str | None
    is_opportunity: bool


@dataclass
class SeriesBundle:
    meta: SeriesMeta
    points: list[StoredPoint]  # every stored revision of the target series
    supporting: dict[MeasurementCategory, list[StoredPoint]] = field(default_factory=dict)
    content: list[ContentEvent] = field(default_factory=list)


@dataclass(frozen=True)
class Visible:
    """The target series as known at ``as_of`` (period order)."""

    as_of: datetime
    t_end: np.ndarray  # period end, hours since epoch
    values: np.ndarray  # NaN = missing value
    revisions: np.ndarray  # revision number of the visible value
    normalization_keys: tuple[str, ...]
    complete: np.ndarray  # sample quality complete
    withheld: int  # periods already over but not yet available
    period_keys: tuple[str, ...] = ()  # normalization key of each visible period

    @property
    def n(self) -> int:
        return int(self.t_end.size)

    def finite(self) -> np.ndarray:
        return np.isfinite(self.values)


def _arrays(points: Sequence[StoredPoint]) -> tuple[np.ndarray, ...]:
    t = np.array([hours(p.period_end) for p in points], dtype=float)
    v = np.array([np.nan if p.value is None else float(p.value) for p in points], dtype=float)
    r = np.array([p.revision for p in points], dtype=float)
    c = np.array([p.sample_quality.value == "complete" for p in points], dtype=bool)
    return t, v, r, c


class PointInTimeSeries:
    def __init__(
        self, points: Iterable[StoredPoint], mode: AvailabilityMode = AvailabilityMode.PUBLICATION
    ) -> None:
        self.mode = mode
        self.points = [p for p in points if p.revision_kind != "conflict"]
        self.available = {p.id: availability(p, mode)[0] for p in self.points}
        by_period = sorted(self.points, key=lambda p: (p.period_start, p.revision, p.id))
        firsts = [p for p in by_period if p.revision == 1]
        self.revision_free = len(firsts) == len(by_period) and len(
            {p.period_start for p in by_period}
        ) == len(by_period)
        avail = [self.available[p.id] for p in by_period]
        self.monotone = all(a <= b for a, b in itertools.pairwise(avail))
        self.fast = self.revision_free and self.monotone
        self._sorted = by_period
        self._avail_h = np.array([hours(a) for a in avail], dtype=float)
        if self.fast:
            self._t, self._v, self._r, self._c = _arrays(by_period)
            self._keys = [p.normalization_key for p in by_period]

    def latest_available(self, as_of_h: float) -> float | None:
        """Availability time (hours) of the newest observation available at ``as_of_h``."""
        if not hasattr(self, "_avail_sorted"):
            self._avail_sorted = np.sort(self._avail_h)
        k = int(np.searchsorted(self._avail_sorted, as_of_h + 1e-9, side="right"))
        return float(self._avail_sorted[k - 1]) if k else None

    def at(self, as_of: datetime) -> Visible:
        if self.fast:
            k = int(np.searchsorted(self._avail_h, hours(as_of), side="right"))
            withheld = int(np.sum(self._t[k:] <= hours(as_of)))
            keys = tuple(self._keys[:k])
            return Visible(
                as_of,
                self._t[:k],
                self._v[:k],
                self._r[:k],
                tuple(dict.fromkeys(keys)),
                self._c[:k],
                withheld,
                keys,
            )
        known = knowledge_at(self.points, as_of, self.mode)
        t, v, r, c = _arrays(known)
        withheld = sum(
            1
            for p in self.points
            if self.available[p.id] > as_of
            and hours(p.period_end) <= hours(as_of)
            and p.revision == 1
        )
        keys = tuple(p.normalization_key for p in known)
        return Visible(as_of, t, v, r, tuple(dict.fromkeys(keys)), c, withheld, keys)

    def last_point(self, as_of: datetime) -> StoredPoint | None:
        """The newest visible observation with a value at ``as_of``."""
        if self.fast:
            k = int(np.searchsorted(self._avail_h, hours(as_of), side="right"))
            for p in reversed(self._sorted[:k]):
                if p.value is not None:
                    return p
            return None
        known = [p for p in knowledge_at(self.points, as_of, self.mode) if p.value is not None]
        return known[-1] if known else None

    def final(self) -> tuple[np.ndarray, np.ndarray]:
        """Latest known values per period (labels use the final revision)."""
        known = knowledge_at(self.points, None, self.mode)
        t, v, _, _ = _arrays(known)
        return t, v

    def first_available(self) -> datetime | None:
        return min(self.available.values(), default=None)


class DetectorFeed:
    """Phase 4 detector state as of each prediction time (causal)."""

    def __init__(
        self,
        points: Sequence[StoredPoint],
        loaded: LoadedDetectionConfig,
        category: str | None,
        *,
        pit: PointInTimeSeries | None = None,
        mode: AvailabilityMode = AvailabilityMode.PUBLICATION,
    ) -> None:
        self.points = list(points)
        self.loaded = loaded
        self.engine = SeriesEngine(loaded)
        self.category = category
        self.mode = mode
        self.pit = pit or PointInTimeSeries(points, mode)
        self._evals: dict[float, PointEvaluation] = {}  # period end (h) -> evaluation
        self._ends: list[float] = []
        self.status: str = "unknown"
        if self.pit.fast:
            series = prepare(self.points, loaded.config, as_of=None, mode=mode)
            self.status = series.status.value
            out = self.engine.run(series, None, category=category)
            self._store(out.evaluations)

    def _store(self, evaluations: Sequence[PointEvaluation]) -> None:
        for ev in evaluations:
            key = hours(ev.period_end)
            if key not in self._evals:
                bisect.insort(self._ends, key)
            self._evals[key] = ev

    def history(self, as_of: datetime) -> list[PointEvaluation]:
        """Evaluations of the periods visible at ``as_of``.

        Fast path (no revisions, availability in period order): the single causal pass,
        cut at the last visible period. Strict path: a fresh engine run on exactly what
        was known at ``as_of`` (``strict_history``) - slower, used only when a series has
        revisions or out-of-order publication."""
        if not self.pit.fast:
            series = prepare(self.points, self.loaded.config, as_of=as_of, mode=self.mode)
            self.status = series.status.value
            return self.engine.run(series, None, category=self.category).evaluations
        vis = self.pit.at(as_of)
        if vis.n == 0:
            return []
        last = float(vis.t_end[-1])
        k = bisect.bisect_right(self._ends, last + 1e-9)
        return [self._evals[e] for e in self._ends[:k]]


def strict_history(
    points: Sequence[StoredPoint],
    loaded: LoadedDetectionConfig,
    as_of: datetime,
    category: str | None,
    mode: AvailabilityMode = AvailabilityMode.PUBLICATION,
) -> list[PointEvaluation]:
    """Reference implementation: a fresh full run on exactly what was known at ``as_of``."""
    series = prepare(points, loaded.config, as_of=as_of, mode=mode)
    return SeriesEngine(loaded).run(series, None, category=category).evaluations
