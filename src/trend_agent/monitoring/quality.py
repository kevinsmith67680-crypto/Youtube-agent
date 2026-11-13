"""Monitoring data-quality checks and health summaries.

Checks distinguish *source behaviour* from *audience behaviour*: a provider outage, an
authentication failure or a schema change is a source failure; low-volume or missing
values reported by a provider are properties of the data (never zero demand, never a
failure); constant values are only noted when the source does not legitimately report
unchanged data. Nothing here interprets values as rising or falling demand.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from trend_agent.monitoring.config import QualityThresholds
from trend_agent.monitoring.scheduler import period_step
from trend_agent.schemas.monitoring import (
    FreshnessStatus,
    HealthStatus,
    QualityIssue,
    Severity,
)
from trend_agent.schemas.scheduling import ProviderProfile
from trend_agent.schemas.time_series import StoredPoint


@dataclass(frozen=True)
class Finding:
    issue: QualityIssue
    severity: Severity
    detail: str
    count: int = 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "issue": self.issue.value,
            "severity": self.severity.value,
            "detail": self.detail,
            "count": self.count,
        }


@dataclass
class QualityReport:
    scope: str  # series:<id> | target:<id> | provider:<name> | category:<name>
    status: HealthStatus
    findings: list[Finding] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "status": self.status.value,
            "findings": [f.as_dict() for f in self.findings],
            "stats": self.stats,
        }


def status_of(findings: Sequence[Finding], has_data: bool = True) -> HealthStatus:
    if any(f.severity is Severity.ERROR for f in findings):
        return HealthStatus.UNHEALTHY
    if any(f.severity is Severity.WARNING for f in findings):
        return HealthStatus.DEGRADED
    return HealthStatus.HEALTHY if has_data else HealthStatus.UNKNOWN


@dataclass(frozen=True)
class AttemptStats:
    attempts: int = 0
    failures: int = 0
    in_batch_duplicates: int = 0
    invalid_timestamps: int = 0
    auth_failures: int = 0
    validation_failures: int = 0
    rejected: int = 0


def check_series(
    points: Sequence[StoredPoint],
    *,
    series_id: int,
    profile: ProviderProfile | None,
    thresholds: QualityThresholds,
    freshness: FreshnessStatus | None,
    arrival: Sequence[tuple[datetime, datetime]],
    provider_versions: Sequence[str],
    attempts: AttemptStats,
) -> QualityReport:
    findings: list[Finding] = []
    current = sorted((p for p in points if p.is_current), key=lambda p: p.period_start)
    revisions = sum(1 for p in points if p.revision_kind == "revision")
    conflicts = sum(1 for p in points if p.revision_kind == "conflict")
    stats: dict[str, Any] = {
        "observations": len(current),
        "revisions": revisions,
        "conflicts": conflicts,
    }
    if not current:
        return QualityReport(f"series:{series_id}", HealthStatus.UNKNOWN, findings, stats)
    regular = profile is None or profile.publication_cadence is not None
    step = period_step(current[0].granularity)
    if step is not None and regular:
        missing = 0
        longest = 0
        for a, b in itertools.pairwise(current):
            gap = round((b.period_start - a.period_start) / step) - 1
            if gap > 0:
                missing += gap
                longest = max(longest, gap)
        stats["missing_periods"] = missing
        if missing:
            findings.append(
                Finding(
                    QualityIssue.MISSING_PERIODS,
                    Severity.WARNING,
                    f"{missing} expected period(s) absent (unknown, not zero)",
                    missing,
                )
            )
        if longest >= thresholds.unexpected_gap_periods:
            findings.append(
                Finding(
                    QualityIssue.UNEXPECTED_GAP,
                    Severity.WARNING,
                    f"longest gap {longest} period(s)",
                    longest,
                )
            )
    missing_values = sum(1 for p in current if p.value is None)
    if missing_values:
        findings.append(
            Finding(
                QualityIssue.MISSING_VALUES,
                Severity.INFO,
                "provider reported missing/suppressed values (stored as NULL, not zero)",
                missing_values,
            )
        )
    low = sum(1 for p in current if p.sample_quality.value == "low_volume")
    if low:
        findings.append(
            Finding(
                QualityIssue.LOW_VOLUME_VALUES,
                Severity.INFO,
                "low-volume values: a data property, not a source failure",
                low,
            )
        )
    if attempts.in_batch_duplicates:
        findings.append(
            Finding(
                QualityIssue.DUPLICATE_RECORDS,
                Severity.INFO,
                "provider repeated points within responses; stored once",
                attempts.in_batch_duplicates,
            )
        )
    late = 0
    seen_max: datetime | None = None
    for period_start, _ingested in arrival:
        if seen_max is not None and period_start < seen_max:
            late += 1
        seen_max = period_start if seen_max is None else max(seen_max, period_start)
    if late:
        findings.append(
            Finding(
                QualityIssue.OUT_OF_ORDER_ARRIVAL,
                Severity.INFO,
                "older periods arrived after newer ones (late data or backfill)",
                late,
            )
        )
    if freshness in (FreshnessStatus.PROVIDER_DELAYED, FreshnessStatus.COLLECTION_DELAYED):
        findings.append(
            Finding(QualityIssue.STALE_SERIES, Severity.WARNING, f"freshness {freshness.value}")
        )
    elif freshness is FreshnessStatus.SOURCE_UNAVAILABLE:
        findings.append(
            Finding(
                QualityIssue.STALE_PROVIDER,
                Severity.WARNING,
                "source unavailable; no new data is being collected",
            )
        )
    if attempts.invalid_timestamps:
        findings.append(
            Finding(
                QualityIssue.INVALID_TIMESTAMPS,
                Severity.WARNING,
                "records rejected for invalid timestamps",
                attempts.invalid_timestamps,
            )
        )
    if len(provider_versions) > 1:
        findings.append(
            Finding(
                QualityIssue.PROVIDER_VERSION_CHANGED,
                Severity.INFO,
                f"provider versions: {', '.join(provider_versions)}",
            )
        )
    if attempts.validation_failures:
        findings.append(
            Finding(
                QualityIssue.SCHEMA_CHANGE_SUSPECTED,
                Severity.WARNING,
                "responses failed validation (possible provider schema change)",
                attempts.validation_failures,
            )
        )
    constant_expected = bool(profile and profile.constant_values_expected)
    if not constant_expected:
        run = best = 1
        for a, b in itertools.pairwise(current):
            run = run + 1 if a.value is not None and a.value == b.value else 1
            best = max(best, run)
        if best >= thresholds.constant_run_periods:
            findings.append(
                Finding(
                    QualityIssue.CONSTANT_VALUES,
                    Severity.INFO,
                    f"{best} consecutive identical values (may be legitimate)",
                    best,
                )
            )
    if current and revisions / len(current) > thresholds.frequent_revision_ratio:
        findings.append(
            Finding(
                QualityIssue.FREQUENT_REVISIONS,
                Severity.WARNING,
                f"{revisions} revision(s) for {len(current)} periods",
                revisions,
            )
        )
    if conflicts:
        findings.append(
            Finding(
                QualityIssue.CONFLICTING_VALUES,
                Severity.WARNING,
                "values contradicting an immutable source were kept as conflicts",
                conflicts,
            )
        )
    missing_prov = sum(1 for p in current if not p.source_reference or not p.data_policy_id)
    if missing_prov:
        findings.append(
            Finding(
                QualityIssue.MISSING_PROVENANCE,
                Severity.ERROR,
                "observations without source reference or data policy",
                missing_prov,
            )
        )
    if attempts.auth_failures:
        findings.append(
            Finding(
                QualityIssue.AUTHENTICATION_FAILURES,
                Severity.ERROR,
                "authorization failures in the error window",
                attempts.auth_failures,
            )
        )
    if attempts.attempts and attempts.failures / attempts.attempts > 0.5:
        findings.append(
            Finding(
                QualityIssue.PROVIDER_ERRORS,
                Severity.WARNING,
                f"{attempts.failures}/{attempts.attempts} attempts failed",
                attempts.failures,
            )
        )
    return QualityReport(f"series:{series_id}", status_of(findings), findings, stats)


def check_target_scales(series_scales: Sequence[tuple[str, str, str]]) -> list[Finding]:
    """(provider, scale_type, normalization_key) of a target's search series."""
    findings: list[Finding] = []
    by_provider: dict[str, set[str]] = {}
    scale_types = set()
    for provider, scale, key in series_scales:
        by_provider.setdefault(provider, set()).add(key)
        scale_types.add(scale)
    for provider, keys in by_provider.items():
        if len(keys) > 1:
            findings.append(
                Finding(
                    QualityIssue.NORMALIZATION_CHANGED,
                    Severity.WARNING,
                    f"{provider}: {len(keys)} normalisation contexts; series are kept separate",
                    len(keys),
                )
            )
    if len(scale_types) > 1:
        findings.append(
            Finding(
                QualityIssue.INCOMPATIBLE_SCALES,
                Severity.WARNING,
                f"scale types: {', '.join(sorted(scale_types))}",
                len(scale_types),
            )
        )
    return findings


def aggregate(
    scope: str, reports: Sequence[QualityReport], extra: Sequence[Finding] = ()
) -> QualityReport:
    findings = list(extra)
    counts: dict[str, int] = {}
    for r in reports:
        counts[r.status.value] = counts.get(r.status.value, 0) + 1
        for f in r.findings:
            if f.severity is not Severity.INFO:
                findings.append(f)
    merged: dict[tuple[QualityIssue, Severity], Finding] = {}
    for f in findings:
        key = (f.issue, f.severity)
        if key in merged:
            prev = merged[key]
            merged[key] = Finding(f.issue, f.severity, prev.detail, prev.count + f.count)
        else:
            merged[key] = f
    status = status_of(list(merged.values()), has_data=bool(reports) or bool(extra))
    return QualityReport(scope, status, list(merged.values()), {"series_status": counts})
