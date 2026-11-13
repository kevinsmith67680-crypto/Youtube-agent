"""Validation of provider observations before persistence.

Each candidate is checked for provenance, timestamps, window/granularity consistency,
value validity under the declared scale, value vs sample-quality consistency, codes,
measurement/property labelling, normalisation context and synthetic labelling. All
problems found are reported (not just the first), so quarantined records carry a full
diagnosis.

Low, missing and suppressed values are *not* rejected: they are valid observations as long
as they are labelled consistently (e.g. ``SUPPRESSED`` with a NULL value).
"""

from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from trend_agent.schemas.enums import (
    SCALE_BOUNDS,
    VALUE_FORBIDDEN_QUALITIES,
    VALUE_REQUIRED_QUALITIES,
    DataNature,
    MeasurementType,
    ProviderType,
    RejectionReason,
    SampleQuality,
    ScaleType,
    SearchProperty,
    TimeGranularity,
)
from trend_agent.schemas.observation import ObservationCandidate, ValidatedObservation
from trend_agent.schemas.provider import ProviderDescriptor
from trend_agent.utils.codes import CodeValidationError, normalize_language, normalize_region
from trend_agent.utils.text import QueryValidationError, canonicalize_query, clean_original_query
from trend_agent.utils.time import NaiveDatetimeError, ensure_utc

SYNTHETIC_REFERENCE_PREFIX = "synthetic://"
EXTERNAL_PROPERTIES = frozenset(SearchProperty) - {
    SearchProperty.YOUTUBE_SEARCH,
    SearchProperty.NOT_APPLICABLE,
    SearchProperty.UNKNOWN,
}


@dataclass(frozen=True)
class ValidationConfig:
    clock_skew: timedelta = timedelta(minutes=5)
    earliest_valid: datetime = datetime(2004, 1, 1, tzinfo=UTC)  # Google Trends data start


@dataclass(frozen=True)
class ValidationIssue:
    reason: RejectionReason
    detail: str


@dataclass(frozen=True)
class RejectedCandidate:
    candidate: ObservationCandidate
    issues: tuple[ValidationIssue, ...]

    @property
    def primary_reason(self) -> RejectionReason:
        return self.issues[0].reason

    @property
    def detail(self) -> str:
        return "; ".join(f"{i.reason.value}: {i.detail}" for i in self.issues)


@dataclass
class ValidationReport:
    accepted: list[ValidatedObservation] = field(default_factory=list)
    rejected: list[RejectedCandidate] = field(default_factory=list)
    duplicates_in_batch: int = 0
    received: int = 0

    def rejected_by_reason(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for item in self.rejected:
            counts[item.primary_reason.value] += 1
        return dict(counts)


class _Issues:
    def __init__(self) -> None:
        self.items: list[ValidationIssue] = []

    def add(self, reason: RejectionReason, detail: str) -> None:
        self.items.append(ValidationIssue(reason, detail))


def _parse_enum[E: StrEnum](
    enum_cls: type[E], raw: str, issues: _Issues, reason: RejectionReason, label: str
) -> E | None:
    try:
        return enum_cls(raw)
    except ValueError:
        issues.add(reason, f"unrecognised {label} {raw!r}")
        return None


def _utc(value: datetime | None, label: str, issues: _Issues) -> datetime | None:
    if value is None:
        issues.add(RejectionReason.INVALID_TIMESTAMP, f"{label} is missing")
        return None
    try:
        return ensure_utc(value)
    except NaiveDatetimeError:
        issues.add(RejectionReason.INVALID_TIMESTAMP, f"{label} is timezone-naive")
        return None


def validate_candidate(
    c: ObservationCandidate,
    descriptor: ProviderDescriptor,
    *,
    now: datetime,
    config: ValidationConfig | None = None,
) -> ValidatedObservation | list[ValidationIssue]:
    cfg = config or ValidationConfig()
    issues = _Issues()
    skew = cfg.clock_skew

    # --- provenance --------------------------------------------------------------
    if not c.provider_name:
        issues.add(RejectionReason.MISSING_PROVENANCE, "provider_name is missing")
    elif c.provider_name != descriptor.name:
        issues.add(
            RejectionReason.PROVIDER_MISMATCH,
            f"record claims provider {c.provider_name!r}, ingested via {descriptor.name!r}",
        )
    if not c.provider_version:
        issues.add(RejectionReason.MISSING_PROVENANCE, "provider_version is missing")
    elif not descriptor.accepts_version(c.provider_version):
        issues.add(
            RejectionReason.UNRECOGNIZED_PROVIDER_VERSION,
            f"version {c.provider_version!r} not interpretable by provider "
            f"{descriptor.name} {descriptor.version}",
        )
    if not c.source_reference or not c.source_reference.strip():
        issues.add(RejectionReason.MISSING_PROVENANCE, "source_reference is missing")
    if c.collected_at is None:
        issues.add(RejectionReason.MISSING_PROVENANCE, "collected_at is missing")

    # --- labels ------------------------------------------------------------------
    measurement = _parse_enum(
        MeasurementType,
        c.measurement_type,
        issues,
        RejectionReason.UNSUPPORTED_MEASUREMENT_TYPE,
        "measurement_type",
    )
    if measurement is not None and measurement not in descriptor.measurement_types:
        issues.add(
            RejectionReason.UNSUPPORTED_MEASUREMENT_TYPE,
            f"provider does not declare measurement type {measurement.value!r}",
        )
    prop = _parse_enum(
        SearchProperty,
        c.search_property,
        issues,
        RejectionReason.MISLABELED_SEARCH_PROPERTY,
        "search_property",
    )
    if prop is not None and prop not in descriptor.search_properties:
        issues.add(
            RejectionReason.MISLABELED_SEARCH_PROPERTY,
            f"provider does not declare search property {prop.value!r}",
        )
    if measurement is MeasurementType.DIRECT_SEARCH_INTEREST and prop not in (
        None,
        SearchProperty.YOUTUBE_SEARCH,
    ):
        issues.add(
            RejectionReason.MISLABELED_SEARCH_PROPERTY,
            "direct_search_interest requires search_property=youtube_search",
        )
    if (
        measurement is MeasurementType.EXTERNAL_SEARCH_INTEREST
        and prop is not None
        and prop not in EXTERNAL_PROPERTIES
    ):
        issues.add(
            RejectionReason.MISLABELED_SEARCH_PROPERTY,
            f"external_search_interest cannot have search_property={prop.value!r}",
        )
    granularity = _parse_enum(
        TimeGranularity, c.granularity, issues, RejectionReason.INVALID_ENUM_VALUE, "granularity"
    )
    scale = _parse_enum(ScaleType, c.scale_type, issues, RejectionReason.INVALID_SCALE, "scale")
    quality = _parse_enum(
        SampleQuality,
        c.sample_quality,
        issues,
        RejectionReason.INVALID_ENUM_VALUE,
        "sample_quality",
    )
    nature = _parse_enum(
        DataNature, c.data_nature, issues, RejectionReason.INVALID_ENUM_VALUE, "data_nature"
    )
    if nature is DataNature.UNAVAILABLE:
        issues.add(RejectionReason.INVALID_ENUM_VALUE, "'unavailable' is not a value nature")

    # --- synthetic labelling -----------------------------------------------------
    synthetic_provider = descriptor.provider_type is ProviderType.SYNTHETIC
    if nature is not None and synthetic_provider != (nature is DataNature.SYNTHETIC):
        issues.add(
            RejectionReason.SYNTHETIC_LABEL_MISMATCH,
            f"provider type {descriptor.provider_type.value!r} emitted data_nature "
            f"{nature.value!r}",
        )
    if (
        nature is DataNature.SYNTHETIC
        and c.source_reference
        and not c.source_reference.startswith(SYNTHETIC_REFERENCE_PREFIX)
    ):
        issues.add(
            RejectionReason.SYNTHETIC_LABEL_MISMATCH,
            f"synthetic records need a {SYNTHETIC_REFERENCE_PREFIX!r} source_reference",
        )

    # --- timestamps and window ---------------------------------------------------
    start = _utc(c.observation_start, "observation_start", issues)
    end = _utc(c.observation_end, "observation_end", issues)
    collected = _utc(c.collected_at, "collected_at", issues) if c.collected_at else None
    available = _utc(c.available_at, "available_at", issues) if c.available_at else None

    if collected is not None and collected > now + skew:
        issues.add(RejectionReason.INVALID_TIMESTAMP, "collected_at is in the future")
    if start is not None and start < cfg.earliest_valid:
        issues.add(
            RejectionReason.INVALID_TIMESTAMP,
            f"observation_start precedes {cfg.earliest_valid.date()}",
        )
    if start is not None and end is not None:
        if end < start:
            issues.add(RejectionReason.INVALID_WINDOW, "observation_end precedes observation_start")
        elif granularity is not None:
            low, high = granularity.allowed_durations()
            duration = end - start
            if not low <= duration <= high:
                issues.add(
                    RejectionReason.GRANULARITY_MISMATCH,
                    f"window of {duration} does not match granularity {granularity.value}",
                )
    if start is not None and collected is not None and start > collected + skew:
        issues.add(RejectionReason.INVALID_WINDOW, "window starts after collection time")
    if (
        end is not None
        and collected is not None
        and end > collected + skew
        and quality is not SampleQuality.PARTIAL_PERIOD
    ):
        issues.add(
            RejectionReason.INVALID_WINDOW,
            "window was incomplete at collection but is not labelled partial_period",
        )
    if available is not None:
        if collected is not None and available > collected + skew:
            issues.add(RejectionReason.INVALID_TIMESTAMP, "available_at is after collected_at")
        if start is not None and available < start - skew:
            issues.add(RejectionReason.INVALID_TIMESTAMP, "available_at precedes the window")

    # --- value -------------------------------------------------------------------
    value = c.value
    if value is not None:
        if not math.isfinite(value):
            issues.add(RejectionReason.NON_FINITE_VALUE, f"value {value!r} is not finite")
        elif scale is not None:
            minimum, maximum, integer_only = SCALE_BOUNDS[scale]
            if value < minimum or (maximum is not None and value > maximum):
                upper = "inf" if maximum is None else str(maximum)
                issues.add(
                    RejectionReason.VALUE_OUT_OF_RANGE,
                    f"value {value} outside [{minimum}, {upper}] for scale {scale.value}",
                )
            elif integer_only and not float(value).is_integer():
                issues.add(
                    RejectionReason.VALUE_OUT_OF_RANGE,
                    f"scale {scale.value} requires integer values",
                )
    if quality in VALUE_REQUIRED_QUALITIES and value is None:
        issues.add(RejectionReason.INCONSISTENT_SAMPLE_QUALITY, f"{quality} requires a value")
    if quality in VALUE_FORBIDDEN_QUALITIES and value is not None:
        issues.add(RejectionReason.INCONSISTENT_SAMPLE_QUALITY, f"{quality} must not carry a value")

    # --- codes and query ---------------------------------------------------------
    geography = _code(c.geography, normalize_region, RejectionReason.INVALID_REGION, issues)
    if geography is None and c.geography is None:
        issues.add(RejectionReason.INVALID_REGION, "geography is missing")
    elif geography is not None and not descriptor.supports_region(geography):
        issues.add(RejectionReason.INVALID_REGION, f"region {geography} not supported")
    language = _code(c.language, normalize_language, RejectionReason.INVALID_LANGUAGE, issues)
    query_region = _code(c.query_region, normalize_region, RejectionReason.INVALID_REGION, issues)
    query_language = _code(
        c.query_language, normalize_language, RejectionReason.INVALID_LANGUAGE, issues
    )
    canonical: str | None = None
    try:
        canonical = canonicalize_query(c.query)
    except QueryValidationError as exc:
        issues.add(RejectionReason.INVALID_QUERY, str(exc))

    # --- normalisation context ---------------------------------------------------
    ctx = c.normalization_context
    if ctx is None:
        issues.add(RejectionReason.MISSING_NORMALIZATION_CONTEXT, "normalization_context missing")
    else:
        if scale is not None and ctx.scale_type is not scale:
            issues.add(
                RejectionReason.INVALID_SCALE,
                f"normalization_context scale {ctx.scale_type.value} != {scale.value}",
            )
        if ctx.scale_type is ScaleType.RELATIVE_PER_REQUEST and not ctx.batch_id:
            issues.add(
                RejectionReason.MISSING_NORMALIZATION_CONTEXT,
                "per-request relative values require a normalisation batch_id",
            )
        if (
            ctx.granularity is not None
            and granularity is not None
            and ctx.granularity is not granularity
        ):
            issues.add(
                RejectionReason.INVALID_SCALE,
                "normalization_context granularity differs from observation granularity",
            )
        if ctx.search_property is not None and prop is not None and ctx.search_property is not prop:
            issues.add(
                RejectionReason.INVALID_SCALE,
                "normalization_context search_property differs from observation",
            )

    if issues.items:
        return issues.items

    # All fields below were checked above; narrow the types for the type checker.
    assert measurement is not None
    assert prop is not None
    assert granularity is not None
    assert scale is not None
    assert quality is not None
    assert nature is not None
    assert start is not None
    assert end is not None
    assert collected is not None
    assert geography is not None
    assert canonical is not None
    assert ctx is not None
    assert c.provider_name is not None
    assert c.provider_version is not None
    assert c.source_reference is not None
    return ValidatedObservation(
        canonical_query=canonical,
        original_query=clean_original_query(c.query),
        query_language=query_language,
        query_region=query_region,
        provider_name=c.provider_name,
        provider_version=c.provider_version,
        measurement_type=measurement,
        search_property=prop,
        observation_start=start,
        observation_end=end,
        granularity=granularity,
        collected_at=collected,
        available_at=available,
        value=value,
        scale_type=scale,
        normalization_context=ctx,
        geography=geography,
        language=language,
        sample_quality=quality,
        data_nature=nature,
        source_reference=c.source_reference.strip(),
    )


def _code(
    raw: str | None,
    normalizer: Any,
    reason: RejectionReason,
    issues: _Issues,
) -> str | None:
    if raw is None:
        return None
    try:
        return str(normalizer(raw))
    except CodeValidationError as exc:
        issues.add(reason, str(exc))
        return None


def validate_batch(
    candidates: Iterable[ObservationCandidate],
    descriptor: ProviderDescriptor,
    *,
    now: datetime,
    config: ValidationConfig | None = None,
) -> ValidationReport:
    """Validate candidates and resolve in-batch duplicates.

    Identical repeats of the same identity are counted and dropped. Records that share an
    identity but disagree on the measurement are *all* rejected: there is no basis for
    choosing one, and silently picking one would hide a provider inconsistency.
    """
    report = ValidationReport()
    valid: list[tuple[ObservationCandidate, ValidatedObservation]] = []
    for candidate in candidates:
        report.received += 1
        result = validate_candidate(candidate, descriptor, now=now, config=config)
        if isinstance(result, ValidatedObservation):
            valid.append((candidate, result))
        else:
            report.rejected.append(RejectedCandidate(candidate, tuple(result)))

    groups: dict[tuple[Any, ...], list[tuple[ObservationCandidate, ValidatedObservation]]] = {}
    for item in valid:
        groups.setdefault(item[1].identity(), []).append(item)
    for members in groups.values():
        first = members[0][1]
        if all(first.same_measurement(other) for _, other in members[1:]):
            report.accepted.append(first)
            report.duplicates_in_batch += len(members) - 1
            continue
        values = sorted({str(m.value) for _, m in members})
        for candidate, _ in members:
            report.rejected.append(
                RejectedCandidate(
                    candidate,
                    (
                        ValidationIssue(
                            RejectionReason.DUPLICATE_CONFLICT,
                            f"{len(members)} records share one identity with differing "
                            f"measurements (values: {', '.join(values)})",
                        ),
                    ),
                )
            )
    return report
