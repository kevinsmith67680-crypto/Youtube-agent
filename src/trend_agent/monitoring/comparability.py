"""Comparability engine for stored time-series observations.

Two values may be compared directly only when they measure the same thing on the same
scale: same provider, measurement category (incl. what a synthetic value simulates),
search property, region, language, granularity, unit, scale type and - for per-request
scales - the very same normalisation batch. Nothing is converted: Google web interest is
never turned into YouTube interest, video activity never shares a unit with search
interest, and independently normalised 0-100 series are never stitched together.

Every decision returns a status and machine-readable reasons:

* COMPARABLE              - nothing in the metadata argues against comparison
* COMPARABLE_WITH_CAVEATS - same scale, but at least one value is partial, low-volume,
                            revised, or the provider version changed
* INCOMPATIBLE            - different measurement definition or scale
* INSUFFICIENT_METADATA   - the metadata needed to decide is missing
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from trend_agent.schemas.enums import SampleQuality, ScaleType
from trend_agent.schemas.monitoring import (
    PROXY_CATEGORIES,
    ComparabilityReason,
    ComparabilityStatus,
    QualityStatus,
)
from trend_agent.schemas.time_series import StoredPoint

R = ComparabilityReason
BATCH_SCOPED = frozenset({ScaleType.RELATIVE_PER_REQUEST, ScaleType.RANK})
ENGINE_VERSION = "comparability-v1"


@dataclass(frozen=True)
class ComparabilityAssessment:
    status: ComparabilityStatus
    reasons: tuple[ComparabilityReason, ...]
    details: tuple[str, ...] = ()

    @property
    def comparable(self) -> bool:
        return self.status in (
            ComparabilityStatus.COMPARABLE,
            ComparabilityStatus.COMPARABLE_WITH_CAVEATS,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "reasons": [r.value for r in self.reasons],
            "details": list(self.details),
            "engine": ENGINE_VERSION,
        }


def _metadata_gaps(p: StoredPoint, context: dict[str, Any] | None) -> list[ComparabilityReason]:
    gaps: list[ComparabilityReason] = []
    if not p.normalization_key or len(p.normalization_key) != 64:
        gaps.append(R.MISSING_NORMALIZATION_CONTEXT)
    if p.scale_type in BATCH_SCOPED and context is not None and not context.get("batch_id"):
        gaps.append(R.MISSING_BATCH_ID)
    return gaps


def assess_pair(
    a: StoredPoint,
    b: StoredPoint,
    *,
    contexts: dict[str, dict[str, Any]] | None = None,
) -> ComparabilityAssessment:
    """Can the values of ``a`` and ``b`` be compared directly?"""
    ctx = contexts or {}
    gaps = _metadata_gaps(a, ctx.get(a.normalization_key)) + _metadata_gaps(
        b, ctx.get(b.normalization_key)
    )
    if gaps:
        return ComparabilityAssessment(
            ComparabilityStatus.INSUFFICIENT_METADATA, tuple(dict.fromkeys(gaps))
        )
    hard: list[ComparabilityReason] = []
    details: list[str] = []

    def differ(reason: ComparabilityReason, left: object, right: object) -> None:
        if left != right:
            hard.append(reason)
            details.append(f"{reason.value}: {left} vs {right}")

    if a.is_synthetic != b.is_synthetic:
        hard.append(R.SYNTHETIC_VS_REAL)
    a_proxy = a.is_proxy or a.effective_category in PROXY_CATEGORIES
    b_proxy = b.is_proxy or b.effective_category in PROXY_CATEGORIES
    if a_proxy != b_proxy:
        hard.append(R.PROXY_VS_SEARCH_INTEREST)
    differ(R.DIFFERENT_MEASUREMENT, a.effective_category.value, b.effective_category.value)
    differ(R.DIFFERENT_PROVIDER, a.provider_name, b.provider_name)
    differ(R.DIFFERENT_REGION, a.region, b.region)
    differ(R.DIFFERENT_LANGUAGE, a.language, b.language)
    differ(R.DIFFERENT_GRANULARITY, a.granularity.value, b.granularity.value)
    differ(R.DIFFERENT_UNIT, a.unit, b.unit)
    differ(R.DIFFERENT_SCALE_TYPE, a.scale_type.value, b.scale_type.value)
    if a.scale_type == b.scale_type and a.normalization_key != b.normalization_key:
        reason = (
            R.DIFFERENT_NORMALIZATION_CONTEXT
            if a.scale_type in BATCH_SCOPED
            else R.DIFFERENT_SCALE_DEFINITION
        )
        hard.append(reason)
        details.append(f"{reason.value}: {a.normalization_key[:12]} vs {b.normalization_key[:12]}")
    if hard:
        return ComparabilityAssessment(
            ComparabilityStatus.INCOMPATIBLE, tuple(dict.fromkeys(hard)), tuple(details)
        )
    caveats: list[ComparabilityReason] = []
    for p in (a, b):
        if p.value is None:
            return ComparabilityAssessment(
                ComparabilityStatus.INCOMPATIBLE,
                (R.NO_VALUE,),
                (f"observation {p.id} has no value ({p.sample_quality.value}); not zero",),
            )
        if p.sample_quality is SampleQuality.PARTIAL_PERIOD:
            caveats.append(R.PARTIAL_PERIOD)
        if p.sample_quality is SampleQuality.LOW_VOLUME:
            caveats.append(R.LOW_VOLUME)
        if p.revision > 1:
            caveats.append(R.REVISED_VALUE)
        if p.quality_status is QualityStatus.CONFLICTING:
            caveats.append(R.CONFLICTING_VALUE)
    if a.provider_version != b.provider_version:
        caveats.append(R.PROVIDER_VERSION_CHANGED)
    if caveats:
        return ComparabilityAssessment(
            ComparabilityStatus.COMPARABLE_WITH_CAVEATS, tuple(dict.fromkeys(caveats))
        )
    return ComparabilityAssessment(ComparabilityStatus.COMPARABLE, ())


def series_signature(p: StoredPoint) -> tuple[Any, ...]:
    """Everything except the window/value that must match for direct comparison."""
    return (
        p.provider_name,
        p.effective_category.value,
        p.is_synthetic,
        p.is_proxy,
        p.region,
        p.language,
        p.granularity.value,
        p.unit,
        p.scale_type.value,
        p.normalization_key,
    )


@dataclass(frozen=True)
class ComparableGroup:
    signature: tuple[Any, ...]
    points: tuple[StoredPoint, ...]

    @property
    def size(self) -> int:
        return len(self.points)


def comparable_groups(points: Iterable[StoredPoint]) -> list[ComparableGroup]:
    """Partition current points into groups whose members share one scale.

    Groups are never merged; a consumer must analyse each group on its own.
    """
    groups: dict[tuple[Any, ...], list[StoredPoint]] = defaultdict(list)
    for p in points:
        if p.is_current:
            groups[series_signature(p)].append(p)
    out = [
        ComparableGroup(sig, tuple(sorted(members, key=lambda p: (p.period_start, p.id))))
        for sig, members in groups.items()
    ]
    return sorted(out, key=lambda g: (-g.size, str(g.signature)))


def assess_series(points: Sequence[StoredPoint]) -> ComparabilityAssessment:
    """Can all ``points`` be treated as one directly comparable series?"""
    if len(points) < 2:
        return ComparabilityAssessment(ComparabilityStatus.COMPARABLE, ())
    first = points[0]
    worst = ComparabilityAssessment(ComparabilityStatus.COMPARABLE, ())
    order = list(ComparabilityStatus)
    reasons: list[ComparabilityReason] = []
    for other in points[1:]:
        result = assess_pair(first, other)
        reasons.extend(result.reasons)
        if order.index(result.status) > order.index(worst.status):
            worst = result
    return ComparabilityAssessment(worst.status, tuple(dict.fromkeys(reasons)), worst.details)
