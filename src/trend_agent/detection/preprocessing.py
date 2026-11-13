"""Causal preprocessing: stored observations -> analysis-ready, comparable series.

Steps (``prepare``):

1. **Knowledge at T** - for every observation window keep the highest revision that was
   *available* at the evaluation time (Phase 3 availability rule: provider publication
   time, else the conservative ``max(collected_at, ingested_at)``). Conflict rows never
   count. Later revisions do not leak backwards.
2. **Comparability** - every value must share one series signature (provider, effective
   measurement, region, language, granularity, unit, scale, normalisation context).
   Mixed inputs are rejected as INCOMPATIBLE with Phase 3 reason codes; nothing is
   rescaled or stitched.
3. **Grid** - values are placed on the period grid of their granularity (hour, UTC day,
   Monday-aligned week). Empty slots are *missing periods*; rows without a value are
   *missing values*. Both stay NaN: nothing is interpolated or zero-filled.
4. **Quality** - partial periods are excluded from analysis, low-volume values are kept
   but flagged, values outside the scale bounds make the series INVALID_SCALE, and
   presence/rank scales are UNSUPPORTED_SCALE (not magnitudes).
5. **Status** - READY / PARTIALLY_READY / INSUFFICIENT_DATA / IRREGULAR_SAMPLING ... with
   reasons. Original values are preserved unchanged.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import numpy as np
import numpy.typing as npt

from trend_agent.detection.config import DetectionConfig, ScaleFamily, scale_family
from trend_agent.monitoring.comparability import (
    ComparabilityAssessment,
    assess_series,
    series_signature,
)
from trend_agent.monitoring.replay import AvailabilityMode, availability
from trend_agent.schemas.detection import PreprocessStatus, Reason
from trend_agent.schemas.enums import SCALE_BOUNDS, SampleQuality, ScaleType, TimeGranularity
from trend_agent.schemas.evidence import effective_category
from trend_agent.schemas.monitoring import ComparabilityStatus, MeasurementCategory
from trend_agent.schemas.time_series import StoredPoint

FloatArray = npt.NDArray[np.float64]
BoolArray = npt.NDArray[np.bool_]

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_DAY_SECONDS = 86_400
_GRID_STEPS: dict[TimeGranularity, int] = {
    TimeGranularity.HOUR: 3_600,
    TimeGranularity.DAY: _DAY_SECONDS,
    TimeGranularity.WEEK: 7 * _DAY_SECONDS,
}
_WEEK_OFFSET = 4 * _DAY_SECONDS  # 1970-01-05 was the first Monday after the epoch
USABLE = frozenset({SampleQuality.COMPLETE, SampleQuality.LOW_VOLUME})


def slot_of(moment: datetime, granularity: TimeGranularity) -> int:
    """Grid index of the period starting at ``moment`` (floor in UTC)."""
    seconds = int((moment - EPOCH).total_seconds())
    step = _GRID_STEPS[granularity]
    if granularity is TimeGranularity.WEEK:
        return (seconds - _WEEK_OFFSET) // step
    return seconds // step


def slot_start(slot: int, granularity: TimeGranularity) -> datetime:
    step = _GRID_STEPS[granularity]
    offset = _WEEK_OFFSET if granularity is TimeGranularity.WEEK else 0
    return EPOCH + timedelta(seconds=slot * step + offset)


def phase_of(slot: int, granularity: TimeGranularity) -> int | None:
    """Seasonal phase: weekday (Mon=0) for daily data, hour of day for hourly data."""
    if granularity is TimeGranularity.DAY:
        return (slot + 3) % 7  # 1970-01-01 was a Thursday
    if granularity is TimeGranularity.HOUR:
        return slot % 24
    return None


def cycle_block(slot: int, granularity: TimeGranularity) -> int | None:
    """Calendar block in which the seasonal profile is refitted (ISO week / UTC day)."""
    if granularity is TimeGranularity.DAY:
        return (slot + 3) // 7
    if granularity is TimeGranularity.HOUR:
        return slot // 24
    return None


def knowledge_at(
    points: Iterable[StoredPoint],
    as_of: datetime | None,
    mode: AvailabilityMode = AvailabilityMode.PUBLICATION,
) -> list[StoredPoint]:
    """Per window, the newest revision available at ``as_of`` (all if ``as_of`` is None)."""
    best: dict[tuple[int, datetime, datetime], StoredPoint] = {}
    for p in points:
        if p.revision_kind == "conflict":
            continue
        if as_of is not None and availability(p, mode)[0] > as_of:
            continue
        key = (p.series_id, p.period_start, p.period_end)
        held = best.get(key)
        if held is None or p.revision > held.revision:
            best[key] = p
    return sorted(best.values(), key=lambda p: (p.period_start, p.id))


@dataclass
class AnalysisSeries:
    """One comparable series on its period grid (values NaN where unknown)."""

    series_id: int
    target_id: int | None
    provider: str
    measurement: MeasurementCategory  # effective category (what synthetic data simulates)
    is_proxy: bool
    is_synthetic: bool
    granularity: TimeGranularity
    scale_type: ScaleType
    family: ScaleFamily | None
    unit: str
    normalization_key: str
    region: str
    language: str | None
    first_slot: int
    values: FloatArray  # usable values (NaN = missing period/value or excluded)
    points: list[StoredPoint | None]  # stored observation per slot (None = missing period)
    status: PreprocessStatus
    reasons: list[str] = field(default_factory=list)
    comparability: ComparabilityAssessment | None = None
    missing_periods: int = 0
    missing_values: int = 0
    partial_periods: int = 0
    low_volume: int = 0
    revised_values: int = 0
    recent_coverage: float = 0.0
    as_of: datetime | None = None
    mode: AvailabilityMode = AvailabilityMode.PUBLICATION

    @property
    def n_slots(self) -> int:
        return len(self.values)

    @property
    def last_slot(self) -> int:
        return self.first_slot + self.n_slots - 1

    @property
    def usable(self) -> BoolArray:
        return np.isfinite(self.values)

    @property
    def n_usable(self) -> int:
        return int(np.count_nonzero(self.usable))

    def index_of(self, slot: int) -> int:
        return slot - self.first_slot

    def point_at(self, slot: int) -> StoredPoint | None:
        i = slot - self.first_slot
        return self.points[i] if 0 <= i < self.n_slots else None

    def period(self, slot: int) -> tuple[datetime, datetime]:
        p = self.point_at(slot)
        if p is not None:
            return p.period_start, p.period_end
        start = slot_start(slot, self.granularity)
        return start, slot_start(slot + 1, self.granularity)

    def available_at(self, slot: int) -> tuple[datetime | None, str | None]:
        p = self.point_at(slot)
        if p is None:
            return None, None
        return availability(p, self.mode)

    @property
    def analysable(self) -> bool:
        return self.status in (
            PreprocessStatus.READY,
            PreprocessStatus.PARTIALLY_READY,
            PreprocessStatus.IRREGULAR_SAMPLING,
            PreprocessStatus.INSUFFICIENT_DATA,
        )

    @property
    def sequential_ok(self) -> bool:
        """CUSUM / change point need a regular cadence."""
        return self.status in (PreprocessStatus.READY, PreprocessStatus.PARTIALLY_READY)


def _empty(points: Sequence[StoredPoint], status: PreprocessStatus, reason: str) -> AnalysisSeries:
    head = points[0] if points else None
    measurement = (
        effective_category(head.measurement_category, head.simulated_category)
        if head is not None
        else MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST
    )
    return AnalysisSeries(
        series_id=head.series_id if head else 0,
        target_id=head.target_id if head else None,
        provider=head.provider_name if head else "?",
        measurement=measurement,
        is_proxy=bool(head and head.is_proxy),
        is_synthetic=bool(head and head.is_synthetic),
        granularity=head.granularity if head else TimeGranularity.DAY,
        scale_type=head.scale_type if head else ScaleType.CONSISTENT_INDEX,
        family=scale_family(head.scale_type) if head else None,
        unit=head.unit if head else "",
        normalization_key=head.normalization_key if head else "",
        region=head.region if head else "",
        language=head.language if head else None,
        first_slot=0,
        values=np.array([], dtype=np.float64),
        points=[],
        status=status,
        reasons=[reason],
    )


def prepare(
    points: Sequence[StoredPoint],
    cfg: DetectionConfig,
    *,
    as_of: datetime | None = None,
    mode: AvailabilityMode = AvailabilityMode.PUBLICATION,
    already_known: bool = False,
) -> AnalysisSeries:
    """Build the analysis-ready series from stored revisions (see module docstring)."""
    known = list(points) if already_known else knowledge_at(points, as_of, mode)
    if not known:
        return _empty(points, PreprocessStatus.NO_DATA, "no observation available at this time")
    head = known[0]
    pre = cfg.preprocessing
    if len({series_signature(p) for p in known}) > 1 or len({p.series_id for p in known}) > 1:
        assessment = assess_series(known)
        out = _empty(known, PreprocessStatus.INCOMPATIBLE, Reason.INCOMPARABLE.value)
        out.comparability = assessment
        out.reasons += [r.value for r in assessment.reasons] or ["different series"]
        if any(r.value == "different_normalization_context" for r in assessment.reasons):
            out.reasons.append(Reason.NORMALIZATION_CHANGED.value)
        return out
    if head.granularity not in pre.supported_granularities:
        return _empty(
            known,
            PreprocessStatus.UNSUPPORTED_GRANULARITY,
            f"granularity {head.granularity.value} is not supported for detection",
        )
    if head.scale_type in pre.unsupported_scales or scale_family(head.scale_type) is None:
        return _empty(
            known,
            PreprocessStatus.UNSUPPORTED_SCALE,
            f"scale {head.scale_type.value} is not a magnitude; upward anomalies are undefined",
        )
    gran = head.granularity
    limit = pre.max_history_periods.get(gran, 730)
    by_slot: dict[int, StoredPoint] = {}
    collisions = 0
    for p in known:
        s = slot_of(p.period_start, gran)
        if s in by_slot:
            collisions += 1
            if p.period_start <= by_slot[s].period_start:
                continue
        by_slot[s] = p
    last = max(by_slot)
    first = max(min(by_slot), last - limit + 1)
    n = last - first + 1
    values = np.full(n, np.nan, dtype=np.float64)
    slots: list[StoredPoint | None] = [None] * n
    out = AnalysisSeries(
        series_id=head.series_id,
        target_id=head.target_id,
        provider=head.provider_name,
        measurement=effective_category(head.measurement_category, head.simulated_category),
        is_proxy=head.is_proxy,
        is_synthetic=head.is_synthetic,
        granularity=gran,
        scale_type=head.scale_type,
        family=scale_family(head.scale_type),
        unit=head.unit,
        normalization_key=head.normalization_key,
        region=head.region,
        language=head.language,
        first_slot=first,
        values=values,
        points=slots,
        status=PreprocessStatus.READY,
        as_of=as_of,
        mode=mode,
    )
    lo, hi, _ = SCALE_BOUNDS[head.scale_type]
    invalid = 0
    for s, p in by_slot.items():
        if s < first:
            continue
        i = s - first
        slots[i] = p
        if p.revision > 1:
            out.revised_values += 1
        if p.sample_quality is SampleQuality.PARTIAL_PERIOD:
            out.partial_periods += 1
            continue  # in-progress window: never analysed
        if p.value is None or p.sample_quality not in USABLE:
            out.missing_values += 1
            continue
        if p.sample_quality is SampleQuality.LOW_VOLUME:
            out.low_volume += 1
        if not math.isfinite(p.value) or p.value < lo or (hi is not None and p.value > hi):
            invalid += 1
            continue
        values[i] = p.value  # original value, unchanged
    out.missing_periods = sum(1 for p in slots if p is None)
    # Comparability caveats (revisions, provider version changes, low volume) over the
    # recent usable values; missing values are handled above, not as incompatibility.
    valued = [p for p in known if p.value is not None and p.sample_quality in USABLE]
    out.comparability = assess_series(valued[-60:])
    if out.comparability.status is ComparabilityStatus.INSUFFICIENT_METADATA:
        out.status = PreprocessStatus.INCOMPATIBLE
        out.reasons += [r.value for r in out.comparability.reasons]
        return out
    if invalid:
        out.status = PreprocessStatus.INVALID_SCALE
        out.reasons.append(f"{invalid} value(s) outside the {head.scale_type.value} bounds")
        return out
    window = min(n, pre.regularity_window.get(gran, 56))
    recent = values[-window:]
    out.recent_coverage = round(float(np.count_nonzero(np.isfinite(recent))) / window, 4)
    usable = out.n_usable
    robust = cfg.baseline.robust
    if collisions:
        out.reasons.append(f"{collisions} window(s) overlapped on the period grid")
    if usable < robust.provisional_min_samples:
        out.status = PreprocessStatus.INSUFFICIENT_DATA
        out.reasons.append(
            f"{usable} usable value(s) < {robust.provisional_min_samples} needed for any "
            "baseline (cold start)"
        )
    elif out.recent_coverage < pre.min_recent_coverage:
        out.status = PreprocessStatus.IRREGULAR_SAMPLING
        out.reasons.append(
            f"only {out.recent_coverage:.0%} of the last {window} periods hold a usable value "
            f"(< {pre.min_recent_coverage:.0%}); sequential detectors disabled"
        )
    elif usable < robust.min_samples or out.missing_periods or out.missing_values:
        out.status = PreprocessStatus.PARTIALLY_READY
        if usable < robust.min_samples:
            out.reasons.append(
                f"{usable} usable values < {robust.min_samples} for a ready baseline"
            )
        if out.missing_periods or out.missing_values:
            out.reasons.append(
                f"{out.missing_periods} missing period(s), {out.missing_values} missing "
                "value(s): left missing, never zero-filled"
            )
    if out.partial_periods:
        out.reasons.append(f"{out.partial_periods} partial period(s) excluded")
    if out.low_volume:
        out.reasons.append(f"{out.low_volume} low-volume value(s) flagged")
    return out
