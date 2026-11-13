"""``trend-agent monitor ...`` commands (Phase 3: historical monitoring).

Monitoring collects observations for discovered keywords from sources that can actually
measure them. Tiers are operational priorities, freshness/readiness describe the data -
nothing printed here is a surge, a forecast or a demand estimate.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

import typer
from rich.markup import escape
from rich.table import Table

from trend_agent import cli as base
from trend_agent.db.repositories.monitoring import MonitoringRepository
from trend_agent.db.repositories.time_series import TimeSeriesRepository
from trend_agent.db.session import transaction
from trend_agent.discovery.config import ConfigError
from trend_agent.discovery.models import Category
from trend_agent.monitoring.backfill import BackfillError, BackfillService
from trend_agent.monitoring.demo import run_demo
from trend_agent.monitoring.metrics import Insights
from trend_agent.monitoring.replay import AvailabilityMode, replay
from trend_agent.monitoring.scheduler import SchedulerLoop
from trend_agent.monitoring.service import MonitoringService
from trend_agent.schemas.monitoring import MeasurementCategory, MonitoringStatus, MonitoringTier
from trend_agent.utils.text import canonicalize_query

monitor_app = typer.Typer(
    help="Historical monitoring: targets, scheduling, time series, freshness, replay.",
    no_args_is_help=True,
)
NOTE = "Tiers are operational priority; nothing here is search volume, a surge or a forecast."
SyntheticOpt = Annotated[
    bool, typer.Option("--include-synthetic", help="Include synthetic (demo/test) targets.")
]


def _service(*, include_synthetic: bool | None = None) -> tuple[Any, MonitoringService]:
    container = base._container(require_db=True)
    try:
        return container, container.monitoring(include_synthetic=include_synthetic)
    except ConfigError as exc:
        container.engine.dispose()
        raise base._fail(f"monitoring configuration: {exc}", 2) from None


def _close(container: Any) -> None:
    base._run(container, base._noop)


# --------------------------------------------------------------------------- targets
@monitor_app.command("targets")
def monitor_targets(
    status: Annotated[list[MonitoringStatus] | None, typer.Option("--status")] = None,
    category: Annotated[Category | None, typer.Option("--category", "-c")] = None,
    tier: Annotated[MonitoringTier | None, typer.Option("--tier")] = None,
    include_synthetic: SyntheticOpt = False,
    limit: Annotated[int, typer.Option(min=1, max=10_000)] = 100,
    as_json: base.JsonOpt = False,
) -> None:
    """List monitoring targets with category, priority, provider, status and next collection."""
    container, svc = _service()
    with transaction(svc.session_factory) as session:
        repo = MonitoringRepository(session)
        rows = repo.targets(
            statuses=status,
            category=category,
            tier=tier,
            include_synthetic=include_synthetic,
            limit=limit,
        )
        texts = repo.query_text(list({t.query_id for t in rows}))
        keywords = repo.keywords_by_ids([t.keyword_id for t in rows if t.keyword_id])
        data: list[dict[str, Any]] = [
            {
                "id": t.id,
                "query": texts[t.query_id][0],
                "category": t.category.value,
                "priority": t.priority_score,
                "measurement": t.measurement_category.value,
                "proxy": t.is_proxy,
                "region": t.region,
                "language": t.language,
                "provider": t.provider_in_use,
                "preferred_provider": t.preferred_provider,
                "fallback_providers": list(t.fallback_providers),
                "tier": t.tier.value,
                "status": t.status.value,
                "status_reason": t.status_reason,
                "keyword_status": (
                    keywords[t.keyword_id].candidate_status.value
                    if t.keyword_id in keywords
                    else None
                ),
                "topic_cluster_id": t.topic_cluster_id,
                "intent_cluster_id": t.intent_cluster_id,
                "last_attempt_at": t.last_attempt_at,
                "last_success_at": t.last_success_at,
                "last_new_observation_at": t.last_new_observation_at,
                "next_collection_at": t.next_collection_at,
                "latest_period_end": t.latest_period_end,
                "config_version": t.config_version,
                "synthetic": t.is_synthetic,
            }
            for t in rows
        ]
    _close(container)
    if as_json:
        base._emit_json(data)
        return
    if not data:
        base.console.print(
            "No monitoring targets"
            + ("" if include_synthetic else " (try --include-synthetic, or `monitor sync`)")
            + "."
        )
        return
    table = Table(
        "id",
        "query",
        "cat",
        "prio",
        "measurement",
        "provider",
        "tier",
        "status",
        "next collection",
        title="Monitoring targets",
    )
    for d in data:
        label = escape(d["query"]) + (" [dim](synthetic)[/]" if d["synthetic"] else "")
        measurement = d["measurement"] + (" [yellow](proxy)[/]" if d["proxy"] else "")
        table.add_row(
            str(d["id"]),
            label,
            d["category"],
            f"{d['priority']:.0f}",
            measurement,
            d["provider"] or "-",
            d["tier"],
            d["status"],
            base._fmt(d["next_collection_at"]),
        )
    base.console.print(table)
    base.console.print(f"[dim]{NOTE}[/]")


@monitor_app.command("sync")
def monitor_sync(as_json: base.JsonOpt = False) -> None:
    """Select/refresh targets from discovered keywords (activation, demotion, tiers)."""
    container, svc = _service()
    report = svc.sync_targets()
    _close(container)
    data = report.__dict__
    if as_json:
        base._emit_json(data)
        return
    base.console.print(
        f"candidates {report.candidates_seen}, created {report.targets_created}, activated "
        f"{report.activated}, waiting for capacity {report.pending_capacity}, provider "
        f"unavailable {report.provider_unavailable}, dormant {report.demoted_dormant}, "
        f"disabled {report.disabled}, tier changes {report.tier_changes}"
    )


@monitor_app.command("activate")
def monitor_activate(
    query: Annotated[str, typer.Option("--query", "-q")],
    proxy: Annotated[
        bool,
        typer.Option(
            "--proxy", help="Activate proxy (event-signal) monitoring instead of search interest."
        ),
    ] = False,
) -> None:
    """Activate monitoring for an existing keyword if a suitable provider is available."""
    container, svc = _service()
    ok, message = svc.targets.activate(query, proxy=proxy)
    _close(container)
    (base.console.print if ok else base.err_console.print)(escape(message))
    if not ok:
        raise typer.Exit(1)


def _manual(query: str, status: MonitoringStatus, reason: str) -> None:
    container, svc = _service()
    lines = svc.targets.set_manual_status(query, status, reason)
    _close(container)
    if not lines:
        raise base._fail(f"no monitoring target for {canonicalize_query(query)!r}", 1)
    for line in lines:
        base.console.print(line)


@monitor_app.command("pause")
def monitor_pause(query: Annotated[str, typer.Option("--query", "-q")]) -> None:
    """Pause monitoring (queued jobs are cancelled; history is kept)."""
    _manual(query, MonitoringStatus.PAUSED, "paused manually")


@monitor_app.command("resume")
def monitor_resume(query: Annotated[str, typer.Option("--query", "-q")]) -> None:
    """Resume a paused target."""
    _manual(query, MonitoringStatus.ACTIVE, "resumed manually")


@monitor_app.command("disable")
def monitor_disable(query: Annotated[str, typer.Option("--query", "-q")]) -> None:
    """Exclude a keyword from monitoring (re-enable with `monitor activate`)."""
    _manual(query, MonitoringStatus.DISABLED, "excluded manually")


@monitor_app.command("override")
def monitor_override(
    query: Annotated[str, typer.Option("--query", "-q")],
    tier: Annotated[str | None, typer.Option(help="Pin a tier, or 'auto' to unpin.")] = None,
    collect_now: Annotated[
        bool, typer.Option("--collect-now", help="Schedule a collection now.")
    ] = False,
    keep_active: Annotated[bool | None, typer.Option("--keep-active/--allow-demotion")] = None,
) -> None:
    """Manual overrides: pin a tier, force a collection, block demotion."""
    override: dict[str, object] = {}
    if tier is not None:
        if tier != "auto" and tier not in {t.value for t in MonitoringTier}:
            raise base._fail(f"unknown tier {tier!r}", 2)
        override["pinned_tier"] = None if tier == "auto" else tier
    if collect_now:
        container, svc = _service()
        override["force_at"] = svc.now().isoformat()
        _close(container)
    if keep_active is not None:
        override["pinned_active"] = True if keep_active else None
    if not override:
        raise base._fail("nothing to change (use --tier, --collect-now or --keep-active)", 2)
    container, svc = _service()
    ids = svc.targets.set_override(query, **override)
    _close(container)
    if not ids:
        raise base._fail(f"no monitoring target for {canonicalize_query(query)!r}", 1)
    base.console.print(f"override applied to target(s) {ids}: {override}")


# --------------------------------------------------------------------------- planning
def _print_plan(report: Any) -> None:
    base.console.print(
        f"{'DRY RUN - ' if report.dry_run else ''}capacity {report.capacity}, due "
        f"{report.due_targets}, planned {len(report.planned)}, created {report.jobs_created}, "
        f"already queued {report.jobs_already_queued}, skipped (no new data expected) "
        f"{report.skipped_no_new_data}, skipped (min poll interval) {report.skipped_backoff}, "
        f"deferred (quota) {report.deferred_quota}, provider unavailable "
        f"{report.provider_unavailable}"
    )
    table = Table(
        "category",
        "reserved",
        "explore",
        "used",
        "explore used",
        "redistributed",
        "candidates",
        title="Category allocation (discretionary capacity)",
    )
    for cat, u in report.categories.items():
        table.add_row(
            cat,
            str(u.reserved),
            str(u.exploration_reserved),
            str(u.used),
            str(u.exploration_used),
            str(u.redistributed_in),
            str(u.candidates),
        )
    base.console.print(table)
    if report.planned:
        jobs = Table(
            "target",
            "provider",
            "cat",
            "tier",
            "pool",
            "score",
            "cost",
            "reason",
            title="Planned jobs",
        )
        for p in report.planned[:50]:
            jobs.add_row(
                str(p.target_id),
                p.provider_name,
                p.category.value,
                p.tier.value,
                p.pool,
                f"{p.score:.2f}",
                str(p.estimated_cost),
                escape(p.reason),
            )
        base.console.print(jobs)
    if report.estimated_quota:
        base.console.print(f"estimated quota use: {report.estimated_quota}")
    for incident in report.quota_incidents:
        base.console.print(f"[yellow]quota:[/] {escape(incident)}")
    for note in report.notes:
        base.console.print(f"[dim]{escape(note)}[/]")


@monitor_app.command("plan")
def monitor_plan(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Decide, but write nothing.")] = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Plan collection jobs (category budgets, cadence, quota). --dry-run executes nothing."""
    container, svc = _service()
    report = svc.plan(dry_run=dry_run)
    _close(container)
    if as_json:
        base._emit_json(report.model_dump(mode="json"))
        return
    _print_plan(report)


@monitor_app.command("run")
def monitor_run(
    once: Annotated[
        bool, typer.Option("--once", help="Sync, plan and execute eligible jobs once.")
    ] = True,
    as_json: base.JsonOpt = False,
) -> None:
    """Sync targets, plan, and execute all eligible jobs once."""
    container, svc = _service()
    result = base._run(container, lambda: svc.run_once())
    statuses: dict[str, int] = {}
    totals: dict[str, int] = {}
    for w in result.workers:
        for k, v in w.by_status().items():
            statuses[k] = statuses.get(k, 0) + v
        for k, v in w.totals().items():
            totals[k] = totals.get(k, 0) + v
    data = {
        "sync": result.sync.__dict__,
        "plan": result.plan.model_dump(mode="json"),
        "jobs": statuses,
        "observations": totals,
    }
    if as_json:
        base._emit_json(data)
        return
    _print_plan(result.plan)
    base.console.print(f"jobs: {statuses or 'none'}; observations: {totals or 'none'}")
    base.console.print(
        "[dim]A succeeded request is not a data refresh: 'no_new_data' means "
        "the source had nothing new.[/]"
    )


@monitor_app.command("worker")
def monitor_worker(
    interval: Annotated[float, typer.Option(min=1.0, help="Seconds between polls.")] = 15.0,
    worker_id: Annotated[str | None, typer.Option("--id")] = None,
    once: Annotated[bool, typer.Option("--once", help="Drain eligible jobs and exit.")] = False,
    max_ticks: Annotated[int | None, typer.Option(hidden=True)] = None,
) -> None:
    """Run a worker: claim jobs (SKIP LOCKED leases), collect, store. Ctrl-C to stop."""
    container, svc = _service()
    executor = svc.executor(worker_id)

    async def tick() -> None:
        reports = await executor.drain(max_batches=100)
        claimed = sum(r.claimed for r in reports)
        if claimed:
            base.console.print(f"{executor.worker_id}: executed {claimed} job(s)")

    async def main() -> int:
        if once:
            await tick()
            return 1
        loop = SchedulerLoop(tick, interval=timedelta(seconds=interval), name="monitor.worker")
        loop.install_signal_handlers()
        return await loop.run(max_ticks=max_ticks)

    base.console.print(
        f"worker {executor.worker_id} started (lease "
        f"{svc.config.jobs.lease_duration}, concurrency "
        f"{svc.config.jobs.worker_concurrency})"
    )
    base._run(container, main)


@monitor_app.command("scheduler")
def monitor_scheduler(
    interval: Annotated[float, typer.Option(min=5.0, help="Seconds between cycles.")] = 300.0,
    once: Annotated[bool, typer.Option("--once", help="One sync + planning cycle.")] = False,
    max_ticks: Annotated[int | None, typer.Option(hidden=True)] = None,
) -> None:
    """Run the scheduler: periodic target sync + planning (jobs are executed by workers)."""
    container, svc = _service()

    async def tick() -> None:
        sync = svc.sync_targets()
        plan = svc.plan()
        base.console.print(
            f"{svc.now():%Y-%m-%d %H:%M} activated {sync.activated}, jobs created "
            f"{plan.jobs_created}, skipped (no new data) {plan.skipped_no_new_data}, "
            f"deferred (quota) {plan.deferred_quota}"
        )

    async def main() -> int:
        if once:
            await tick()
            return 1
        loop = SchedulerLoop(tick, interval=timedelta(seconds=interval), name="monitor.scheduler")
        loop.install_signal_handlers()
        return await loop.run(max_ticks=max_ticks)

    base._run(container, main)


# --------------------------------------------------------------------------- health
@monitor_app.command("status")
def monitor_status(
    window_hours: Annotated[int, typer.Option(min=1, max=24 * 90)] = 24,
    as_json: base.JsonOpt = False,
) -> None:
    """Overall monitoring health: targets, queue, attempts, quota, providers, coverage."""
    container, svc = _service()
    data = Insights(svc).status(window=timedelta(hours=window_hours))
    _close(container)
    if as_json:
        base._emit_json(data)
        return
    t = data["targets"]
    base.console.print(
        f"[bold]targets[/] active {t['active_total']} (search interest "
        f"{t['active_search_interest']}, proxy {t['active_proxy']}); by status {t['by_status']}"
    )
    base.console.print(f"active by tier: {t['active_by_tier']}")
    for cat, counts in t["by_category"].items():
        base.console.print(f"  {cat}: {counts}")
    j = data["jobs"]
    base.console.print(f"[bold]queue[/] depth {j['queue_depth']}; {j['by_status']}")
    a = data["attempts"]
    base.console.print(
        f"[bold]last {window_hours}h[/] attempts {a['attempts']}, succeeded {a['succeeded']}, "
        f"no new data {a['no_new_data']}, retry {a['retry_wait']}, failed {a['failed']}, "
        f"deferred {a['deferred']}, lease expired {a['lease_expired']}; observations new "
        f"{a['new_observations']}, unchanged {a['unchanged_observations']}, revised "
        f"{a['revised_observations']}, conflicting {a['conflicting_observations']}; latency "
        f"p50/p95 {a['latency_ms_p50']}/{a['latency_ms_p95']} ms; schedule delay p50/p95 "
        f"{a['scheduled_delay_ms_p50']}/{a['scheduled_delay_ms_p95']} ms; errors "
        f"{a['errors_by_category']}"
    )
    for q in data["quota"]:
        base.console.print(
            f"[bold]quota[/] {q['provider']}: {q['used']}/{q['limit']} used, "
            f"resets {q['window_end']} ({q['reset_timezone']})"
        )
    for h in data["provider_health"]:
        base.console.print(
            f"[bold]provider[/] {h['provider']}: {h['status']} "
            f"(failures {h['consecutive_failures']})"
        )
    base.console.print(f"[bold]time series[/] {data['time_series']['observations']}")
    base.console.print(f"[bold]baseline readiness[/] {data['baseline_readiness'] or 'no targets'}")
    base.console.print(f"[dim]{NOTE}[/]")


@monitor_app.command("providers")
def monitor_providers(as_json: base.JsonOpt = False) -> None:
    """Provider capabilities, freshness profile, availability and quota."""
    container, svc = _service()
    rows = svc.providers()
    _close(container)
    data = [
        {**r.__dict__, "delay": r.delay.model_dump(mode="json") if r.delay else None} for r in rows
    ]
    if as_json:
        base._emit_json(data)
        return
    table = Table(
        "provider",
        "collector",
        "measures",
        "available",
        "status",
        "health",
        "cadence",
        "delay",
        "detail",
        title="Monitoring providers",
    )
    for r in rows:
        prof = r.profile
        table.add_row(
            r.name + (" [dim](synthetic)[/]" if r.synthetic else ""),
            r.collector,
            ", ".join(r.measurement_categories),
            "yes" if r.available else "no",
            r.status,
            r.health,
            str(prof.get("publication_cadence", "irregular")),
            str(prof.get("typical_delay", "-")),
            escape(r.detail[:80]),
        )
    base.console.print(table)


@monitor_app.command("freshness")
def monitor_freshness(
    query: Annotated[str | None, typer.Option("--query", "-q")] = None,
    include_synthetic: SyntheticOpt = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Provider and target freshness (source current / delayed / unavailable / unknown)."""
    container, svc = _service()
    data = Insights(svc).freshness(query=query, include_synthetic=include_synthetic or bool(query))
    _close(container)
    if as_json:
        base._emit_json(data)
        return
    table = Table(
        "provider",
        "available",
        "health",
        "cadence h",
        "delay p50/p90 h",
        "basis",
        "targets",
        title="Provider freshness",
    )
    for p in data["providers"]:
        d = p["delay"] or {}
        table.add_row(
            p["provider"],
            "yes" if p["available"] else "no",
            p["health"],
            str(p["publication_cadence_hours"] or "irregular"),
            f"{d.get('p50_hours')}/{d.get('p90_hours')}",
            str(d.get("basis")),
            str(p["targets"] or "-"),
        )
    base.console.print(table)
    targets = Table(
        "target",
        "query",
        "provider",
        "tier",
        "freshness",
        "latest period end",
        "reason",
        title="Target freshness",
    )
    for t in data["targets"][:200]:
        targets.add_row(
            str(t["target_id"]),
            escape(t["query"]),
            t["provider"] or "-",
            t["tier"],
            t["freshness"],
            t["latest_period_end"] or "-",
            escape(t["reason"][:90]),
        )
    base.console.print(targets)
    base.console.print("[dim]Delays are estimates from observed behaviour, not guarantees.[/]")


@monitor_app.command("readiness")
def monitor_readiness(
    query: Annotated[str | None, typer.Option("--query", "-q")] = None,
    profile: Annotated[
        str | None, typer.Option(help="Readiness profile (monitoring.yaml).")
    ] = None,
    include_synthetic: SyntheticOpt = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Baseline readiness for Phase 4 analysis, with the exact reasons (no surge scores)."""
    container, svc = _service()
    if profile is not None and profile not in svc.config.readiness.profiles:
        _close(container)
        raise base._fail(
            f"unknown readiness profile {profile!r}; defined: "
            f"{sorted(svc.config.readiness.profiles)}",
            2,
        )
    rows = Insights(svc).readiness(
        query=query, profile_name=profile, include_synthetic=include_synthetic or bool(query)
    )
    _close(container)
    if as_json:
        base._emit_json([r.as_dict() for r in rows])
        return
    table = Table(
        "target",
        "query",
        "provider",
        "state",
        "obs",
        "span d",
        "coverage",
        "missing",
        "reasons",
        title="Baseline readiness",
    )
    for r in rows:
        b = r.best_series
        table.add_row(
            str(r.target_id),
            escape(r.query),
            r.provider or "-",
            r.state.value,
            str(b.comparable_observations if b else 0),
            str(b.span_days if b else 0),
            f"{b.coverage:.0%}" if b else "-",
            str(b.missing_periods if b else 0),
            escape("; ".join(r.reasons)[:120]),
        )
    base.console.print(table)
    base.console.print("[dim]Readiness measures history; it is not a prediction or surge score.[/]")


@monitor_app.command("quality")
def monitor_quality(
    query: Annotated[str | None, typer.Option("--query", "-q")] = None,
    include_synthetic: SyntheticOpt = False,
    as_json: base.JsonOpt = False,
) -> None:
    """Data-quality findings at provider, category, target and series level."""
    container, svc = _service()
    data = Insights(svc).quality(query=query, include_synthetic=include_synthetic or bool(query))
    _close(container)
    if as_json:
        base._emit_json(data)
        return
    for level in ("providers", "categories", "targets"):
        table = Table("scope", "status", "findings", title=f"Quality - {level}")
        for r in data[level]:
            label = r["scope"] + (f" {r.get('query', '')}" if level == "targets" else "")
            findings = (
                "; ".join(f"{f['issue']}({f['severity']},{f['count']})" for f in r["findings"])
                or "-"
            )
            table.add_row(escape(label), r["status"], escape(findings[:140]))
        base.console.print(table)
    base.console.print(
        "[dim]Source failures and low/missing values are reported separately; "
        "missing values are never zero demand.[/]"
    )


@monitor_app.command("quota")
def monitor_quota(as_json: base.JsonOpt = False) -> None:
    """API budgets: remaining quota, resets (provider time zone), queued and used estimates."""
    container, svc = _service()
    providers = svc.providers()
    status = Insights(svc).status()
    youtube = [
        {
            "bucket": u.bucket,
            "used": u.used,
            "limit": u.effective_limit,
            "daily_limit": u.daily_limit,
            "window_end": u.window_end.isoformat(),
            "server_exhausted": u.server_exhausted,
        }
        for u in container.youtube().quota.status()
    ]
    _close(container)
    data = {
        "monitoring_quotas": [{"provider": p.name, **p.quota} for p in providers if p.quota],
        "youtube_data_api": youtube,
        "estimated_usage_last_24h": status["attempts"]["quota_used_by_provider"],
        "queued_jobs": status["jobs"]["by_status"],
        "deferred_attempts_last_24h": status["attempts"]["deferred"],
        "note": "local estimates; the provider's own console is authoritative",
    }
    if as_json:
        base._emit_json(data)
        return
    table = Table(
        "provider",
        "bucket",
        "used",
        "limit",
        "remaining",
        "resets (UTC)",
        "time zone",
        title="Monitoring quotas (local estimates)",
    )
    for q in data["monitoring_quotas"]:
        table.add_row(
            q["provider"],
            q["bucket"],
            str(q["used"]),
            str(q["limit"]),
            str(q["remaining"]),
            q["window_end"],
            q["reset_timezone"],
        )
    for y in youtube:
        table.add_row(
            "youtube_data_api",
            y["bucket"],
            str(y["used"]),
            str(y["limit"]),
            str(max(0, y["limit"] - y["used"])),
            y["window_end"],
            container.settings.youtube_quota_timezone,
        )
    base.console.print(table)
    base.console.print(
        f"usage last 24 h: {data['estimated_usage_last_24h'] or 'none'}; "
        f"deferred attempts: {data['deferred_attempts_last_24h']}"
    )
    base.console.print(f"[dim]{data['note']}[/]")


# --------------------------------------------------------------------------- data
@monitor_app.command("history")
def monitor_history(
    query: Annotated[str, typer.Option("--query", "-q")],
    provider: Annotated[str | None, typer.Option("--provider", "-p")] = None,
    revisions: Annotated[bool, typer.Option("--revisions/--current-only")] = True,
    limit: Annotated[int, typer.Option(min=1, max=100_000)] = 60,
    as_json: base.JsonOpt = False,
) -> None:
    """Historical observations with provenance and revision lineage."""
    container, svc = _service()
    data = Insights(svc).history(
        query=query, provider=provider, include_revisions=revisions, limit=100_000
    )
    _close(container)
    if as_json:
        base._emit_json(data)
        return
    if not data["series"]:
        raise base._fail(f"no observations stored for {data['query']!r}", 1)
    for s in data["series"]:
        label = (
            f"{s['provider']} / {s['measurement']}"
            + (f" (simulates {s['simulates']})" if s["simulates"] else "")
            + (" PROXY" if s["proxy"] else "")
            + (" SYNTHETIC" if s["synthetic"] else "")
        )
        table = Table(
            "period",
            "value",
            "quality",
            "rev",
            "published",
            "collected",
            "policy",
            title=f"series {s['series_id']}: {label} [{s['region']}, {s['granularity']}, "
            f"{s['scale_type']}, norm {s['normalization_key']}]",
        )
        for p in s["points"][-limit:]:
            rev = f"{p['revision']}{'' if p['current'] else '*'} {p['revision_kind']}"
            table.add_row(
                p["period_start"][:10],
                "NULL" if p["value"] is None else str(p["value"]),
                p["sample_quality"],
                rev,
                (p["provider_published_at"] or "unknown")[:16],
                p["collected_at"][:16],
                p["data_policy"],
            )
        base.console.print(table)
    base.console.print(
        "[dim]* = superseded revision or conflict (kept for lineage). NULL is "
        "missing data, not zero. Series with different normalisation are never "
        "combined.[/]"
    )


@monitor_app.command("backfill")
def monitor_backfill(
    query: Annotated[str, typer.Option("--query", "-q")],
    provider: Annotated[str, typer.Option("--provider", "-p")],
    days: Annotated[int, typer.Option(min=1, max=3650)] = 30,
    measurement: Annotated[MeasurementCategory | None, typer.Option()] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Plan and estimate only.")] = False,
    execute: Annotated[bool, typer.Option("--execute/--queue-only")] = True,
    as_json: base.JsonOpt = False,
) -> None:
    """Historical backfill for providers that support it (resumable, quota-aware)."""
    container, svc = _service()
    service = BackfillService(svc.session_factory, svc.config, svc.collectors, clock=svc.now)
    try:
        plan = service.plan(
            query=query, provider=provider, days=days, measurement=measurement, dry_run=dry_run
        )
    except BackfillError as exc:
        _close(container)
        raise base._fail(str(exc), 1) from None
    outcome: dict[str, int] = {}
    if execute and not dry_run and plan.job_ids:
        reports = base._run(
            container,
            lambda: svc.executor("backfill").run_once(
                job_ids=plan.job_ids, limit=len(plan.job_ids)
            ),
        )
        outcome = reports.by_status()
    else:
        _close(container)
    data = {
        "backfill_id": str(plan.backfill_id) if plan.backfill_id else None,
        "target_id": plan.target_id,
        "provider": plan.provider,
        "query": plan.query,
        "measurement": plan.measurement,
        "range": [plan.range_start.isoformat(), plan.range_end.isoformat()],
        "windows": [[a.isoformat(), b.isoformat()] for a, b in plan.windows],
        "skipped_already_covered": plan.skipped_covered,
        "estimated_requests": plan.estimated_requests,
        "quota_remaining": plan.quota_remaining,
        "limitations": plan.limitations,
        "resumed": plan.resumed,
        "dry_run": dry_run,
        "jobs_created": plan.jobs_created,
        "jobs": outcome,
    }
    if as_json:
        base._emit_json(data)
        return
    base.console.print(
        f"{'DRY RUN - ' if dry_run else ''}backfill {plan.query!r} via {plan.provider} "
        f"({plan.measurement}) {plan.range_start:%Y-%m-%d}..{plan.range_end:%Y-%m-%d}: "
        f"{len(plan.windows)} window(s), {plan.skipped_covered} already covered, "
        f"~{plan.estimated_requests} request(s); quota remaining {plan.quota_remaining}"
        + (" (resumed)" if plan.resumed else "")
    )
    for note in plan.limitations:
        base.console.print(f"[yellow]limitation:[/] {escape(note)}")
    if outcome:
        base.console.print(f"jobs: {outcome}")


@monitor_app.command("replay")
def monitor_replay(
    query: Annotated[str, typer.Option("--query", "-q")],
    provider: Annotated[str | None, typer.Option("--provider", "-p")] = None,
    start: Annotated[datetime | None, typer.Option(help="Replay clock start (UTC).")] = None,
    end: Annotated[datetime | None, typer.Option(help="Replay clock end (UTC).")] = None,
    step_hours: Annotated[int, typer.Option(min=1, max=24 * 31)] = 24,
    mode: Annotated[AvailabilityMode, typer.Option()] = AvailabilityMode.PUBLICATION,
    as_json: base.JsonOpt = False,
) -> None:
    """Replay stored observations as they became available (no look-ahead)."""
    from trend_agent.utils.time import ensure_utc

    container, svc = _service()
    canonical = canonicalize_query(query)
    with transaction(svc.session_factory) as session:
        repo = MonitoringRepository(session)
        ts = TimeSeriesRepository(session)
        target_ids = [t.id for t in repo.targets(query_text=canonical, limit=100)]
        points = []
        for tid in target_ids:
            points.extend(
                ts.points(
                    target_id=tid, provider_name=provider, current_only=False, limit=1_000_000
                )
            )
    _close(container)
    if not points:
        raise base._fail(
            f"no stored observations for {canonical!r}" + (f" from {provider}" if provider else ""),
            1,
        )
    first = min(p.period_end for p in points)
    last = max(
        max(p.collected_at, p.ingested_at, p.provider_published_at or p.collected_at)
        for p in points
    )
    begin = ensure_utc(start) if start else first
    finish = ensure_utc(end) if end else last
    report = replay(points, start=begin, end=finish, step=timedelta(hours=step_hours), mode=mode)
    data = report.as_dict()
    if as_json:
        base._emit_json(data)
        return
    base.console.print(
        f"replay {canonical!r} ({mode.value} availability) {begin:%Y-%m-%d %H:%M}.."
        f"{finish:%Y-%m-%d %H:%M} every {step_hours}h: {report.events} events in "
        f"{report.series} series; late arrivals {report.late_arrivals}; basis "
        f"{report.basis_counts}; final digest {report.final_digest}"
    )
    table = Table(
        "as of",
        "visible",
        "new",
        "revisions",
        "not yet available",
        "latest visible period end",
        "digest",
        title="Replay steps",
    )
    for s in report.steps[-30:]:
        table.add_row(
            f"{s.as_of:%Y-%m-%d %H:%M}",
            str(s.visible),
            str(s.newly_available),
            str(s.revisions_applied),
            str(s.withheld_not_yet_available),
            f"{s.latest_visible_period_end:%Y-%m-%d}" if s.latest_visible_period_end else "-",
            s.digest,
        )
    base.console.print(table)
    base.console.print(
        "[dim]Each step shows only data available at that time "
        "(publication time, else conservative collection/ingestion time).[/]"
    )


@monitor_app.command("demo")
def monitor_demo(
    reset: Annotated[
        bool,
        typer.Option("--reset/--no-reset", help="Remove earlier synthetic monitoring data first."),
    ] = True,
    as_json: base.JsonOpt = False,
) -> None:
    """End-to-end deterministic demonstration on SYNTHETIC data (scenarios A-J)."""
    container = base._container(require_db=True)
    try:
        container.monitoring_config()
    except ConfigError as exc:
        container.engine.dispose()
        raise base._fail(f"monitoring configuration: {exc}", 2) from None
    report = base._run(container, lambda: run_demo(container, reset=reset))
    data = report.as_dict()
    if as_json:
        base._emit_json(data)
    else:
        base.console.print(
            "[bold yellow]SYNTHETIC MONITORING DEMO - fictional subjects; "
            "simulated clock; not real search interest.[/]"
        )
        table = Table(
            "query",
            "cat",
            "tier",
            "status",
            "attempts",
            "obs",
            "freshness",
            "readiness",
            title=f"Targets after {report.hours} simulated hours",
        )
        for t in report.targets:
            if t.get("monitored") is False:
                table.add_row(
                    escape(t["query"]), t["category"], "-", "not monitored", "-", "-", "-", "-"
                )
                continue
            table.add_row(
                escape(t["query"]),
                t["category"],
                t["tier"],
                t["status"],
                str(t["attempts"]),
                str(t["observations"]),
                str(t["freshness"]),
                str(t["readiness"]),
            )
        base.console.print(table)
        checks = Table("scenario", "check", "result", "detail", title="Scenario checks")
        for c in report.checks:
            checks.add_row(
                c.scenario,
                escape(c.name),
                "[green]PASS[/]" if c.passed else "[red]FAIL[/]",
                escape(c.detail[:150]),
            )
        base.console.print(checks)
        base.console.print(f"[dim]{NOTE}[/]")
    if not report.passed:
        raise typer.Exit(1)
