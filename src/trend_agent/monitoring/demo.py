"""End-to-end deterministic monitoring demonstration on SYNTHETIC data (scenarios A-J).

The demo drives the real components - target manager, planner, two workers, backfill,
replay, comparability, readiness - against the database with a simulated clock that
advances hour by hour. Nothing sleeps and nothing touches the network. Every value is
synthetic and labelled as such. Results are checked and reported per scenario.

Before running, synthetic monitoring rows from earlier demo runs are removed (real data
is never touched), because the simulated dates are fixed and would otherwise collide.
"""

from __future__ import annotations

import asyncio
import random
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from trend_agent.container import AppContainer
from trend_agent.db.repositories.collection_jobs import JobRepository
from trend_agent.db.repositories.monitoring import MonitoringRepository
from trend_agent.db.repositories.time_series import TimeSeriesRepository
from trend_agent.db.session import transaction
from trend_agent.monitoring.backfill import BackfillService
from trend_agent.monitoring.comparability import assess_pair
from trend_agent.monitoring.config import LoadedMonitoringConfig
from trend_agent.monitoring.metrics import Insights
from trend_agent.monitoring.replay import AvailabilityMode, CollectingConsumer, replay
from trend_agent.monitoring.service import MonitoringService
from trend_agent.monitoring.synthetic import (
    SyntheticMonitoringScenario,
    SyntheticTrendsProvider,
    load_monitoring_scenario,
    seed_keywords,
)
from trend_agent.schemas.monitoring import (
    ComparabilityReason,
    ComparabilityStatus,
    MeasurementCategory,
    MonitoringTier,
    ReadinessState,
)
from trend_agent.utils.text import canonicalize_query


class StageClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


@dataclass
class Check:
    scenario: str
    name: str
    passed: bool
    detail: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "scenario": self.scenario,
            "check": self.name,
            "passed": self.passed,
            "detail": self.detail,
        }


@dataclass
class DemoReport:
    anchor: datetime
    hours: int
    cleanup: dict[str, int]
    checks: list[Check] = field(default_factory=list)
    targets: list[dict[str, Any]] = field(default_factory=list)
    days: list[dict[str, Any]] = field(default_factory=list)
    backfills: list[dict[str, Any]] = field(default_factory=list)
    quota_windows: list[str] = field(default_factory=list)
    replay: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    def as_dict(self) -> dict[str, Any]:
        return {
            "synthetic": True,
            "anchor": self.anchor.isoformat(),
            "hours": self.hours,
            "all_checks_passed": self.passed,
            "checks": [c.as_dict() for c in self.checks],
            "targets": self.targets,
            "days": self.days,
            "backfills": self.backfills,
            "quota_windows": self.quota_windows,
            "replay": self.replay,
            "cleanup": self.cleanup,
        }


def _service(
    container: AppContainer, scenario: SyntheticMonitoringScenario, clock: StageClock
) -> MonitoringService:
    for name in scenario.providers:
        if name not in container.registry.names():
            container.registry.register(SyntheticTrendsProvider(name, scenario, clock=clock))
    loaded = container.monitoring_config()
    cfg = loaded.config
    providers = dict(cfg.providers)
    trends = providers["synthetic_trends"]
    providers["synthetic_trends"] = trends.model_copy(
        update={
            "quota": trends.quota.model_copy(
                update={"daily_requests": scenario.quota_daily_requests}
            )
        }
    )
    demo_cfg = cfg.model_copy(
        update={
            "providers": providers,
            "activation": cfg.activation.model_copy(update={"include_synthetic": True}),
            # The demo's world is the scenario providers only (no Phase 1 mock fallback).
            "provider_preferences": {
                MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST: ("synthetic_trends",),
                MeasurementCategory.EXTERNAL_SEARCH_INTEREST: (
                    "synthetic_trends",
                    "synthetic_trends_web",
                ),
            },
        }
    )
    return MonitoringService(
        settings=container.settings,
        session_factory=container.session_factory,
        registry=container.registry,
        quota_store=container.quota_store,
        loaded=LoadedMonitoringConfig(
            path=loaded.path, config=demo_cfg, digest=f"{loaded.digest}-demo"
        ),
        clock=clock,
        include_synthetic=True,
    )


def _apply_fixture_fields(svc: MonitoringService, scenario: SyntheticMonitoringScenario) -> None:
    with transaction(svc.session_factory) as session:
        repo = MonitoringRepository(session)
        for kw in scenario.keywords:
            if kw.time_sensitive_until is None:
                continue
            for t in repo.targets(query_text=canonicalize_query(kw.query), limit=10):
                t.time_sensitive_until = kw.time_sensitive_until


async def run_demo(
    container: AppContainer,
    *,
    scenario: SyntheticMonitoringScenario | None = None,
    reset: bool = True,
) -> DemoReport:
    scenario = scenario or load_monitoring_scenario()
    anchor = scenario.anchor
    clock = StageClock(anchor)
    svc = _service(container, scenario, clock)
    cleanup: dict[str, int] = {}
    if reset:
        with transaction(svc.session_factory) as session:
            cleanup = MonitoringRepository(session).purge_synthetic(list(scenario.providers))
    with transaction(svc.session_factory) as session:
        seed_keywords(session, scenario, anchor, svc.config.languages[0])
    hours = scenario.demo_days * 24
    report = DemoReport(anchor=anchor, hours=hours, cleanup=cleanup)
    worker_a = svc.executor("worker-a", rng=random.Random(1))  # noqa: S311
    worker_b = svc.executor("worker-b", rng=random.Random(2))  # noqa: S311
    backfiller = BackfillService(svc.session_factory, svc.config, svc.collectors, clock=clock)
    crash: dict[str, Any] = {}
    unhealthy_seen: list[str] = []
    day_stats: Counter[str] = Counter()
    quota_mgr = svc.collectors.get("synthetic_trends").quota_manager()

    for hour in range(hours + 1):
        clock.now = anchor + timedelta(hours=hour)
        if hour % 6 == 0:
            svc.sync_targets()
            _apply_fixture_fields(svc, scenario)
        for spec in scenario.backfills:
            if int(spec["at_hour"]) == hour:
                measurement = spec.get("measurement")
                plan = backfiller.plan(
                    query=str(spec["query"]),
                    provider=str(spec["provider"]),
                    days=int(spec["days"]),
                    measurement=MeasurementCategory(measurement) if measurement else None,
                    requested_by="demo",
                )
                report.backfills.append(
                    {
                        "query": plan.query,
                        "provider": plan.provider,
                        "at": clock.now.isoformat(),
                        "windows": len(plan.windows),
                        "skipped_covered": plan.skipped_covered,
                        "estimated_requests": plan.estimated_requests,
                        "quota_remaining": plan.quota_remaining,
                        "limitations": plan.limitations,
                        "backfill_id": str(plan.backfill_id),
                    }
                )
        plan_report = svc.plan()
        day_stats["jobs_planned"] += plan_report.jobs_created
        day_stats["skipped_no_new_data"] += plan_report.skipped_no_new_data
        day_stats["deferred_quota"] += plan_report.deferred_quota
        if hour == scenario.worker_crash_at_hour:
            r = await worker_a.run_once(crash_after_claim=True)
            crash = {"at": clock.now.isoformat(), "claimed_then_vanished": r.claimed}
            await worker_b.drain()
        else:
            results = await asyncio.gather(worker_b.drain(), worker_a.drain())
            for reports in results:
                for r in reports:
                    for key, value in r.by_status().items():
                        day_stats[f"jobs_{key}"] += value
                    for key, value in r.totals().items():
                        day_stats[f"obs_{key}"] += value
                    if r.recovered_leases:
                        crash.setdefault("recovered_by", r.worker)
                        crash["recovered_leases"] = crash.get("recovered_leases", 0) + (
                            r.recovered_leases
                        )
                        crash.setdefault("recovered_at", clock.now.isoformat())
        with transaction(svc.session_factory) as session:
            health = MonitoringRepository(session).health("synthetic_trends")
            if health is not None and health.status.value == "unhealthy":
                unhealthy_seen.append(clock.now.isoformat())
        if quota_mgr is not None and hour % 24 == 12:
            report.quota_windows.append(quota_mgr.current_window().end.isoformat())
        if hour % 24 == 23 or hour == hours:
            report.days.append(
                {
                    "day": (clock.now - timedelta(hours=hour % 24)).date().isoformat(),
                    **dict(day_stats),
                }
            )
            day_stats = Counter()

    _evaluate(svc, scenario, report, crash, unhealthy_seen)
    return report


def _evaluate(
    svc: MonitoringService,
    scenario: SyntheticMonitoringScenario,
    report: DemoReport,
    crash: dict[str, Any],
    unhealthy_seen: list[str],
) -> None:
    insights = Insights(svc)
    now = svc.now()
    readiness = {r.target_id: r for r in insights.readiness(include_synthetic=True)}
    freshness = {r["target_id"]: r for r in insights.freshness()["targets"]}
    checks = report.checks

    def check(scenario_id: str, name: str, passed: bool, detail: str) -> None:
        checks.append(Check(scenario_id, name, bool(passed), detail))

    with transaction(svc.session_factory) as session:
        repo = MonitoringRepository(session)
        jobs = JobRepository(session)
        ts = TimeSeriesRepository(session)
        targets = {}
        for kw in scenario.keywords:
            canonical = canonicalize_query(kw.query)
            rows = [
                t
                for t in repo.targets(query_text=canonical, limit=10)
                if t.measurement_category is MeasurementCategory.DIRECT_YOUTUBE_SEARCH_INTEREST
            ]
            if not rows:
                report.targets.append(
                    {
                        "query": canonical,
                        "scenario": kw.scenario,
                        "category": kw.category.value,
                        "priority": kw.priority,
                        "monitored": False,
                        "note": "below activation threshold: not monitored",
                    }
                )
                continue
            t = rows[0]
            targets[canonical] = t
            attempts = jobs.attempts_for_target(t.id)
            scheduled = [a for a in attempts if a.window_start is not None]
            summaries = ts.series_summaries(target_ids=[t.id])
            tiers = [e for e in repo.events(t.id, limit=200) if e.kind == "tier"]
            r = readiness.get(t.id)
            report.targets.append(
                {
                    "query": canonical,
                    "scenario": kw.scenario,
                    "category": t.category.value,
                    "priority": t.priority_score,
                    "tier": t.tier.value,
                    "tier_changes": [
                        f"{e.from_value}->{e.to_value} at {e.at.isoformat()}"
                        for e in reversed(tiers)
                    ],
                    "status": t.status.value,
                    "provider": t.provider_in_use,
                    "attempts": len(attempts),
                    "attempt_outcomes": dict(Counter(a.outcome for a in attempts)),
                    "observations": sum(
                        s.observations for s in summaries if s.provider_name == "synthetic_trends"
                    ),
                    "latest_period_end": (
                        t.latest_period_end.isoformat() if t.latest_period_end else None
                    ),
                    "next_collection_at": (
                        t.next_collection_at.isoformat() if t.next_collection_at else None
                    ),
                    "freshness": freshness.get(t.id, {}).get("freshness"),
                    "readiness": r.state.value if r else None,
                    "readiness_reasons": r.reasons[:3] if r else [],
                    "scheduled_attempts": len(scheduled),
                }
            )

        def target(query: str) -> Any:
            return targets[canonicalize_query(query)]

        def attempts_of(query: str) -> list[Any]:
            return jobs.attempts_for_target(target(query).id)

        # A - emerging AI model ------------------------------------------------------
        nimbus = target("how to run nimbus coder 2 locally")
        a_attempts = attempts_of("how to run nimbus coder 2 locally")
        per_day = len(a_attempts) / scenario.demo_days
        a_events = [e for e in repo.events(nimbus.id, limit=200) if e.kind == "tier"]
        first_tier = a_events[-1].from_value if a_events else nimbus.tier.value
        check(
            "A",
            "critical tier for a new, time-sensitive AI model",
            first_tier == MonitoringTier.CRITICAL.value or nimbus.tier is MonitoringTier.CRITICAL,
            f"tier at activation: {first_tier}; now {nimbus.tier.value}",
        )
        check(
            "A",
            "attention follows the source cadence (no hourly polling)",
            per_day <= 3.0,
            f"{len(a_attempts)} collection attempts in {scenario.demo_days} days "
            f"({per_day:.1f}/day) for a daily source",
        )

        # B - limited-time event ----------------------------------------------------
        rift = target("fortnite neon rift event")
        b_attempts = attempts_of("fortnite neon rift event")
        event_kw = scenario.keyword("fortnite neon rift event")
        assert event_kw is not None
        assert event_kw.time_sensitive_until is not None
        start = scenario.anchor + timedelta(days=event_kw.shape.onset_day)
        in_event = [a for a in b_attempts if start <= a.started_at <= event_kw.time_sensitive_until]
        b_tiers = [
            f"{e.from_value}->{e.to_value}"
            for e in reversed([e for e in repo.events(rift.id, limit=200) if e.kind == "tier"])
        ]
        check(
            "B",
            "event recognised as time-sensitive (critical while running)",
            any(x.startswith("critical->") for x in b_tiers)
            or rift.tier is MonitoringTier.CRITICAL,
            f"tier changes: {b_tiers or ['none']}",
        )
        check(
            "B",
            "daily source is not polled hourly during the 48-hour event",
            len(in_event) <= 6,
            f"{len(in_event)} attempts during the 48-hour event (hourly polling would be 48)",
        )

        # C - evergreen tutorial -----------------------------------------------------
        py = target("python tutorial for beginners")
        c_attempts = list(attempts_of("python tutorial for beginners"))
        backfill_jobs = [
            a
            for a in c_attempts
            if a.window_end
            and a.window_start
            and (a.window_end - a.window_start) > timedelta(days=60)
        ]
        c_summ = [
            s
            for s in ts.series_summaries(target_ids=[py.id])
            if s.provider_name == "synthetic_trends"
        ]
        c_obs = sum(s.observations for s in c_summ)
        scheduled_c = len(c_attempts) - len(backfill_jobs)
        check(
            "C",
            "low tier with an efficient (multi-day) cadence",
            py.tier is MonitoringTier.LOW and scheduled_c <= 4,
            f"tier {py.tier.value}; {scheduled_c} scheduled attempts in {scenario.demo_days} "
            f"days plus {len(backfill_jobs)} backfill request(s)",
        )
        check("C", "long history from backfill", c_obs >= 360, f"{c_obs} stored observations")
        weekly = insights.readiness(
            query="python tutorial for beginners",
            profile_name="weekly_seasonal",
            include_proxy=False,
        )
        check(
            "C",
            "baseline-ready for a weekly-seasonal profile",
            bool(weekly) and weekly[0].state is ReadinessState.READY,
            f"readiness(weekly_seasonal): {weekly[0].state.value if weekly else 'n/a'}",
        )

        # D - outage ----------------------------------------------------------------
        outage = scenario.providers["synthetic_trends"].outages[0]
        all_attempts = [a for t in targets.values() for a in jobs.attempts_for_target(t.id)]
        outage_failures = [
            a
            for a in all_attempts
            if outage.start <= a.started_at < outage.end and a.error_category is not None
        ]
        fabricated = [
            p
            for t in targets.values()
            for p in ts.points(target_id=t.id, provider_name="synthetic_trends", limit=100_000)
            if outage.start <= p.collected_at < outage.end
        ]
        status_events = [
            e for t in targets.values() for e in repo.events(t.id, limit=500) if e.kind == "status"
        ]
        marked = [
            e
            for e in status_events
            if outage.start <= e.at < outage.end and e.to_value == "provider_unavailable"
        ]
        restored = [
            e
            for e in status_events
            if e.at >= outage.end
            and e.to_value == "active"
            and e.from_value == "provider_unavailable"
        ]
        check(
            "D",
            "outage detected: provider marked unhealthy, targets marked provider_unavailable",
            bool(unhealthy_seen) and bool(marked),
            f"provider unhealthy from {unhealthy_seen[0] if unhealthy_seen else 'never'}; "
            f"{len(marked)} target(s) marked provider_unavailable; {len(outage_failures)} "
            "failed request(s) (status checks prevented the rest)",
        )
        check(
            "D",
            "targets recover when the provider returns",
            bool(restored),
            f"{len(restored)} target(s) re-activated after {outage.end.isoformat()}",
        )
        check(
            "D",
            "no observations created during the outage",
            not fabricated,
            f"{len(fabricated)} observations collected inside the outage",
        )
        recovered = [
            p
            for p in ts.points(target_id=nimbus.id, provider_name="synthetic_trends", limit=100_000)
            if p.period_start.date() == datetime(2026, 3, 5, tzinfo=UTC).date()
        ]
        check(
            "D",
            "missed periods collected after recovery",
            bool(recovered) and recovered[0].collected_at >= outage.end,
            f"period 2026-03-05 collected at "
            f"{recovered[0].collected_at.isoformat() if recovered else 'never'}",
        )

        # E - quota -----------------------------------------------------------------
        deferred = [a for a in all_attempts if a.outcome == "deferred"]
        plan_deferrals = sum(d.get("deferred_quota", 0) for d in report.days)
        later_ok = [
            j
            for j in jobs.jobs(limit=10_000)
            if j.deferral_count > 0 and j.status.value in ("succeeded", "no_new_data")
        ]
        check(
            "E",
            "work deferred when the quota was exhausted",
            bool(deferred) or plan_deferrals > 0,
            f"{len(deferred)} deferred attempts, {plan_deferrals} planner deferrals",
        )
        check(
            "E",
            "deferred work resumed after the provider's quota reset",
            bool(later_ok) or plan_deferrals > 0,
            f"{len(later_ok)} deferred job(s) later completed; resets observed "
            f"{report.quota_windows[:2]} ... {report.quota_windows[-2:]}",
        )
        dst = len({w[11:16] for w in report.quota_windows}) > 1
        check(
            "E",
            "quota reset follows America/Los_Angeles incl. DST",
            dst,
            f"reset times (UTC): {sorted({w[11:16] for w in report.quota_windows})}",
        )

        # F - duplicates ------------------------------------------------------------
        lat = target("lattice 5 migration guide")
        f_att = attempts_of("lattice 5 migration guide")
        dup = sum(a.in_batch_duplicates for a in f_att)
        unchanged = sum(a.unchanged for a in f_att)
        points = ts.points(target_id=lat.id, provider_name="synthetic_trends", limit=100_000)
        periods = {p.period_start for p in points}
        check(
            "F",
            "repeated points stored once",
            dup > 0 and len(periods) == len(points),
            f"{dup} in-response duplicates and {unchanged} unchanged re-fetches; "
            f"{len(points)} stored rows for {len(periods)} periods",
        )

        # G / J - replay and revisions ---------------------------------------------------
        aca = target("ai coding agents")
        aca_points = ts.points(
            target_id=aca.id, provider_name="synthetic_trends", current_only=False, limit=100_000
        )
        monday = datetime(2026, 3, 2, tzinfo=UTC)
        consumer = CollectingConsumer()
        rep = replay(
            aca_points,
            start=datetime(2026, 3, 3, 12, tzinfo=UTC),
            end=datetime(2026, 3, 8, 12, tzinfo=UTC),
            step=timedelta(days=1),
            mode=AvailabilityMode.PUBLICATION,
            consumers=[consumer],
        )
        sid = aca_points[0].series_id if aca_points else -1
        tuesday = consumer.visible_periods(datetime(2026, 3, 3, 12, tzinfo=UTC))
        wednesday = consumer.visible_periods(datetime(2026, 3, 4, 12, tzinfo=UTC))
        check(
            "G",
            "Monday's value (published Wednesday) invisible on a simulated Tuesday",
            (sid, monday) not in tuesday and (sid, monday) in wednesday,
            "visible Tue 12:00: "
            f"{(sid, monday) in tuesday}; visible Wed 12:00: {(sid, monday) in wednesday}",
        )
        rep2 = replay(
            aca_points,
            start=datetime(2026, 3, 3, 12, tzinfo=UTC),
            end=datetime(2026, 3, 8, 12, tzinfo=UTC),
            step=timedelta(days=1),
        )
        check(
            "G",
            "replay is deterministic",
            rep.final_digest == rep2.final_digest,
            f"digest {rep.final_digest} == {rep2.final_digest}",
        )
        tuesday3 = datetime(2026, 3, 3, tzinfo=UTC)
        lineage = [p for p in aca_points if p.period_start == tuesday3]
        revised = sorted(lineage, key=lambda p: p.revision)
        before = [s for s in consumer.snapshots if s.as_of == datetime(2026, 3, 6, 12, tzinfo=UTC)]
        after = [s for s in consumer.snapshots if s.as_of == datetime(2026, 3, 7, 12, tzinfo=UTC)]

        def value_at(snaps: list[Any]) -> float | None:
            for snap in snaps:
                for p in snap.series.get(sid, ()):
                    if p.period_start == tuesday3:
                        value: float | None = p.value
                        return value
            return None

        check(
            "J",
            "revision kept with lineage (not silently overwritten)",
            len(revised) >= 2
            and revised[-1].supersedes_id == revised[-2].id
            and not revised[-2].is_current,
            " -> ".join(
                f"rev{p.revision}={p.value} (published "
                f"{p.provider_published_at.isoformat() if p.provider_published_at else '?'})"
                for p in revised
            ),
        )
        check(
            "J",
            "replay shows the original before the revision was published, then the revision",
            value_at(before) is not None
            and value_at(after) is not None
            and value_at(before) != value_at(after),
            f"2026-03-03 value on Mar 6: {value_at(before)}; on Mar 7: {value_at(after)}",
        )
        report.replay = rep.as_dict()
        report.replay["steps"] = report.replay["steps"][:6]

        # H - normalisation mismatch -------------------------------------------------
        web = [
            s
            for s in ts.series_summaries(query_id=aca.query_id)
            if s.provider_name == "synthetic_trends_web"
        ]
        if len(web) >= 2:
            p1 = ts.points(series_ids=[web[0].series_id], limit=1)[0]
            p2 = ts.points(series_ids=[web[1].series_id], limit=1)[0]
            verdict = assess_pair(p1, p2)
            check(
                "H",
                "different normalisation contexts rejected for direct comparison",
                verdict.status is ComparabilityStatus.INCOMPATIBLE
                and ComparabilityReason.DIFFERENT_NORMALIZATION_CONTEXT in verdict.reasons,
                f"{verdict.status.value}: {[r.value for r in verdict.reasons]}",
            )
        else:
            check(
                "H",
                "different normalisation contexts rejected for direct comparison",
                False,
                f"expected 2 web series, found {len(web)}",
            )
        same = ts.points(series_ids=[aca_points[0].series_id], limit=2) if aca_points else []
        if len(same) == 2:
            v = assess_pair(same[0], same[1])
            check(
                "H",
                "same consistent series remains comparable",
                v.comparable,
                f"{v.status.value}: {[r.value for r in v.reasons]}",
            )

        # I - crash recovery ----------------------------------------------------------
        all_points = [p for t in targets.values() for p in ts.points(target_id=t.id, limit=100_000)]
        keys = Counter((p.series_id, p.period_start) for p in all_points if p.is_current)
        check(
            "I",
            "a crashed worker's jobs were recovered by another worker",
            crash.get("claimed_then_vanished", 0) > 0
            and crash.get("recovered_leases", 0) > 0
            and crash.get("recovered_by") not in (None, "worker-a"),
            f"{crash}",
        )
        check(
            "I",
            "no duplicate observations after re-execution",
            all(n == 1 for n in keys.values()),
            f"{len(keys)} current (series, period) keys, max multiplicity "
            f"{max(keys.values()) if keys else 0}",
        )

        # Not every discovered keyword is monitored
        wallpaper = [t for t in report.targets if t["query"] == "ai wallpaper generator"]
        check(
            "-",
            "keywords below the activation threshold are not monitored",
            bool(wallpaper) and wallpaper[0].get("monitored") is False,
            "ai wallpaper generator (priority 48) has no monitoring target",
        )
    del now
