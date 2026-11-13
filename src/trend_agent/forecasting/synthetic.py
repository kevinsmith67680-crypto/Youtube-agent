"""Deterministic SYNTHETIC series for forecasting tests, scenarios and the experimental corpus.

Everything here is artificial and labelled synthetic (``is_synthetic = True``, measurement
``SYNTHETIC_TEST_OBSERVATION`` with the simulated category recorded separately). Models
trained on it are ``data_origin = synthetic`` and can never serve real series.

    y_t = clip(round(base * season_t + effect_t + noise_t), 0, 100)

Hourly series have a diurnal and weekly pattern, daily series a weekly one. Event shapes
(index points added to the base): ``launch`` (accelerating rise, slow decay), ``adoption``
(logistic growth that persists), ``limited`` (rise, plateau, sharp end at a known time),
``spike``, ``step``, ``drop``. Labels are *not* taken from these parameters: the target
definitions compute them from the generated values, exactly as for real data.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from trend_agent.forecasting.feature_store import SeriesBundle, SeriesMeta
from trend_agent.schemas.enums import SampleQuality, ScaleType, TimeGranularity
from trend_agent.schemas.monitoring import MeasurementCategory, QualityStatus
from trend_agent.schemas.time_series import StoredPoint

START = datetime(2026, 9, 7, tzinfo=UTC)  # a Monday
PROVIDER = "synthetic_forecast_trends"
M = MeasurementCategory


@dataclass(frozen=True)
class Event:
    kind: str  # launch | adoption | limited | spike | step | drop
    onset_h: float
    gain: float  # peak effect as a multiple of the base level
    rise_h: float = 24.0
    length_h: float = 72.0  # plateau end (limited), duration (spike/adoption)
    decay_h: float = 48.0

    def effect(self, d: float) -> float:
        """Effect (multiple of base) ``d`` hours after onset."""
        if d < 0:
            return 0.0
        g = self.gain
        if self.kind == "launch":
            if d <= self.rise_h:
                k = 4.0 / self.rise_h
                return g * (math.exp(k * d) - 1.0) / (math.exp(k * self.rise_h) - 1.0)
            return g * math.exp(-(d - self.rise_h) / self.decay_h)
        if self.kind == "adoption":
            mid, width = self.length_h / 2.0, max(self.length_h / 8.0, 1.0)
            return g / (1.0 + math.exp(-(d - mid) / width))
        if self.kind == "limited":
            if d < self.rise_h:
                return g * d / self.rise_h
            if d < self.length_h:
                return g
            return g * math.exp(-(d - self.length_h) / 6.0)
        if self.kind == "spike":
            return g if d < self.length_h else 0.0
        if self.kind in ("step", "drop"):
            return g
        raise ValueError(f"unknown event kind {self.kind}")


@dataclass(frozen=True)
class Spec:
    key: str
    seed: int
    granularity: TimeGranularity = TimeGranularity.HOUR
    periods: int = 24 * 42
    base: float = 25.0
    noise: float = 0.08
    ar: float = 0.4
    diurnal: float = 0.25
    weekly: float = 0.1
    events: tuple[Event, ...] = ()
    missing_rate: float = 0.0
    measurement: MeasurementCategory = M.DIRECT_YOUTUBE_SEARCH_INTEREST
    scale: ScaleType = ScaleType.CONSISTENT_INDEX
    lag_h: float | None = None  # publication lag (default: 2 h hourly, 36 h daily)
    category: str = "ai"
    group: str = ""
    start: datetime = START
    provider: str = PROVIDER
    revise_every: int = 0  # every n-th period is revised later (+revision_delta)
    revision_delta: float = 3.0
    revision_delay_h: float = 48.0
    normalization_change_at: int | None = None  # period index where the scale is re-based
    delayed: tuple[tuple[int, float], ...] = ()  # (period index, extra publication delay h)
    query: str = ""

    @property
    def step_h(self) -> float:
        return 1.0 if self.granularity is TimeGranularity.HOUR else 24.0


def _rng(seed: int, key: str) -> np.random.Generator:
    digest = hashlib.sha256(f"{seed}|{key}".encode()).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "big"))


def values(spec: Spec) -> list[float | None]:
    rng = _rng(spec.seed, spec.key)
    n, step = spec.periods, spec.step_h
    eps = rng.normal(0.0, 1.0, n)
    ar = np.empty(n)
    ar[0] = eps[0]
    scale = math.sqrt(max(1e-9, 1 - spec.ar**2))
    for i in range(1, n):
        ar[i] = spec.ar * ar[i - 1] + scale * eps[i]
    out: list[float | None] = []
    sd = max(1.0, spec.noise * spec.base)
    for i in range(n):
        t = spec.start + timedelta(hours=step * i)
        season = 1.0 + (spec.weekly if t.weekday() >= 5 else 0.0)
        if spec.granularity is TimeGranularity.HOUR:
            season *= 1.0 + spec.diurnal * math.sin(2 * math.pi * (t.hour - 9) / 24.0)
        th = step * i
        effect = sum(ev.effect(th - ev.onset_h) for ev in spec.events)
        level = spec.base * (season + effect)
        y = level + sd * ar[i]
        out.append(float(min(100.0, max(0.0, round(y)))))
    missing = rng.random(n) < spec.missing_rate
    return [None if missing[i] else out[i] for i in range(n)]


def points(
    spec: Spec, *, series_id: int, first_id: int, target_id: int | None = None
) -> list[StoredPoint]:
    vals = values(spec)
    step = timedelta(hours=spec.step_h)
    lag = timedelta(hours=spec.lag_h if spec.lag_h is not None else (2 if spec.step_h == 1 else 36))
    base_key = hashlib.sha256(f"norm|{spec.key}".encode()).hexdigest()
    alt_key = hashlib.sha256(f"norm2|{spec.key}".encode()).hexdigest()
    delays = dict(spec.delayed)
    out: list[StoredPoint] = []
    pid = first_id
    for i, v in enumerate(vals):
        start = spec.start + step * i
        end = start + step
        published = end + lag + timedelta(hours=delays.get(i, 0.0))
        key = (
            alt_key
            if spec.normalization_change_at is not None and i >= spec.normalization_change_at
            else base_key
        )
        common: dict[str, Any] = {
            "series_id": series_id,
            "target_id": target_id,
            "provider_name": spec.provider,
            "measurement_category": M.SYNTHETIC_TEST_OBSERVATION,
            "simulated_category": spec.measurement,
            "period_start": start,
            "period_end": end,
            "granularity": spec.granularity,
            "scale_type": spec.scale,
            "normalization_key": key,
            "unit": "count" if spec.scale is ScaleType.ABSOLUTE_COUNT else "index",
            "region": "US",
            "language": "en",
            "provider_version": "synthetic-forecast-1",
            "quality_status": QualityStatus.VALID,
            "is_synthetic": True,
            "is_proxy": spec.measurement
            not in (M.DIRECT_YOUTUBE_SEARCH_INTEREST, M.EXTERNAL_SEARCH_INTEREST),
            "source_reference": f"synthetic://forecast/{spec.key}/{start.isoformat()}",
            "data_policy_id": "synthetic-test-data@1",
        }
        out.append(
            StoredPoint(
                id=pid,
                value=v,
                sample_quality=SampleQuality.COMPLETE if v is not None else SampleQuality.MISSING,
                provider_published_at=published,
                first_available_at=published,
                collected_at=published,
                ingested_at=published,
                revision=1,
                revision_kind="initial",
                is_current=not (spec.revise_every and i % spec.revise_every == 0 and v is not None),
                supersedes_id=None,
                **common,
            )
        )
        pid += 1
        if spec.revise_every and i % spec.revise_every == 0 and v is not None:
            revised_at = published + timedelta(hours=spec.revision_delay_h)
            out.append(
                StoredPoint(
                    id=pid,
                    value=float(min(100.0, v + spec.revision_delta)),
                    sample_quality=SampleQuality.COMPLETE,
                    provider_published_at=revised_at,
                    first_available_at=published,
                    collected_at=revised_at,
                    ingested_at=revised_at,
                    revision=2,
                    revision_kind="revision",
                    is_current=True,
                    supersedes_id=pid - 1,
                    **common,
                )
            )
            pid += 1
    return out


@dataclass
class Bundle:
    """A target series and its supporting series (all synthetic)."""

    spec: Spec
    support: dict[MeasurementCategory, Spec] = field(default_factory=dict)

    def build(self, series_id: int, first_id: int) -> tuple[SeriesBundle, int]:
        pts = points(self.spec, series_id=series_id, first_id=first_id)
        nxt = first_id + len(pts)
        supporting: dict[MeasurementCategory, list[StoredPoint]] = {}
        for k, (cat, sspec) in enumerate(self.support.items()):
            sp = points(sspec, series_id=series_id * 10 + k + 1, first_id=nxt)
            nxt += len(sp)
            supporting[cat] = sp
        meta = SeriesMeta(
            series_id=series_id,
            query=self.spec.query or self.spec.key.replace("_", " "),
            provider=self.spec.provider,
            measurement=self.spec.measurement,
            granularity=self.spec.granularity,
            scale_type=self.spec.scale,
            unit="index",
            region="US",
            is_proxy=self.spec.measurement
            not in (M.DIRECT_YOUTUBE_SEARCH_INTEREST, M.EXTERNAL_SEARCH_INTEREST),
            is_synthetic=True,
            category=self.spec.category,
            group_key=self.spec.group or self.spec.key,
            language="en",
        )
        return SeriesBundle(meta, pts, supporting), nxt


_CATEGORY_EVENTS = {
    "ai": (("launch", 0.35), ("adoption", 0.25), ("spike", 0.1), ("none", 0.3)),
    "technology": (("adoption", 0.25), ("step", 0.15), ("launch", 0.15), ("none", 0.45)),
    "gaming": (("limited", 0.4), ("spike", 0.15), ("launch", 0.1), ("none", 0.35)),
}


def _pick(rng: np.random.Generator, options: Sequence[tuple[str, float]]) -> str:
    names = [o[0] for o in options]
    probs = np.array([o[1] for o in options], dtype=float)
    return str(rng.choice(names, p=probs / probs.sum()))


def _random_event(rng: np.random.Generator, kind: str, span_h: float, step: float) -> Event | None:
    if kind == "none":
        return None
    onset = float(rng.uniform(0.1, 0.97) * span_h)  # events throughout the period (stationary)
    onset = round(onset / step) * step
    if kind == "launch":
        return Event(
            "launch",
            onset,
            float(rng.uniform(0.6, 3.0)),
            rise_h=float(rng.choice([12, 24, 36, 48])) * (step if step > 1 else 1),
            decay_h=float(rng.uniform(24, 120)) * (1 if step == 1 else 3),
        )
    if kind == "adoption":
        return Event(
            "adoption",
            onset,
            float(rng.uniform(0.5, 1.5)),
            length_h=float(rng.uniform(72, 240)) * (1 if step == 1 else 3),
        )
    if kind == "limited":
        return Event(
            "limited",
            onset,
            float(rng.uniform(0.8, 2.5)),
            rise_h=float(rng.choice([6, 12, 24])) * (1 if step == 1 else 4),
            length_h=float(rng.uniform(48, 168)) * (1 if step == 1 else 3),
        )
    if kind == "spike":
        return Event(
            "spike",
            onset,
            float(rng.uniform(0.6, 2.0)),
            length_h=float(rng.choice([3, 6, 12])) * (1 if step == 1 else 24),
        )
    if kind == "step":
        return Event("step", onset, float(rng.uniform(0.3, 1.0)))
    return None


def corpus(
    *,
    hourly: int,
    daily: int,
    hourly_days: int,
    daily_days: int,
    seed: int,
    allocation: dict[str, float] | None = None,
) -> list[Bundle]:
    """The isolated experimental training corpus (SYNTHETIC)."""
    alloc = allocation or {"ai": 0.45, "technology": 0.25, "gaming": 0.30}
    rng = _rng(seed, "corpus")
    cats = list(alloc)
    probs = np.array([alloc[c] for c in cats])
    out: list[Bundle] = []
    families = 0
    for gran, count, days in (
        (TimeGranularity.HOUR, hourly, hourly_days),
        (TimeGranularity.DAY, daily, daily_days),
    ):
        step = 1.0 if gran is TimeGranularity.HOUR else 24.0
        periods = int(days * 24 / step)
        span = periods * step
        for i in range(count):
            if i % 3 == 0:
                families += 1
            cat = str(rng.choice(cats, p=probs / probs.sum()))
            kinds = [_pick(rng, _CATEGORY_EVENTS[cat]) for _ in range(int(rng.integers(1, 4)))]
            events = tuple(e for k in kinds if (e := _random_event(rng, k, span, step)) is not None)
            spec = Spec(
                key=f"corpus_{gran.value}_{i:04d}",
                seed=seed + i,
                granularity=gran,
                periods=periods,
                base=float(rng.uniform(8, 40)),
                noise=float(rng.uniform(0.05, 0.15)),
                diurnal=float(rng.uniform(0.0, 0.35)) if step == 1 else 0.0,
                weekly=float(rng.uniform(0.0, 0.2)),
                events=events,
                missing_rate=float(rng.choice([0.0, 0.0, 0.02, 0.05])),
                category=cat,
                group=f"family_{gran.value}_{families:03d}",
            )
            support: dict[MeasurementCategory, Spec] = {}
            if rng.random() < 0.5:  # proxy video activity loosely following the target
                support[M.VIDEO_ACTIVITY] = Spec(
                    key=spec.key + "_video",
                    seed=seed + 50_000 + i,
                    granularity=gran,
                    periods=periods,
                    base=float(rng.uniform(5, 30)),
                    noise=0.15,
                    diurnal=0.1 if step == 1 else 0.0,
                    events=tuple(
                        Event(
                            e.kind,
                            e.onset_h - step * 6,
                            e.gain * 0.8,
                            e.rise_h,
                            e.length_h,
                            e.decay_h,
                        )
                        for e in events
                    ),
                    measurement=M.VIDEO_ACTIVITY,
                    scale=ScaleType.ABSOLUTE_COUNT,
                    category=cat,
                )
            out.append(Bundle(spec, support))
    return out


def materialise(bundles: Sequence[Bundle], first_series: int = 1) -> list[SeriesBundle]:
    out = []
    nxt = 1
    for i, b in enumerate(bundles):
        sb, nxt = b.build(first_series + i, nxt)
        out.append(sb)
    return out
