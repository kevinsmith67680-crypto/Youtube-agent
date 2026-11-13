"""Comparison compatibility between observations.

Later phases compute growth and surges. Doing that across incomparable values (e.g. two
Google-Trends-style samples normalised independently, or web vs YouTube interest) produces
meaningless numbers. Every comparison of two values must therefore pass
:func:`check_comparability` first, and series should be built only within one
:func:`comparability_key` group.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass

from trend_agent.schemas.enums import MeasurementType, SampleQuality, ScaleType
from trend_agent.schemas.observation import ValidatedObservation

# Scales whose values are only meaningful within one normalisation batch.
BATCH_SCOPED_SCALES = frozenset(
    {ScaleType.RELATIVE_PER_REQUEST, ScaleType.RANK, ScaleType.PRESENCE}
)
UNRELIABLE_QUALITIES = frozenset(
    {
        SampleQuality.PARTIAL_PERIOD,
        SampleQuality.LOW_VOLUME,
        SampleQuality.SUPPRESSED,
        SampleQuality.MISSING,
    }
)


@dataclass(frozen=True)
class ComparabilityResult:
    comparable: bool
    reasons: tuple[str, ...]

    def __bool__(self) -> bool:
        return self.comparable


def _scale_signature(obs: ValidatedObservation) -> dict[str, object]:
    """Fields of the normalisation context that must match for values to share a scale."""
    ctx = obs.normalization_context
    if obs.scale_type in BATCH_SCOPED_SCALES:
        return {"normalization_key": obs.normalization_key}
    if obs.scale_type in (ScaleType.CONSISTENT_INDEX, ScaleType.ESTIMATED_COUNT):
        return {
            "method": ctx.method,
            "scale_version": ctx.scale_version,
            "category": ctx.category,
        }
    return {"method": ctx.method}  # ABSOLUTE_COUNT: counting method must match


def series_signature(obs: ValidatedObservation) -> dict[str, object]:
    """Everything (except the value and window) that must match for direct comparison."""
    return {
        "measurement_type": obs.measurement_type.value,
        "search_property": obs.search_property.value,
        "provider": obs.provider_name,
        "data_nature": obs.data_nature.value,
        "scale_type": obs.scale_type.value,
        "granularity": obs.granularity.value,
        "geography": obs.geography,
        "language": obs.language,
        "scale": _scale_signature(obs),
    }


def comparability_key(obs: ValidatedObservation) -> str:
    payload = json.dumps(series_signature(obs), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def check_comparability(a: ValidatedObservation, b: ValidatedObservation) -> ComparabilityResult:
    """Decide whether the *values* of two observations can be compared directly."""
    reasons: list[str] = []
    for obs in (a, b):
        if obs.measurement_type is MeasurementType.UNKNOWN:
            reasons.append("measurement type is unknown")
        if obs.value is None:
            reasons.append(f"observation has no value ({obs.sample_quality.value})")
        elif obs.sample_quality in UNRELIABLE_QUALITIES:
            reasons.append(f"observation quality is {obs.sample_quality.value}")
    sig_a, sig_b = series_signature(a), series_signature(b)
    labels = {
        "measurement_type": "different measurement types",
        "search_property": "different search properties (e.g. YouTube vs web search)",
        "provider": "different providers",
        "data_nature": "different data natures (e.g. synthetic vs real, sampled vs actual)",
        "scale_type": "different scale types",
        "granularity": "different time granularities",
        "geography": "different geographies",
        "language": "different languages",
    }
    for field_name, message in labels.items():
        if sig_a[field_name] != sig_b[field_name]:
            reasons.append(f"{message}: {sig_a[field_name]} vs {sig_b[field_name]}")
    if sig_a["scale_type"] == sig_b["scale_type"] and sig_a["scale"] != sig_b["scale"]:
        if a.scale_type in BATCH_SCOPED_SCALES:
            reasons.append(
                "values were normalised in different batches/samples and are not on a common scale"
            )
        else:
            reasons.append("scale definitions differ (method, version or category)")
    unique = tuple(dict.fromkeys(reasons))
    return ComparabilityResult(comparable=not unique, reasons=unique)


def group_by_comparability(
    observations: Iterable[ValidatedObservation],
) -> dict[str, list[ValidatedObservation]]:
    """Partition observations into groups whose members share a scale."""
    groups: dict[str, list[ValidatedObservation]] = defaultdict(list)
    for obs in observations:
        groups[comparability_key(obs)].append(obs)
    for members in groups.values():
        members.sort(key=lambda o: (o.observation_start, o.observation_end))
    return dict(groups)
