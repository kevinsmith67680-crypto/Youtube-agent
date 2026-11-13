"""Deterministic synthetic time series with independent ground truth (tests/evaluation).

Everything here is SYNTHETIC. Series are generated from explicit parameters and a seed:

    value_t = clip(round(base * (1 + weekly_t) + effect_t + noise_t [+ common factor]), 0, 100)

with AR(1) noise, optional weekly seasonality, optional shared factors (correlated queries),
missing days (left missing, never zero) and injected events (spike, step, ramp,
limited-time event, drop).

**Ground truth is defined by the generator, not by any detector threshold**: an injected
upward event is a *surge* when its peak effect is at least ``SURGE_RELATIVE`` (50 %) of the
base level **and** at least ``SURGE_ABSOLUTE`` (10 index points). Smaller upward effects
are *ambiguous* (excluded from precision and recall); drops are not surges. The label also
records the onset period and the first period in which the effect reached the surge size
("observable onset"), so detection delay can be measured from either.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np

from trend_agent.discovery.models import Category
from trend_agent.schemas.enums import SampleQuality, ScaleType, TimeGranularity
from trend_agent.schemas.monitoring import MeasurementCategory, QualityStatus
from trend_agent.schemas.time_series import StoredPoint

SURGE_RELATIVE = 0.5
SURGE_ABSOLUTE = 10.0
AMBIGUOUS_RELATIVE = 0.2
DAY = timedelta(days=1)
START = datetime(2026, 1, 5, tzinfo=UTC)  # a Monday
SYNTHETIC_PROVIDER = "synthetic_trends"


@dataclass(frozen=True)
class InjectedEvent:
    kind: str  # spike | step | ramp | event | drop
    onset: int  # day index
    magnitude: float  # peak absolute effect (index points); negative for drops
    duration: int = 1  # spike/event length in days; ramp length; ignored for step

    def effect(self, i: int) -> float:
        d = i - self.onset
        if d < 0:
            return 0.0
        if self.kind == "spike":
            return self.magnitude if d < self.duration else 0.0
        if self.kind in ("step", "drop"):
            return self.magnitude
        if self.kind == "ramp":
            return self.magnitude * min(1.0, (d + 1) / max(self.duration, 1))
        if self.kind == "event":  # rise in 1 day, plateau, fast exponential decline
            if d < self.duration:
                return self.magnitude
            return self.magnitude * math.exp(-(d - self.duration + 1) / 1.5)
        raise ValueError(f"unknown event kind {self.kind}")

    def end(self, days: int) -> int:
        if self.kind in ("step", "drop"):
            return days - 1
        if self.kind == "event":
            return self.onset + self.duration + 3
        return self.onset + self.duration - 1


@dataclass(frozen=True)
class GroundTruthLabel:
    series_key: str
    kind: str
    label: str  # surge | ambiguous | not_surge
    onset_index: int
    observable_index: int | None  # first day the effect reached the surge size
    end_index: int
    magnitude: float
    relative_magnitude: float


@dataclass(frozen=True)
class SyntheticSeriesSpec:
    key: str
    seed: int
    days: int = 150
    base: float = 30.0
    noise: float = 0.08  # relative sd of the noise
    noise_floor: float = 1.0  # absolute minimum sd (index points)
    ar: float = 0.3
    weekly: float = 0.0  # relative weekend uplift (Sat/Sun)
    missing_rate: float = 0.0
    events: tuple[InjectedEvent, ...] = ()
    factor: str | None = None  # shared factor id (correlated queries)
    factor_weight: float = 0.0
    category: Category = Category.AI
    measurement: MeasurementCategory = MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST
    scale: ScaleType = ScaleType.CONSISTENT_INDEX
    publication_lag: timedelta = timedelta(hours=36)
    start: datetime = START
    provider: str = SYNTHETIC_PROVIDER
    cluster: str = ""


@dataclass
class GeneratedSeries:
    spec: SyntheticSeriesSpec
    values: list[float | None]
    labels: list[GroundTruthLabel] = field(default_factory=list)

    def period(self, i: int) -> tuple[datetime, datetime]:
        start = self.spec.start + DAY * i
        return start, start + DAY

    def available_at(self, i: int) -> datetime:
        return self.period(i)[1] + self.spec.publication_lag


def _rng(seed: int, key: str) -> np.random.Generator:
    digest = hashlib.sha256(f"{seed}|{key}".encode()).digest()
    return np.random.default_rng(int.from_bytes(digest[:8], "big"))


def _ar_noise(rng: np.random.Generator, n: int, phi: float) -> np.ndarray:
    eps = rng.normal(0.0, 1.0, n)
    out = np.empty(n)
    out[0] = eps[0]
    scale = math.sqrt(max(1e-9, 1.0 - phi * phi))
    for i in range(1, n):
        out[i] = phi * out[i - 1] + scale * eps[i]
    return out


def factor_series(factor: str, seed: int, days: int) -> np.ndarray:
    return _ar_noise(_rng(seed, f"factor:{factor}"), days, 0.6)


def label_for(spec: SyntheticSeriesSpec, ev: InjectedEvent) -> GroundTruthLabel:
    peak = max((ev.effect(i) for i in range(spec.days)), key=abs, default=0.0)
    rel = peak / max(spec.base, 1e-9)
    size = max(SURGE_ABSOLUTE, SURGE_RELATIVE * spec.base)
    if peak >= size:
        label = "surge"
    elif peak > 0 and (rel >= AMBIGUOUS_RELATIVE or peak >= 0.5 * SURGE_ABSOLUTE):
        label = "ambiguous"  # a real but smaller increase: neither a hit nor a miss
    else:
        label = "not_surge"  # no increase (drops included)
    observable = next((i for i in range(ev.onset, spec.days) if ev.effect(i) >= size), None)
    return GroundTruthLabel(
        series_key=spec.key,
        kind=ev.kind,
        label=label,
        onset_index=ev.onset,
        observable_index=observable,
        end_index=min(spec.days - 1, ev.end(spec.days)),
        magnitude=round(peak, 4),
        relative_magnitude=round(rel, 4),
    )


def generate(spec: SyntheticSeriesSpec) -> GeneratedSeries:
    rng = _rng(spec.seed, spec.key)
    n = spec.days
    noise = _ar_noise(rng, n, spec.ar)
    sd = max(spec.noise_floor, spec.noise * spec.base)
    common = factor_series(spec.factor, spec.seed, n) if spec.factor and spec.factor_weight else 0.0
    weekday = np.array([(spec.start + DAY * i).weekday() for i in range(n)])
    weekly = np.where(weekday >= 5, spec.weekly, 0.0)
    effect = np.array([sum(ev.effect(i) for ev in spec.events) for i in range(n)])
    level = spec.base * (1.0 + weekly) + effect
    raw = level + sd * (noise * (1.0 - spec.factor_weight) + common * spec.factor_weight)
    upper = (
        100.0 if spec.scale in (ScaleType.CONSISTENT_INDEX, ScaleType.RELATIVE_PER_REQUEST) else 1e9
    )
    values = np.clip(np.round(raw), 0.0, upper)
    missing = rng.random(n) < spec.missing_rate
    out: list[float | None] = [None if missing[i] else float(values[i]) for i in range(n)]
    return GeneratedSeries(spec, out, [label_for(spec, ev) for ev in spec.events])


def stored_points(
    series: GeneratedSeries,
    *,
    series_id: int,
    target_id: int,
    first_id: int,
    normalization_key: str | None = None,
) -> list[StoredPoint]:
    """In-memory StoredPoints (evaluation / replay without a database)."""
    spec = series.spec
    key = normalization_key or hashlib.sha256(f"norm|{spec.key}".encode()).hexdigest()
    unit = "items" if spec.scale is ScaleType.ABSOLUTE_COUNT else "index"
    out = []
    for i, v in enumerate(series.values):
        start, end = series.period(i)
        published = series.available_at(i)
        out.append(
            StoredPoint(
                id=first_id + i,
                series_id=series_id,
                target_id=target_id,
                provider_name=spec.provider,
                measurement_category=MeasurementCategory.SYNTHETIC_TEST_OBSERVATION,
                simulated_category=spec.measurement,
                period_start=start,
                period_end=end,
                granularity=TimeGranularity.DAY,
                value=v,
                sample_quality=SampleQuality.COMPLETE if v is not None else SampleQuality.MISSING,
                scale_type=spec.scale,
                normalization_key=key,
                unit=unit,
                region="US",
                language="en",
                provider_version="synthetic-1",
                provider_published_at=published,
                first_available_at=published,
                collected_at=published,
                ingested_at=published,
                revision=1,
                revision_kind="initial",
                is_current=True,
                supersedes_id=None,
                quality_status=QualityStatus.VALID,
                is_synthetic=True,
                is_proxy=spec.measurement
                not in (
                    MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST,
                    MeasurementCategory.EXTERNAL_SEARCH_INTEREST,
                ),
                source_reference=f"synthetic://detection/{spec.key}/{start.date()}",
                data_policy_id="synthetic-test-data@1",
            )
        )
    return out


# ----------------------------------------------------------------------------- datasets
def _choice(rng: np.random.Generator, items: list[str]) -> str:
    return items[int(rng.integers(0, len(items)))]


def benchmark_specs(
    split: str, count: int, *, days: int = 150, eval_start: int = 90, seed: int = 20261113
) -> list[SyntheticSeriesSpec]:
    """Mixed benchmark: ~50 % series with one injected event inside the evaluation window.

    ``split`` ('train' / 'test') selects disjoint seeds and cluster ids, so no series,
    event or topic cluster is shared between calibration and evaluation.
    """
    offset = {"train": 0, "test": 1_000_000, "null": 2_000_000}[split]
    rng = _rng(seed + offset, f"benchmark:{split}")
    cats = [Category.AI, Category.TECHNOLOGY, Category.GAMING]
    specs: list[SyntheticSeriesSpec] = []
    for k in range(count):
        base = float(np.exp(rng.uniform(math.log(8), math.log(70))))
        has_event = split != "null" and rng.random() < 0.5
        events: tuple[InjectedEvent, ...] = ()
        if has_event:
            kind = _choice(rng, ["spike", "step", "ramp", "event", "drop"])
            if kind == "drop":
                mag = -base * float(rng.uniform(0.3, 0.6))
            else:
                rel = float(np.exp(rng.uniform(math.log(0.25), math.log(2.5))))
                mag = base * rel
            onset = int(rng.integers(eval_start, days - 15))
            duration = {
                "spike": int(rng.integers(1, 3)),
                "step": 1,
                "ramp": int(rng.integers(10, 28)),
                "event": int(rng.integers(2, 5)),
                "drop": 1,
            }[kind]
            events = (InjectedEvent(kind, onset, round(mag, 2), duration),)
        specs.append(
            SyntheticSeriesSpec(
                key=f"{split}-{k:05d}",
                seed=seed + offset + k,
                days=days,
                base=round(base, 2),
                noise=float(rng.uniform(0.05, 0.15)),
                ar=float(rng.uniform(0.0, 0.5)),
                weekly=float(rng.uniform(0.2, 0.6)) if rng.random() < 0.4 else 0.0,
                missing_rate=float(rng.uniform(0.0, 0.15)) if rng.random() < 0.2 else 0.0,
                events=events,
                category=cats[k % 3],
                cluster=f"{split}-cluster-{k // 5}",
            )
        )
    return specs


def null_specs(
    count: int, *, days: int = 120, correlated_share: float = 0.5, seed: int = 20261114
) -> list[SyntheticSeriesSpec]:
    """Scenario L: many null series (no injected events), half sharing common factors."""
    rng = _rng(seed, "null-scale")
    cats = [Category.AI, Category.TECHNOLOGY, Category.GAMING]
    out = []
    for k in range(count):
        correlated = k < int(count * correlated_share)
        base = float(np.exp(rng.uniform(math.log(8), math.log(70))))
        out.append(
            SyntheticSeriesSpec(
                key=f"null-{k:05d}",
                seed=seed + 3_000_000 + k,
                days=days,
                base=round(base, 2),
                noise=float(rng.uniform(0.05, 0.15)),
                ar=float(rng.uniform(0.0, 0.5)),
                weekly=float(rng.uniform(0.2, 0.6)) if rng.random() < 0.4 else 0.0,
                factor=f"factor-{k % 20}" if correlated else None,
                factor_weight=0.5 if correlated else 0.0,
                category=cats[k % 3],
                cluster=f"null-cluster-{k // 5}",
            )
        )
    return out
