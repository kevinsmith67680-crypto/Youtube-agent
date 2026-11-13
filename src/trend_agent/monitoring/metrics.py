"""Read models: operational metrics, freshness, readiness, data quality and history.

Metrics use low-cardinality labels only (status, category, tier, provider, error
category) - never query text - and contain no secrets. These views are what the CLI shows
and what Phase 4 consumes (``Insights.readiness`` and ``Insights.history``).
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from trend_agent.db.monitoring_models import MonitoringTargetRow
from trend_agent.db.repositories.collection_jobs import JobRepository
from trend_agent.db.repositories.monitoring import MonitoringRepository
from trend_agent.db.repositories.time_series import SeriesSummary, TimeSeriesRepository
from trend_agent.db.session import transaction
from trend_agent.discovery.models import Category
from trend_agent.monitoring.comparability import comparable_groups
from trend_agent.monitoring.freshness import FreshnessAssessment, assess_freshness, estimate_delay
from trend_agent.monitoring.quality import (
    AttemptStats,
    Finding,
    QualityReport,
    aggregate,
    check_series,
    check_target_scales,
)
from trend_agent.monitoring.readiness import TargetReadiness, combine_target, evaluate_series
from trend_agent.monitoring.service import MonitoringService
from trend_agent.schemas.monitoring import (
    FreshnessStatus,
    HealthStatus,
    MonitoringStatus,
    QualityIssue,
    Severity,
)
from trend_agent.schemas.scheduling import DelayEstimate
from trend_agent.utils.text import canonicalize_query


@dataclass(frozen=True)
class TargetFreshness:
    target_id: int
    query: str
    provider: str | None
    category: str
    tier: str
    status: str
    is_proxy: bool
    assessment: FreshnessAssessment

    def as_dict(self) -> dict[str, Any]:
        a = self.assessment
        return {
            "target_id": self.target_id,
            "query": self.query,
            "provider": self.provider,
            "category": self.category,
            "tier": self.tier,
            "monitoring_status": self.status,
            "proxy": self.is_proxy,
            "freshness": a.status.value,
            "reason": a.reason,
            "latest_period_end": a.latest_period_end.isoformat() if a.latest_period_end else None,
            "expected_period_end": (
                a.expected_period_end.isoformat() if a.expected_period_end else None
            ),
            "expected_available_at": (
                a.expected_available_at.isoformat() if a.expected_available_at else None
            ),
        }


def _delay_dict(d: DelayEstimate | None) -> dict[str, Any] | None:
    if d is None:
        return None
    return {
        "p50_hours": round(d.p50.total_seconds() / 3600, 2) if d.p50 is not None else None,
        "p90_hours": round(d.p90.total_seconds() / 3600, 2) if d.p90 is not None else None,
        "basis": d.basis,
        "samples_exact": d.samples_exact,
        "samples_upper_bound": d.samples_upper_bound,
        "observed_cadence_hours": (
            round(d.observed_cadence.total_seconds() / 3600, 2) if d.observed_cadence else None
        ),
        "declared_delay_hours": (
            round(d.declared_delay.total_seconds() / 3600, 2) if d.declared_delay else None
        ),
        "note": "estimate from observed behaviour; not a guarantee",
    }


class Insights:
    def __init__(self, service: MonitoringService) -> None:
        self._svc = service
        self._factory = service.session_factory
        self._config = service.config
        self._collectors = service.collectors

    # ------------------------------------------------------------------ helpers
    def _targets(
        self,
        repo: MonitoringRepository,
        *,
        query: str | None = None,
        category: Category | None = None,
        statuses: list[MonitoringStatus] | None = None,
        include_synthetic: bool = True,
        limit: int = 1000,
    ) -> list[MonitoringTargetRow]:
        return repo.targets(
            statuses=statuses,
            category=category,
            query_text=canonicalize_query(query) if query else None,
            include_synthetic=include_synthetic,
            limit=limit,
        )

    def _delays(self, ts: TimeSeriesRepository) -> dict[str, DelayEstimate]:
        return {
            name: estimate_delay(ts.delay_samples(name), profile)
            for name, profile in self._config.providers.items()
        }

    def _freshness(
        self,
        target: MonitoringTargetRow,
        delays: dict[str, DelayEstimate],
        health: dict[str, str],
        now: datetime,
    ) -> FreshnessAssessment:
        provider = target.provider_in_use or target.preferred_provider
        profile = self._collectors.profile(provider) if provider else None
        available = provider is not None and self._collectors.availability(provider).available
        return assess_freshness(
            now=now,
            profile=profile,
            provider_available=available,
            provider_unhealthy=health.get(provider or "") == HealthStatus.UNHEALTHY.value,
            latest_period_end=target.latest_period_end,
            last_attempt_at=target.last_attempt_at,
            last_no_data_at=target.last_no_data_at,
            delay=delays.get(provider) if provider else None,
            planned_periods=self._config.tiers.rule(target.tier).collect_every_periods,
        )

    # ------------------------------------------------------------------ freshness
    def freshness(
        self, *, query: str | None = None, include_synthetic: bool = True
    ) -> dict[str, Any]:
        now = self._svc.now()
        with transaction(self._factory) as session:
            repo = MonitoringRepository(session)
            ts = TimeSeriesRepository(session)
            delays = self._delays(ts)
            health = {h.provider_name: h.status.value for h in repo.all_health()}
            targets = self._targets(
                repo,
                query=query,
                include_synthetic=include_synthetic,
                statuses=[s for s in MonitoringStatus if s is not MonitoringStatus.DISABLED],
            )
            texts = repo.query_text(list({t.query_id for t in targets}))
            rows = [
                TargetFreshness(
                    t.id,
                    texts[t.query_id][0],
                    t.provider_in_use or t.preferred_provider,
                    t.category.value,
                    t.tier.value,
                    t.status.value,
                    t.is_proxy,
                    self._freshness(t, delays, health, now),
                )
                for t in targets
            ]
        by_provider: dict[str, Counter[str]] = defaultdict(Counter)
        for r in rows:
            by_provider[r.provider or "-"][r.assessment.status.value] += 1
        providers = []
        for name, profile in sorted(self._config.providers.items()):
            avail = self._collectors.availability(name)
            providers.append(
                {
                    "provider": name,
                    "available": avail.available,
                    "status": avail.status,
                    "health": health.get(name, "unknown"),
                    "publication_cadence_hours": (
                        profile.publication_cadence.total_seconds() / 3600
                        if profile.publication_cadence
                        else None
                    ),
                    "granularity": profile.collection_granularity.value,
                    "delay": _delay_dict(delays.get(name)),
                    "targets": dict(by_provider.get(name, {})),
                }
            )
        return {
            "as_of": now.isoformat(),
            "providers": providers,
            "targets": [r.as_dict() for r in rows],
        }

    # ------------------------------------------------------------------ readiness
    def readiness(
        self,
        *,
        query: str | None = None,
        profile_name: str | None = None,
        include_synthetic: bool = True,
        include_proxy: bool = True,
    ) -> list[TargetReadiness]:
        now = self._svc.now()
        out: list[TargetReadiness] = []
        with transaction(self._factory) as session:
            repo = MonitoringRepository(session)
            ts = TimeSeriesRepository(session)
            delays = self._delays(ts)
            health = {h.provider_name: h.status.value for h in repo.all_health()}
            targets = self._targets(repo, query=query, include_synthetic=include_synthetic)
            if not include_proxy:
                targets = [t for t in targets if not t.is_proxy]
            texts = repo.query_text(list({t.query_id for t in targets}))
            summaries = ts.series_summaries(target_ids=[t.id for t in targets])
            by_target: dict[int, list[SeriesSummary]] = defaultdict(list)
            for s in summaries:
                if s.target_id is not None:
                    by_target[s.target_id].append(s)
            for t in targets:
                provider = t.provider_in_use or t.preferred_provider
                name, profile = (
                    (profile_name, self._config.readiness.profiles[profile_name])
                    if profile_name
                    else self._config.readiness_profile(provider or "")
                )
                available = (
                    provider is not None and self._collectors.availability(provider).available
                )
                fresh = self._freshness(t, delays, health, now)
                series = []
                for summary in by_target.get(t.id, []):
                    points = ts.points(series_ids=[summary.series_id], limit=100_000)
                    series_fresh = fresh.status if summary.provider_name == provider else None
                    series.append(
                        evaluate_series(
                            points,
                            series_id=summary.series_id,
                            profile_name=name,
                            profile=profile,
                            now=now,
                            freshness=series_fresh,
                            provider_available=(
                                self._collectors.availability(summary.provider_name).available
                            ),
                        )
                    )
                out.append(
                    combine_target(t.id, texts[t.query_id][0], provider, series, profile, available)
                )
        return out

    # ------------------------------------------------------------------ quality
    def quality(
        self,
        *,
        query: str | None = None,
        include_synthetic: bool = True,
        window: timedelta = timedelta(days=7),
    ) -> dict[str, Any]:
        now = self._svc.now()
        since = now - window
        series_reports: list[QualityReport] = []
        target_reports: list[tuple[MonitoringTargetRow, QualityReport]] = []
        with transaction(self._factory) as session:
            repo = MonitoringRepository(session)
            ts = TimeSeriesRepository(session)
            jobs = JobRepository(session)
            delays = self._delays(ts)
            health_rows = {h.provider_name: h for h in repo.all_health()}
            health = {k: v.status.value for k, v in health_rows.items()}
            targets = self._targets(repo, query=query, include_synthetic=include_synthetic)
            stats = jobs.attempt_stats_by_target([t.id for t in targets], since)
            summaries = ts.series_summaries(target_ids=[t.id for t in targets])
            by_target: dict[int, list[SeriesSummary]] = defaultdict(list)
            for s in summaries:
                if s.target_id is not None:
                    by_target[s.target_id].append(s)
            series_by_provider: dict[str, list[QualityReport]] = defaultdict(list)
            for t in targets:
                fresh = self._freshness(t, delays, health, now).status
                per_target = []
                attempt = AttemptStats(**stats.get(t.id, {}))
                for summary in by_target.get(t.id, []):
                    points = ts.points(
                        series_ids=[summary.series_id], current_only=False, limit=100_000
                    )
                    report = check_series(
                        points,
                        series_id=summary.series_id,
                        profile=self._collectors.profile(summary.provider_name),
                        thresholds=self._config.quality,
                        freshness=fresh if summary.provider_name == t.provider_in_use else None,
                        arrival=ts.arrival_order(summary.series_id),
                        provider_versions=ts.provider_versions(summary.series_id),
                        attempts=attempt,
                    )
                    report.stats.update(
                        {
                            "provider": summary.provider_name,
                            "category": summary.effective_category.value,
                            "proxy": summary.is_proxy,
                        }
                    )
                    per_target.append(report)
                    series_reports.append(report)
                    series_by_provider[summary.provider_name].append(report)
                scales = [
                    (s.provider_name, s.scale_type, s.normalization_key)
                    for s in by_target.get(t.id, [])
                    if not s.is_proxy
                ]
                extra = check_target_scales(scales)
                if t.status in (MonitoringStatus.STALE, MonitoringStatus.PROVIDER_UNAVAILABLE):
                    extra.append(
                        Finding(
                            QualityIssue.STALE_PROVIDER,
                            Severity.WARNING,
                            f"target {t.status.value}: {t.status_reason[:120]}",
                        )
                    )
                target_reports.append((t, aggregate(f"target:{t.id}", per_target, extra)))
            provider_reports = []
            for name in sorted(self._config.providers):
                extra = []
                h = health_rows.get(name)
                if h is not None and h.status is HealthStatus.UNHEALTHY:
                    extra.append(
                        Finding(
                            QualityIssue.PROVIDER_ERRORS,
                            Severity.ERROR,
                            f"{h.consecutive_failures} consecutive failures "
                            f"({h.last_error_category})",
                        )
                    )
                elif h is not None and h.status is HealthStatus.DEGRADED:
                    extra.append(
                        Finding(
                            QualityIssue.PROVIDER_ERRORS,
                            Severity.WARNING,
                            f"recent failure ({h.last_error_category})",
                        )
                    )
                provider_reports.append(
                    aggregate(f"provider:{name}", series_by_provider.get(name, []), extra)
                )
            texts = repo.query_text(list({t.query_id for t, _ in target_reports}))
        categories = []
        by_category: dict[str, list[QualityReport]] = defaultdict(list)
        for t, r in target_reports:
            by_category[t.category.value].append(r)
        for cat, reports in sorted(by_category.items()):
            categories.append(aggregate(f"category:{cat}", reports).as_dict())
        return {
            "as_of": now.isoformat(),
            "window_days": window.days,
            "providers": [r.as_dict() for r in provider_reports],
            "categories": categories,
            "targets": [
                {
                    **r.as_dict(),
                    "query": texts[t.query_id][0],
                    "category": t.category.value,
                    "monitoring_status": t.status.value,
                }
                for t, r in target_reports
            ],
            "series": [r.as_dict() for r in series_reports],
        }

    # ------------------------------------------------------------------ history
    def history(
        self,
        *,
        query: str,
        provider: str | None = None,
        include_revisions: bool = True,
        limit: int = 5000,
    ) -> dict[str, Any]:
        canonical = canonicalize_query(query)
        with transaction(self._factory) as session:
            repo = MonitoringRepository(session)
            ts = TimeSeriesRepository(session)
            targets = repo.targets(query_text=canonical, limit=100)
            ids = [t.id for t in targets]
            summaries = ts.series_summaries(target_ids=ids) if ids else []
            series_out = []
            for s in summaries:
                if provider and s.provider_name != provider:
                    continue
                points = ts.points(
                    series_ids=[s.series_id], current_only=not include_revisions, limit=limit
                )
                groups = comparable_groups(points)
                series_out.append(
                    {
                        "series_id": s.series_id,
                        "target_id": s.target_id,
                        "provider": s.provider_name,
                        "measurement": s.measurement_category.value,
                        "simulates": s.simulated_category.value if s.simulated_category else None,
                        "proxy": s.is_proxy,
                        "synthetic": s.is_synthetic,
                        "region": s.region,
                        "language": s.language,
                        "granularity": s.granularity,
                        "scale_type": s.scale_type,
                        "unit": s.unit,
                        "normalization_key": s.normalization_key[:16],
                        "normalization_context": s.normalization_context,
                        "observations": s.observations,
                        "revisions": s.revisions,
                        "conflicts": s.conflicts,
                        "comparable_groups": len(groups),
                        "points": [
                            {
                                "id": p.id,
                                "period_start": p.period_start.isoformat(),
                                "period_end": p.period_end.isoformat(),
                                "value": p.value,
                                "sample_quality": p.sample_quality.value,
                                "revision": p.revision,
                                "revision_kind": p.revision_kind,
                                "current": p.is_current,
                                "supersedes": p.supersedes_id,
                                "provider_published_at": (
                                    p.provider_published_at.isoformat()
                                    if p.provider_published_at
                                    else None
                                ),
                                "collected_at": p.collected_at.isoformat(),
                                "ingested_at": p.ingested_at.isoformat(),
                                "provider_version": p.provider_version,
                                "quality": p.quality_status.value,
                                "source_reference": p.source_reference,
                                "data_policy": p.data_policy_id,
                                "job_id": p.job_id,
                            }
                            for p in points
                        ],
                    }
                )
            return {
                "query": canonical,
                "targets": [
                    {
                        "target_id": t.id,
                        "measurement": t.measurement_category.value,
                        "status": t.status.value,
                        "provider": t.provider_in_use,
                        "proxy": t.is_proxy,
                    }
                    for t in targets
                ],
                "series": series_out,
            }

    # ------------------------------------------------------------------ status
    def status(self, *, window: timedelta = timedelta(days=1)) -> dict[str, Any]:
        now = self._svc.now()
        with transaction(self._factory) as session:
            repo = MonitoringRepository(session)
            jobs = JobRepository(session)
            ts = TimeSeriesRepository(session)
            breakdown = repo.target_breakdown()
            by_status: Counter[str] = Counter()
            by_category: dict[str, Counter[str]] = defaultdict(Counter)
            by_tier: Counter[str] = Counter()
            proxy_active = 0
            for cat, status, tier, proxy, n in breakdown:
                by_status[status] += n
                by_category[cat][status] += n
                if status in ("active", "stale", "quota_blocked"):
                    by_tier[tier] += n
                    if proxy:
                        proxy_active += n
            attempts = jobs.attempt_metrics(now - window)
            plans = repo.recent_plan_runs(5)
            health = [
                {
                    "provider": h.provider_name,
                    "status": h.status.value,
                    "consecutive_failures": h.consecutive_failures,
                    "last_success_at": h.last_success_at.isoformat() if h.last_success_at else None,
                    "last_error_category": h.last_error_category,
                }
                for h in repo.all_health()
            ]
            coverage = ts.coverage_counts()
            queue = jobs.counts_by_status()
        quota = [
            {"provider": p.name, **p.quota} for p in self._svc.providers() if p.quota is not None
        ]
        active = sum(by_status[s] for s in ("active", "stale", "quota_blocked"))
        readiness = Counter(
            r.state.value for r in self.readiness(include_synthetic=True, include_proxy=False)
        )
        return {
            "as_of": now.isoformat(),
            "window_hours": window.total_seconds() / 3600,
            "targets": {
                "active_total": active,
                "active_search_interest": active - proxy_active,
                "active_proxy": proxy_active,
                "by_status": dict(by_status),
                "by_category": {k: dict(v) for k, v in sorted(by_category.items())},
                "active_by_tier": dict(by_tier),
            },
            "jobs": {
                "by_status": queue,
                "queue_depth": sum(
                    queue.get(s, 0)
                    for s in ("pending", "claimed", "running", "retry_wait", "deferred")
                ),
            },
            "attempts": attempts,
            "quota": quota,
            "provider_health": health,
            "time_series": coverage,
            "baseline_readiness": dict(readiness),
            "recent_plans": [
                {
                    "at": p.started_at.isoformat(),
                    "jobs_created": p.jobs_created,
                    "capacity": p.capacity,
                    "skipped_no_new_data": p.details.get("skipped_no_new_data"),
                    "deferred_quota": p.details.get("deferred_quota"),
                }
                for p in plans
            ],
            "notes": [
                "Tiers are operational priority, not search growth.",
                "Counts contain no query text (low-cardinality labels only).",
            ],
        }


def freshness_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(r["freshness"] for r in rows))


__all__ = ["FreshnessStatus", "Insights", "freshness_counts"]
